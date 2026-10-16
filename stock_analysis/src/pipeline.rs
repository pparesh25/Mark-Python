use crate::chart::{create_stock_chart, EodData};
use crate::screener::{self, FlagResult, MarkResult, ScreenOptions, SymbolData};
use chrono::{Duration, NaiveDate};
use rayon::prelude::*;
use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Strategy {
    MarkTight,
    TightFlag,
}

impl Strategy {
    fn suffix(self, exchange: &str) -> String {
        match self {
            Self::MarkTight => format!("{}_mark_tight", exchange.to_lowercase()),
            Self::TightFlag => format!("{}_tight_flag", exchange.to_lowercase()),
        }
    }

    fn folder(self) -> &'static str {
        match self {
            Self::MarkTight => "mark_tight",
            Self::TightFlag => "Tight Flag",
        }
    }

    fn plot_days(self) -> i64 {
        match self {
            Self::MarkTight => 500,
            Self::TightFlag => 200,
        }
    }
}

struct FundamentalTable {
    headers: Vec<String>,
    header_index: HashMap<String, usize>,
    rows_by_isin: HashMap<String, Vec<String>>,
}

impl FundamentalTable {
    fn load(path: &Path) -> Result<Self, Box<dyn Error>> {
        let mut reader = csv::Reader::from_path(path)?;
        let headers: Vec<String> = reader
            .headers()?
            .iter()
            .map(|value| value.trim().to_string())
            .collect();
        let header_index: HashMap<String, usize> = headers
            .iter()
            .enumerate()
            .map(|(index, value)| (value.clone(), index))
            .collect();
        let isin_index = header_index
            .get("ISIN Code")
            .copied()
            .ok_or("ISIN Code column missing in fundamental CSV")?;
        let mut rows_by_isin = HashMap::new();
        for record in reader.records() {
            let record = record?;
            let row: Vec<String> = record.iter().map(str::to_string).collect();
            let isin = row
                .get(isin_index)
                .map(|value| value.trim().to_uppercase())
                .unwrap_or_default();
            if !isin.is_empty() && isin != "NAN" {
                rows_by_isin.entry(isin).or_insert(row);
            }
        }
        Ok(Self {
            headers,
            header_index,
            rows_by_isin,
        })
    }

    fn row(&self, isin: &str) -> Option<&Vec<String>> {
        self.rows_by_isin.get(&isin.trim().to_uppercase())
    }

    fn value<'a>(&self, row: &'a [String], column: &str) -> &'a str {
        self.header_index
            .get(column)
            .and_then(|index| row.get(*index))
            .map(String::as_str)
            .unwrap_or("")
            .trim()
    }
}

#[derive(Clone)]
enum CandidateResult {
    Mark(MarkResult),
    Flag(FlagResult),
}

#[derive(Clone)]
struct Candidate {
    strategy: Strategy,
    exchange: String,
    symbol: String,
    isin: String,
    breakout: NaiveDate,
    days_since_breakout: i64,
    fundamental: Vec<String>,
    result: CandidateResult,
}

type CandidateGroups = HashMap<(String, Strategy), Vec<Candidate>>;

#[derive(Clone)]
struct ChartJob {
    index: usize,
    strategy: Strategy,
    exchange: String,
    symbol: String,
    breakout: NaiveDate,
    pattern_start: Option<NaiveDate>,
    pattern_end: Option<NaiveDate>,
    info_line1: String,
    info_line2: String,
    info_line3: String,
    output_dir: PathBuf,
}

struct PipelineOptions {
    include_sme: bool,
    only_sme: bool,
    flag_lookback: usize,
    max_stale_sessions: usize,
    write_watchlists: bool,
    strip_sme_suffix: bool,
}

impl Default for PipelineOptions {
    fn default() -> Self {
        Self {
            include_sme: false,
            only_sme: false,
            flag_lookback: 90,
            max_stale_sessions: ScreenOptions::default().max_stale_sessions,
            write_watchlists: true,
            strip_sme_suffix: false,
        }
    }
}

fn parse_date(value: &str) -> Result<NaiveDate, Box<dyn Error>> {
    Ok(NaiveDate::parse_from_str(
        value.split('T').next().unwrap_or(value).trim(),
        "%Y-%m-%d",
    )?)
}

fn run_date_from_timestamp(timestamp: &str) -> Result<NaiveDate, Box<dyn Error>> {
    let compact_date = timestamp
        .get(0..8)
        .ok_or("pipeline timestamp must start with YYYYMMDD")?;
    Ok(NaiveDate::parse_from_str(compact_date, "%Y%m%d")?)
}

fn clean_optional_date(value: &Option<String>) -> Option<NaiveDate> {
    value
        .as_deref()
        .filter(|text| !text.trim().is_empty())
        .and_then(|text| parse_date(text).ok())
}

fn format_number(value: f64) -> String {
    if value.fract().abs() < f64::EPSILON {
        format!("{value:.1}")
    } else {
        value.to_string()
    }
}

/// Days_Since_Breakout counts calendar days from the exchange's latest stored
/// session, so a run on a weekend or holiday gives the same numbers as one on
/// the last trading day.  The run date is only a fallback for an exchange with
/// no session at all.
fn recency_anchor(dataset: &screener::ScreenedDataset, run_date: NaiveDate) -> NaiveDate {
    dataset
        .latest_session
        .as_deref()
        .and_then(|value| parse_date(value).ok())
        .unwrap_or(run_date)
}

fn collect_candidates(
    run_date: NaiveDate,
    fundamental: &FundamentalTable,
    datasets: HashMap<String, screener::ScreenedDataset>,
) -> Result<CandidateGroups, Box<dyn Error>> {
    let mut grouped = HashMap::new();
    for exchange in ["NSE", "BSE"] {
        let Some(dataset) = datasets.get(exchange) else {
            continue;
        };
        let latest_session = recency_anchor(dataset, run_date);
        let mut marks = Vec::new();
        for result in &dataset.mark_tight_qualified {
            let Some(fundamental_row) = fundamental.row(&result.isin) else {
                continue;
            };
            let breakout = parse_date(&result.mark_tight_date)?;
            marks.push(Candidate {
                strategy: Strategy::MarkTight,
                exchange: exchange.to_string(),
                symbol: result.symbol.clone(),
                isin: result.isin.clone(),
                breakout,
                days_since_breakout: (latest_session - breakout).num_days(),
                fundamental: fundamental_row.clone(),
                result: CandidateResult::Mark(result.clone()),
            });
        }
        sort_candidates(&mut marks);
        grouped.insert((exchange.to_string(), Strategy::MarkTight), marks);

        let mut flags = Vec::new();
        for result in &dataset.tight_flag_qualified {
            let Some(fundamental_row) = fundamental.row(&result.isin) else {
                continue;
            };
            let breakout_text = result
                .breakout_date
                .as_deref()
                .ok_or("Tight Flag result is missing breakout_date")?;
            let breakout = parse_date(breakout_text)?;
            flags.push(Candidate {
                strategy: Strategy::TightFlag,
                exchange: exchange.to_string(),
                symbol: result.symbol.clone(),
                isin: result.isin.clone(),
                breakout,
                days_since_breakout: (latest_session - breakout).num_days(),
                fundamental: fundamental_row.clone(),
                result: CandidateResult::Flag(result.clone()),
            });
        }
        sort_candidates(&mut flags);
        grouped.insert((exchange.to_string(), Strategy::TightFlag), flags);
    }
    Ok(grouped)
}

fn sort_candidates(candidates: &mut [Candidate]) {
    candidates.sort_by(|left, right| {
        left.days_since_breakout
            .cmp(&right.days_since_breakout)
            .then_with(|| right.breakout.cmp(&left.breakout))
            .then_with(|| left.symbol.cmp(&right.symbol))
    });
}

fn output_headers(strategy: Strategy, fundamental: &FundamentalTable) -> Vec<String> {
    let mut headers: Vec<String> = match strategy {
        // Computed columns carry names that cannot collide with Screener.in
        // export columns (it already has "Down from 52w high").
        Strategy::MarkTight => vec![
            "Index",
            "symbol",
            "Days_Since_Breakout",
            "isin",
            "Last_Date",
            "Mark_Tight_Date",
            "EOD_Down_From_52w_High",
            "Exchange",
        ],
        Strategy::TightFlag => vec![
            "Index",
            "symbol",
            "Days_Since_Breakout",
            "isin",
            "Last_Date",
            "is_flag",
            "flag_date",
            "is_tight_flag",
            "tight_flag_date",
            "breakout_date",
            "Exchange",
        ],
    }
    .into_iter()
    .map(str::to_string)
    .collect();
    headers.extend(fundamental.headers.iter().cloned());
    headers
}

fn output_record(index: usize, candidate: &Candidate) -> Vec<String> {
    let mut record = vec![
        index.to_string(),
        candidate.symbol.clone(),
        candidate.days_since_breakout.to_string(),
        candidate.isin.clone(),
    ];
    match &candidate.result {
        CandidateResult::Mark(result) => {
            record.push(result.date.clone());
            record.push(result.mark_tight_date.clone());
            record.push(format_number(result.down_52w_high));
        }
        CandidateResult::Flag(result) => {
            record.push(result.date.clone());
            record.push(if result.is_flag { "True" } else { "False" }.to_string());
            record.push(result.flag_date.clone().unwrap_or_default());
            record.push(
                if result.is_tight_flag {
                    "True"
                } else {
                    "False"
                }
                .to_string(),
            );
            record.push(result.tight_flag_date.clone().unwrap_or_default());
            record.push(result.breakout_date.clone().unwrap_or_default());
        }
    }
    record.push(candidate.exchange.clone());
    record.extend(candidate.fundamental.iter().cloned());
    record
}

fn write_csv(
    path: &Path,
    strategy: Strategy,
    candidates: &[Candidate],
    fundamental: &FundamentalTable,
) -> Result<(), Box<dyn Error>> {
    let mut writer = csv::Writer::from_path(path)?;
    writer.write_record(output_headers(strategy, fundamental))?;
    for (index, candidate) in candidates.iter().enumerate() {
        writer.write_record(output_record(index, candidate))?;
    }
    writer.flush()?;
    Ok(())
}

fn write_watchlist(
    path: &Path,
    candidates: &[Candidate],
    strip_sme_suffix: bool,
) -> Result<(), Box<dyn Error>> {
    let mut seen = HashSet::new();
    let mut text = String::new();
    for candidate in candidates {
        let mut symbol = candidate.symbol.trim().to_uppercase();
        if strip_sme_suffix {
            symbol = symbol.strip_suffix("_SME").unwrap_or(&symbol).to_string();
        }
        if !symbol.is_empty() && seen.insert(symbol.clone()) {
            text.push_str(&symbol);
            text.push('\n');
        }
    }
    fs::write(path, text)?;
    Ok(())
}

fn display_or_na(value: &str) -> &str {
    if value.is_empty() {
        "N/A"
    } else {
        value
    }
}

fn chart_info(fundamental: &FundamentalTable, row: &[String]) -> (String, String, String) {
    let name = fundamental.value(row, "Name");
    let industry = fundamental.value(row, "Industry");
    let current_price = fundamental.value(row, "Current Price");
    let market_cap = fundamental.value(row, "Market Capitalization");
    let pe = display_or_na(fundamental.value(row, "Price to Earning"));
    let roe = display_or_na(fundamental.value(row, "Return on equity"));
    let roce = display_or_na(fundamental.value(row, "Return on capital employed"));
    let opm = display_or_na(fundamental.value(row, "OPM"));
    let pb = display_or_na(fundamental.value(row, "Price to book value"));
    let debt_eq = display_or_na(fundamental.value(row, "Debt to equity"));
    let sales_5y = display_or_na(fundamental.value(row, "Sales growth 5Years"));
    let profit_5y = display_or_na(fundamental.value(row, "Profit growth 5Years"));
    let promoter = display_or_na(fundamental.value(row, "Promoter holding"));
    let fii = display_or_na(fundamental.value(row, "FII holding"));
    let dii = display_or_na(fundamental.value(row, "DII holding"));
    let down_52w = fundamental.value(row, "Down from 52w high");
    let up_52w = fundamental.value(row, "Up from 52w low");
    (
        format!(
            "{name}  |  Industry: {industry}  |  Price: ₹{current_price}  |  MCap: ₹{market_cap} Cr  |  PE: {pe}"
        ),
        format!("ROE: {roe}%  |  ROCE: {roce}%  |  OPM: {opm}%  |  P/B: {pb}  |  Debt/Eq: {debt_eq}"),
        format!(
            "Sales 5Y: {sales_5y}%  |  Profit 5Y: {profit_5y}%  |  Promoter: {promoter}%  |  FII: {fii}%  |  DII: {dii}%  |  52w: -{}% / +{}%",
            if down_52w.is_empty() { "0" } else { down_52w },
            if up_52w.is_empty() { "0" } else { up_52w },
        ),
    )
}

fn prepare_chart_jobs(
    chart_root: &Path,
    grouped: &CandidateGroups,
    fundamental: &FundamentalTable,
) -> Vec<ChartJob> {
    let mut jobs = Vec::new();
    for ((exchange, strategy), candidates) in grouped {
        let output_dir = chart_root.join(exchange).join(strategy.folder());
        for (index, candidate) in candidates.iter().enumerate() {
            // The breakout line comes from the qualifying (tight) pole, so the
            // pole lines must too; flag_* can describe an earlier pole that
            // failed the tight checks.
            let (pattern_start, pattern_end) = match &candidate.result {
                CandidateResult::Mark(_) => (None, None),
                CandidateResult::Flag(result) => (
                    clean_optional_date(&result.tight_start_date),
                    clean_optional_date(&result.tight_peak_date),
                ),
            };
            let (info_line1, info_line2, info_line3) =
                chart_info(fundamental, &candidate.fundamental);
            jobs.push(ChartJob {
                index,
                strategy: candidate.strategy,
                exchange: exchange.clone(),
                symbol: candidate.symbol.clone(),
                breakout: candidate.breakout,
                pattern_start,
                pattern_end,
                info_line1,
                info_line2,
                info_line3,
                output_dir: output_dir.clone(),
            });
        }
    }
    jobs
}

fn reset_chart_directories(chart_root: &Path) -> Result<(), Box<dyn Error>> {
    for exchange in ["NSE", "BSE"] {
        let exchange_root = chart_root.join(exchange);
        for folder in ["mark", "mark_tight", "Tight Flag"] {
            let target = exchange_root.join(folder);
            if target.exists() {
                fs::remove_dir_all(&target)?;
            }
        }
        fs::create_dir_all(exchange_root.join("mark_tight"))?;
        fs::create_dir_all(exchange_root.join("Tight Flag"))?;
    }
    Ok(())
}

/// Chart dates: from a fixed span before the breakout to the symbol's own last
/// candle (not the run date).
fn plot_window(job: &ChartJob, data: &SymbolData) -> Result<(NaiveDate, NaiveDate), String> {
    let end = data
        .dates
        .last()
        .and_then(|value| parse_date(value).ok())
        .ok_or_else(|| format!("No valid last date for {} {}", job.exchange, job.symbol))?;
    Ok((job.breakout - Duration::days(job.strategy.plot_days()), end))
}

fn render_charts(jobs: &[ChartJob], symbol_datasets: &[SymbolData]) -> Result<(), Box<dyn Error>> {
    let data_by_symbol: HashMap<(String, String), &SymbolData> = symbol_datasets
        .iter()
        .map(|data| ((data.exchange.clone(), data.symbol.clone()), data))
        .collect();
    jobs.par_iter().try_for_each(|job| -> Result<(), String> {
        let data = data_by_symbol
            .get(&(job.exchange.clone(), job.symbol.clone()))
            .ok_or_else(|| format!("Missing EOD data for {} {}", job.exchange, job.symbol))?;
        let eod = EodData {
            dates: data.dates.clone(),
            opens: data.opens.clone(),
            highs: data.highs.clone(),
            lows: data.lows.clone(),
            closes: data.closes.clone(),
            volumes: data.volumes.clone(),
        };
        let (plot_start, plot_end) = plot_window(job, data)?;
        let output_path = job
            .output_dir
            .join(format!("{}_{}.png", job.index, job.symbol));
        create_stock_chart(
            &eod,
            &job.symbol,
            &output_path,
            plot_start,
            plot_end,
            job.pattern_start,
            job.pattern_end,
            Some(job.breakout),
            &job.info_line1,
            &job.info_line2,
            &job.info_line3,
        )
        .map_err(|error| format!("Chart failed for {}: {error}", job.symbol))
    })?;
    Ok(())
}

pub fn run_pipeline_cmd(args: &[String]) -> Result<(), Box<dyn Error>> {
    let mut positional: Vec<&String> = Vec::new();
    let mut options = PipelineOptions::default();
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--include-sme" => options.include_sme = true,
            "--only-sme" => options.only_sme = true,
            "--no-watchlists" => options.write_watchlists = false,
            "--strip-sme-suffix" => options.strip_sme_suffix = true,
            "--flag-lookback" => {
                let value = args
                    .get(index + 1)
                    .ok_or("--flag-lookback requires a positive integer")?;
                options.flag_lookback = value.parse()?;
                index += 1;
            }
            "--max-stale-sessions" => {
                let value = args
                    .get(index + 1)
                    .ok_or("--max-stale-sessions requires a non-negative integer")?;
                options.max_stale_sessions = value.parse()?;
                index += 1;
            }
            value if value.starts_with("--") => {
                return Err(format!("unknown pipeline option: {value}").into());
            }
            _ => positional.push(&args[index]),
        }
        index += 1;
    }
    if positional.len() != 6 {
        return Err("Usage: stock_analysis pipeline <duckdb_file> <fundamental_csv> <output_root> <chart_root> <timestamp> <opentrader_root> [--include-sme|--only-sme] [--flag-lookback N] [--max-stale-sessions N] [--no-watchlists] [--strip-sme-suffix]".into());
    }

    let duckdb_file = Path::new(positional[0]);
    let fundamental_csv = Path::new(positional[1]);
    let output_root = Path::new(positional[2]);
    let chart_root = Path::new(positional[3]);
    let timestamp = positional[4];
    let opentrader_root = Path::new(positional[5]);
    let run_date = run_date_from_timestamp(timestamp)?;
    let total_started = std::time::Instant::now();

    println!("Loading fundamental data in Rust...");
    let fundamental = FundamentalTable::load(fundamental_csv)?;
    println!(
        "Loaded {} fundamental rows; starting one-pass market screening...",
        fundamental.rows_by_isin.len()
    );
    let screen_options = ScreenOptions {
        include_sme: options.include_sme,
        only_sme: options.only_sme,
        flag_lookback: options.flag_lookback,
        max_stale_sessions: options.max_stale_sessions,
        ..ScreenOptions::default()
    };
    let run = screener::screen_market(
        duckdb_file
            .to_str()
            .ok_or("DuckDB path is not valid UTF-8")?,
        fundamental_csv
            .to_str()
            .ok_or("fundamental CSV path is not valid UTF-8")?,
        "ALL",
        &screen_options,
    )?;
    let grouped = collect_candidates(run_date, &fundamental, run.datasets)?;

    let output_started = std::time::Instant::now();
    for exchange in ["NSE", "BSE"] {
        for strategy in [Strategy::MarkTight, Strategy::TightFlag] {
            let candidates = grouped
                .get(&(exchange.to_string(), strategy))
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let suffix = strategy.suffix(exchange);
            let output_dir = output_root.join(exchange).join(strategy.folder());
            fs::create_dir_all(&output_dir)?;
            let csv_path = output_dir.join(format!("{timestamp}_{suffix}_qualified.csv"));
            write_csv(&csv_path, strategy, candidates, &fundamental)?;
            println!(
                "   {} {}: {} stocks -> {}",
                exchange,
                strategy.folder(),
                candidates.len(),
                csv_path.display()
            );

            if options.write_watchlists {
                let watchlist_dir = opentrader_root
                    .join("databases")
                    .join(format!("{exchange}_EOD"))
                    .join("watchlists");
                fs::create_dir_all(&watchlist_dir)?;
                let watchlist_path = watchlist_dir.join(format!("{timestamp}_{suffix}.txt"));
                write_watchlist(&watchlist_path, candidates, options.strip_sme_suffix)?;
            }
        }
    }
    println!(
        "⏱️ Rust CSV/watchlist output: {:.3}s",
        output_started.elapsed().as_secs_f64()
    );

    reset_chart_directories(chart_root)?;
    let jobs = prepare_chart_jobs(chart_root, &grouped, &fundamental);
    let chart_started = std::time::Instant::now();
    render_charts(&jobs, &run.symbol_datasets)?;
    println!(
        "⏱️ Rust charts: {:.2}s ({} images)",
        chart_started.elapsed().as_secs_f64(),
        jobs.len()
    );
    println!(
        "🚀 TOTAL LOCAL RUST PIPELINE: {:.2}s",
        total_started.elapsed().as_secs_f64()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        collect_candidates, output_headers, plot_window, prepare_chart_jobs,
        run_date_from_timestamp, sort_candidates, write_watchlist, Candidate, CandidateGroups,
        CandidateResult, FundamentalTable, Strategy,
    };
    use crate::screener::{FlagResult, MarkResult, ScreenedDataset, SymbolData};
    use chrono::NaiveDate;
    use std::collections::HashMap;
    use std::fs;

    fn mark(symbol: &str, date: &str) -> Candidate {
        let breakout = NaiveDate::parse_from_str(date, "%Y-%m-%d").unwrap();
        Candidate {
            strategy: Strategy::MarkTight,
            exchange: "NSE".to_string(),
            symbol: symbol.to_string(),
            isin: format!("ISIN-{symbol}"),
            breakout,
            days_since_breakout: (NaiveDate::from_ymd_opt(2026, 7, 30).unwrap() - breakout)
                .num_days(),
            fundamental: Vec::new(),
            result: CandidateResult::Mark(MarkResult {
                symbol: symbol.to_string(),
                isin: format!("ISIN-{symbol}"),
                date: "2026-07-30".to_string(),
                close: 100.0,
                ema_50: 90.0,
                ema_150: 80.0,
                ema_200: 70.0,
                high_52w: 110.0,
                low_52w: 50.0,
                mark_tight_date: date.to_string(),
                down_52w_high: 9.09,
            }),
        }
    }

    #[test]
    fn timestamp_date_is_the_fallback_recency_anchor() {
        assert_eq!(
            run_date_from_timestamp("20260730_120000").unwrap(),
            NaiveDate::from_ymd_opt(2026, 7, 30).unwrap()
        );
    }

    fn dataset_with_latest_session(latest: Option<&str>) -> HashMap<String, ScreenedDataset> {
        let Candidate {
            result: CandidateResult::Mark(result),
            ..
        } = mark("FRIDAY", "2026-10-05")
        else {
            unreachable!()
        };
        let dataset = ScreenedDataset {
            exchange: "NSE".to_string(),
            fundamental_isins: 1,
            selected_symbols: 1,
            history_ready: 1,
            max_history: 300,
            latest_session: latest.map(str::to_string),
            stale_skipped: 0,
            mark_tight_qualified: vec![result],
            tight_flag_qualified: Vec::new(),
        };
        HashMap::from([("NSE".to_string(), dataset)])
    }

    #[test]
    fn days_since_breakout_counts_from_the_exchange_latest_session() {
        let fundamentals = FundamentalTable {
            headers: vec!["ISIN Code".to_string()],
            header_index: HashMap::from([("ISIN Code".to_string(), 0)]),
            rows_by_isin: HashMap::from([(
                "ISIN-FRIDAY".to_string(),
                vec!["ISIN-FRIDAY".to_string()],
            )]),
        };
        let days = |run_date: NaiveDate, latest: Option<&str>| {
            let grouped =
                collect_candidates(run_date, &fundamentals, dataset_with_latest_session(latest))
                    .unwrap();
            grouped[&("NSE".to_string(), Strategy::MarkTight)][0].days_since_breakout
        };
        let friday = NaiveDate::from_ymd_opt(2026, 10, 9).unwrap();
        let sunday = NaiveDate::from_ymd_opt(2026, 10, 11).unwrap();

        // Breakout Monday 2026-10-05, last session Friday 2026-10-09.
        assert_eq!(days(friday, Some("2026-10-09")), 4);
        assert_eq!(days(sunday, Some("2026-10-09")), 4);
        // Only an exchange without any session falls back to the run date.
        assert_eq!(days(sunday, None), 6);
    }

    #[test]
    fn newest_breakout_receives_the_first_index() {
        let mut rows = vec![
            mark("OLDER", "2026-07-20"),
            mark("LATEST", "2026-07-29"),
            mark("SAME-B", "2026-07-28"),
            mark("SAME-A", "2026-07-28"),
        ];
        sort_candidates(&mut rows);
        let symbols: Vec<&str> = rows.iter().map(|row| row.symbol.as_str()).collect();
        assert_eq!(symbols, ["LATEST", "SAME-A", "SAME-B", "OLDER"]);
    }

    #[test]
    fn csv_starts_with_breakout_recency_columns() {
        let fundamentals = FundamentalTable {
            headers: vec![
                "ISIN Code".to_string(),
                "Name".to_string(),
                "Down from 52w high".to_string(),
            ],
            header_index: HashMap::new(),
            rows_by_isin: HashMap::new(),
        };
        for strategy in [Strategy::MarkTight, Strategy::TightFlag] {
            let headers = output_headers(strategy, &fundamentals);
            let unique: std::collections::HashSet<&String> = headers.iter().collect();
            assert_eq!(unique.len(), headers.len(), "duplicate CSV header");
            assert_eq!(headers[4], "Last_Date");
        }
        assert_eq!(
            &output_headers(Strategy::MarkTight, &fundamentals)[..4],
            ["Index", "symbol", "Days_Since_Breakout", "isin"]
        );
        assert_eq!(
            &output_headers(Strategy::TightFlag, &fundamentals)[..4],
            ["Index", "symbol", "Days_Since_Breakout", "isin"]
        );
    }

    #[test]
    fn watchlist_deduplicates_and_can_strip_sme_suffix() {
        let path = std::env::temp_dir().join(format!(
            "mark-rust-watchlist-test-{}.txt",
            std::process::id()
        ));
        let rows = vec![
            mark("ABC_SME", "2026-07-29"),
            mark("ABC_SME", "2026-07-28"),
            mark("XYZ", "2026-07-27"),
        ];
        write_watchlist(&path, &rows, true).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "ABC\nXYZ\n");
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn tight_flag_chart_marks_the_qualifying_pole() {
        let breakout = NaiveDate::from_ymd_opt(2026, 7, 20).unwrap();
        let candidate = Candidate {
            strategy: Strategy::TightFlag,
            exchange: "NSE".to_string(),
            symbol: "FLAGCO".to_string(),
            isin: "INE000F01010".to_string(),
            breakout,
            days_since_breakout: 10,
            fundamental: Vec::new(),
            result: CandidateResult::Flag(FlagResult {
                symbol: "FLAGCO".to_string(),
                isin: "INE000F01010".to_string(),
                date: "2026-07-30".to_string(),
                close: 350.0,
                is_flag: true,
                flag_date: Some("2026-05-20".to_string()),
                flag_start_date: Some("2026-05-01".to_string()),
                flag_peak_date: Some("2026-05-20".to_string()),
                is_tight_flag: true,
                tight_flag_date: Some("2026-07-10".to_string()),
                tight_start_date: Some("2026-06-15".to_string()),
                tight_peak_date: Some("2026-07-10".to_string()),
                breakout_date: Some("2026-07-20".to_string()),
            }),
        };
        let mut grouped = CandidateGroups::new();
        grouped.insert(("NSE".to_string(), Strategy::TightFlag), vec![candidate]);
        let fundamentals = FundamentalTable {
            headers: Vec::new(),
            header_index: HashMap::new(),
            rows_by_isin: HashMap::new(),
        };

        let jobs = prepare_chart_jobs(std::path::Path::new("charts"), &grouped, &fundamentals);

        assert_eq!(jobs[0].pattern_start, NaiveDate::from_ymd_opt(2026, 6, 15));
        assert_eq!(jobs[0].pattern_end, NaiveDate::from_ymd_opt(2026, 7, 10));
        assert_eq!(jobs[0].breakout, breakout);

        // The chart ends on the symbol's last candle (a Friday), whatever the
        // run date, and starts 200 days before the breakout.
        let data = SymbolData {
            exchange: "NSE".to_string(),
            symbol: "FLAGCO".to_string(),
            isin: "INE000F01010".to_string(),
            dates: vec!["2026-07-29".to_string(), "2026-07-31".to_string()],
            opens: vec![1.0; 2],
            highs: vec![1.0; 2],
            lows: vec![1.0; 2],
            closes: vec![1.0; 2],
            volumes: vec![1.0; 2],
        };
        assert_eq!(
            plot_window(&jobs[0], &data).unwrap(),
            (
                NaiveDate::from_ymd_opt(2026, 1, 1).unwrap(),
                NaiveDate::from_ymd_opt(2026, 7, 31).unwrap()
            )
        );
    }
}
