use duckdb::{AccessMode, Config, Connection};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::File;
use std::io::BufReader;
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MarkResult {
    pub symbol: String,
    pub isin: String,
    pub date: String,
    pub close: f64,
    pub ema_50: f64,
    pub ema_150: f64,
    pub ema_200: f64,
    pub high_52w: f64,
    pub low_52w: f64,
    pub mark_tight_date: String,
    pub down_52w_high: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FlagResult {
    pub symbol: String,
    pub isin: String,
    pub date: String,
    pub close: f64,
    pub is_flag: bool,
    pub flag_date: Option<String>,
    pub flag_start_date: Option<String>,
    pub flag_peak_date: Option<String>,
    pub is_tight_flag: bool,
    pub tight_flag_date: Option<String>,
    pub tight_start_date: Option<String>,
    pub tight_peak_date: Option<String>,
    pub breakout_date: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScreenedDataset {
    pub exchange: String,
    pub fundamental_isins: usize,
    pub selected_symbols: usize,
    pub history_ready: usize,
    pub max_history: usize,
    pub mark_tight_qualified: Vec<MarkResult>,
    pub tight_flag_qualified: Vec<FlagResult>,
}

pub struct SymbolData {
    pub exchange: String,
    pub symbol: String,
    pub isin: String,
    pub dates: Vec<String>,
    pub opens: Vec<f64>,
    pub highs: Vec<f64>,
    pub lows: Vec<f64>,
    pub closes: Vec<f64>,
}

struct EmaAccumulator {
    period: usize,
    multiplier: f64,
    sum: f64,
    previous: f64,
    values: Vec<f64>,
}

impl EmaAccumulator {
    fn new(period: usize, capacity: usize) -> Self {
        Self {
            period,
            multiplier: 2.0 / (period as f64 + 1.0),
            sum: 0.0,
            previous: f64::NAN,
            values: vec![f64::NAN; capacity],
        }
    }

    fn push(&mut self, index: usize, close: f64) {
        if index < self.period {
            self.sum += close;
        }
        if index + 1 == self.period {
            self.previous = self.sum / self.period as f64;
            self.values[index] = (self.previous * 100.0).round() / 100.0;
        } else if index + 1 > self.period {
            self.previous = close * self.multiplier + self.previous * (1.0 - self.multiplier);
            self.values[index] = (self.previous * 100.0).round() / 100.0;
        }
    }
}

struct MarkIndicators {
    ema_50: Vec<f64>,
    ema_150: Vec<f64>,
    ema_200: Vec<f64>,
    high_52w: Vec<f64>,
    low_52w: Vec<f64>,
    donchian_60: Vec<f64>,
}

fn calculate_mark_indicators(data: &SymbolData) -> MarkIndicators {
    let n = data.closes.len();
    let mut ema_50 = EmaAccumulator::new(50, n);
    let mut ema_150 = EmaAccumulator::new(150, n);
    let mut ema_200 = EmaAccumulator::new(200, n);
    for (index, close) in data.closes.iter().copied().enumerate() {
        ema_50.push(index, close);
        ema_150.push(index, close);
        ema_200.push(index, close);
    }

    let mut high_52w = vec![f64::NAN; n];
    let mut low_52w = vec![f64::NAN; n];
    let mut donchian_60 = vec![f64::NAN; n];
    let mut high_252_queue: VecDeque<usize> = VecDeque::new();
    let mut low_252_queue: VecDeque<usize> = VecDeque::new();
    let mut high_60_queue: VecDeque<usize> = VecDeque::new();

    for i in 0..n {
        if i >= 60 {
            let start = i - 60;
            while high_60_queue.front().is_some_and(|&index| index < start) {
                high_60_queue.pop_front();
            }
            if let Some(&index) = high_60_queue.front() {
                donchian_60[i] = data.highs[index];
            }
        }
        while high_60_queue
            .back()
            .is_some_and(|&index| data.highs[index] <= data.highs[i])
        {
            high_60_queue.pop_back();
        }
        high_60_queue.push_back(i);

        let start_252 = (i + 1).saturating_sub(252);
        while high_252_queue
            .front()
            .is_some_and(|&index| index < start_252)
        {
            high_252_queue.pop_front();
        }
        while low_252_queue
            .front()
            .is_some_and(|&index| index < start_252)
        {
            low_252_queue.pop_front();
        }
        while high_252_queue
            .back()
            .is_some_and(|&index| data.highs[index] <= data.highs[i])
        {
            high_252_queue.pop_back();
        }
        while low_252_queue
            .back()
            .is_some_and(|&index| data.lows[index] >= data.lows[i])
        {
            low_252_queue.pop_back();
        }
        high_252_queue.push_back(i);
        low_252_queue.push_back(i);

        if i + 1 >= 252 {
            high_52w[i] = data.highs[*high_252_queue.front().unwrap()];
            low_52w[i] = data.lows[*low_252_queue.front().unwrap()];
        }
    }

    MarkIndicators {
        ema_50: ema_50.values,
        ema_150: ema_150.values,
        ema_200: ema_200.values,
        high_52w,
        low_52w,
        donchian_60,
    }
}

fn mark_condition(data: &SymbolData, indicators: &MarkIndicators, index: usize) -> bool {
    let close = data.closes[index];
    let ema_50 = indicators.ema_50[index];
    let ema_150 = indicators.ema_150[index];
    let ema_200 = indicators.ema_200[index];
    let high_52w = indicators.high_52w[index];
    let low_52w = indicators.low_52w[index];
    [ema_50, ema_150, ema_200, high_52w, low_52w]
        .iter()
        .all(|value| value.is_finite())
        && close > ema_50
        && ema_50 > ema_150
        && ema_150 > ema_200
        && close >= high_52w * 0.80
        && close >= low_52w * 1.50
}

/// Mark Tight is the first 60-session Donchian breakout inside the current
/// uninterrupted Mark trend. The latest candle must still satisfy Mark and
/// hold above the breakout level.
fn process_mark_screener(data: &SymbolData, indicators: &MarkIndicators) -> Option<MarkResult> {
    let n = data.closes.len();
    if n < 252 || !mark_condition(data, indicators, n - 1) {
        return None;
    }

    let mut mark_start = n - 1;
    while mark_start > 0 && mark_condition(data, indicators, mark_start - 1) {
        mark_start -= 1;
    }

    let (breakout_index, breakout_level) = (mark_start..n).find_map(|index| {
        let level = indicators.donchian_60[index];
        (level.is_finite() && data.closes[index] > level).then_some((index, level))
    })?;
    if data.closes[n - 1] <= breakout_level {
        return None;
    }

    let down_52w_high = (((indicators.high_52w[n - 1] - data.closes[n - 1])
        / indicators.high_52w[n - 1])
        * 100.0
        * 100.0)
        .round()
        / 100.0;

    Some(MarkResult {
        symbol: data.symbol.clone(),
        isin: data.isin.clone(),
        date: data.dates[n - 1].clone(),
        close: data.closes[n - 1],
        ema_50: indicators.ema_50[n - 1],
        ema_150: indicators.ema_150[n - 1],
        ema_200: indicators.ema_200[n - 1],
        high_52w: indicators.high_52w[n - 1],
        low_52w: indicators.low_52w[n - 1],
        mark_tight_date: data.dates[breakout_index].clone(),
        down_52w_high,
    })
}

/// Stage 1 ("Flag") -> Stage 2 ("Tight Flag") Hierarchical Screening
fn process_flag_screener(
    data: &SymbolData,
    flag_lookback: usize,
    shared_indicators: Option<&MarkIndicators>,
) -> Option<FlagResult> {
    let n_total = data.closes.len();
    if n_total < 252 {
        return None;
    }

    let tail_len = std::cmp::min(flag_lookback, n_total);
    let start_offset = n_total - tail_len;

    let opens = &data.opens[start_offset..n_total];
    let highs = &data.highs[start_offset..n_total];
    let lows = &data.lows[start_offset..n_total];
    let closes = &data.closes[start_offset..n_total];
    let dates = &data.dates[start_offset..n_total];
    let n = tail_len;

    let mut is_flag = false;
    let mut is_tight_flag = false;

    let mut flag_date: Option<String> = None;
    let mut flag_start_date: Option<String> = None;
    let mut flag_peak_date: Option<String> = None;

    let mut tight_flag_date: Option<String> = None;
    let mut tight_start_date: Option<String> = None;
    let mut tight_peak_date: Option<String> = None;
    let mut breakout_date: Option<String> = None;

    let last_close = data.closes[n_total - 1];
    if last_close < 5.0 {
        return None;
    }
    let (last_ema200, high_52w) = if let Some(indicators) = shared_indicators {
        (
            indicators.ema_200[n_total - 1],
            indicators.high_52w[n_total - 1],
        )
    } else {
        let mut ema_200 = EmaAccumulator::new(200, n_total);
        for (index, close) in data.closes.iter().copied().enumerate() {
            ema_200.push(index, close);
        }
        let high = data.highs[n_total - 252..]
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        (ema_200.values[n_total - 1], high)
    };
    let dist_52w = if high_52w > 0.0 {
        (high_52w - last_close) / high_52w * 100.0
    } else {
        f64::INFINITY
    };
    if last_ema200.is_nan() || last_close < last_ema200 || dist_52w > 45.0 {
        return None;
    }

    let mut gap_prefix = vec![0_usize; n + 1];
    let mut circuit_prefix = vec![0_usize; n + 1];
    for index in 0..n {
        let extreme_gap = index > 0
            && closes[index - 1] > 0.0
            && (opens[index] - closes[index - 1]).abs() / closes[index - 1] > 0.50;
        gap_prefix[index + 1] = gap_prefix[index] + usize::from(extreme_gap);
        circuit_prefix[index + 1] =
            circuit_prefix[index] + usize::from((highs[index] - lows[index]).abs() < 1e-4);
    }

    // --- STAGE 1: Flag Pattern Detection (90%+ gain in 45 days) ---
    for i in 0..(n - 1) {
        if lows[i] <= 0.0 {
            continue;
        }
        let max_j = std::cmp::min(n - 1, i + 45);
        if max_j <= i {
            continue;
        }

        // Find peak high index in range (i + 1)..=max_j
        let mut j_peak = i + 1;
        let mut max_h = highs[j_peak];
        for candidate_j in (i + 1)..=max_j {
            if highs[candidate_j] > max_h {
                max_h = highs[candidate_j];
                j_peak = candidate_j;
            }
        }

        let run_up = (highs[j_peak] - lows[i]) / lows[i];
        if run_up >= 0.90 {
            if !is_flag {
                is_flag = true;
                flag_date = Some(dates[j_peak].clone());
                flag_start_date = Some(dates[i].clone());
                flag_peak_date = Some(dates[j_peak].clone());
            }

            // --- STAGE 2: Tight Flag Checks ---
            // 1. Minimum Pole Duration > 3 candles
            if j_peak - i > 3 {
                // 2. Extreme Gap Check (No single-day gap > 50% in pole)
                let has_extreme_gap = gap_prefix[j_peak + 1] > gap_prefix[i + 1];

                if !has_extreme_gap {
                    // 3. Circuit Trap Check (Circuit candles <= 40% in pole)
                    let pole_candles = j_peak - i + 1;
                    let circuit_count = circuit_prefix[j_peak + 1] - circuit_prefix[i];
                    let circuit_pct = (circuit_count as f64) / (pole_candles as f64) * 100.0;

                    if circuit_pct <= 40.0 {
                        // 4. Tight Breakout Check (within 15 days of peak)
                        let mut breakout_k: Option<usize> = None;
                        let max_breakout_k = std::cmp::min(n, j_peak + 16);
                        for k in (j_peak + 1)..max_breakout_k {
                            if closes[k] > highs[j_peak] {
                                breakout_k = Some(k);
                                break;
                            }
                        }

                        if let Some(k_idx) = breakout_k {
                            // 5. Breakout Holding Rule: Latest close > Pole Peak High
                            if closes[n - 1] > highs[j_peak] {
                                if !is_tight_flag {
                                    is_tight_flag = true;
                                    tight_flag_date = Some(dates[j_peak].clone());
                                    tight_start_date = Some(dates[i].clone());
                                    tight_peak_date = Some(dates[j_peak].clone());
                                    breakout_date = Some(dates[k_idx].clone());
                                }
                            }
                        }
                    }
                }
            }

            if is_flag && is_tight_flag {
                break;
            }
        }
    }

    // Flag is only an internal 90%-in-45-days candidate gate. Returning Tight
    // Flag matches only avoids carrying rejected flag-only candidates.
    if !is_tight_flag {
        return None;
    }

    Some(FlagResult {
        symbol: data.symbol.clone(),
        isin: data.isin.clone(),
        date: dates[n - 1].clone(),
        close: closes[n - 1],
        is_flag,
        flag_date,
        flag_start_date,
        flag_peak_date,
        is_tight_flag,
        tight_flag_date,
        tight_start_date,
        tight_peak_date,
        breakout_date,
    })
}

fn load_valid_isins(fundamental_csv: &str) -> Result<HashSet<String>, Box<dyn std::error::Error>> {
    let mut valid_isins = HashSet::new();
    let file = File::open(fundamental_csv)?;
    let mut rdr = csv::Reader::from_reader(BufReader::new(file));
    let headers = rdr.headers()?.clone();

    let isin_idx = headers
        .iter()
        .position(|h| h.trim() == "ISIN Code")
        .ok_or_else(|| "ISIN Code column missing in fundamental CSV")?;

    for result in rdr.records() {
        let record = result?;
        if let Some(isin) = record.get(isin_idx) {
            let trimmed = isin.trim().to_uppercase();
            if !trimmed.is_empty() && trimmed != "NAN" {
                valid_isins.insert(trimmed);
            }
        }
    }
    Ok(valid_isins)
}

pub fn run_screener_cmd(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let mut positional: Vec<&String> = Vec::new();
    let mut include_sme = false;
    let mut only_sme = false;
    let mut flag_lookback = 90_usize;
    let mut mark_tight_only = false;
    let mut tight_flag_only = false;
    let mut index = 0;

    while index < args.len() {
        match args[index].as_str() {
            "--include-sme" => include_sme = true,
            "--only-sme" => only_sme = true,
            "--mark-tight-only" => mark_tight_only = true,
            "--tight-flag-only" => tight_flag_only = true,
            "--flag-lookback" => {
                let value = args.get(index + 1).ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "--flag-lookback requires a positive integer",
                    )
                })?;
                flag_lookback = value.parse::<usize>().map_err(|_| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "--flag-lookback requires a positive integer",
                    )
                })?;
                if flag_lookback == 0 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "--flag-lookback must be greater than zero",
                    )
                    .into());
                }
                index += 1;
            }
            value if value.starts_with("--") => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    format!("unknown screen option: {value}"),
                )
                .into());
            }
            _ => positional.push(&args[index]),
        }
        index += 1;
    }

    if positional.len() < 4 {
        eprintln!("Usage: stock_analysis screen <duckdb_file> <fundamental_csv> <NSE|BSE|ALL> <out_dir> [max_date] [--mark-tight-only|--tight-flag-only] [--include-sme|--only-sme] [--flag-lookback N]");
        std::process::exit(1);
    }
    if mark_tight_only && tight_flag_only {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "--mark-tight-only and --tight-flag-only cannot be combined",
        )
        .into());
    }

    let duckdb_file = positional[0];
    let fundamental_csv = positional[1];
    let exchange_scope = positional[2].to_uppercase();
    if !matches!(exchange_scope.as_str(), "NSE" | "BSE" | "ALL") {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "exchange must be NSE, BSE, or ALL",
        )
        .into());
    }
    let out_dir = Path::new(positional[3]);
    let max_date = positional
        .get(4)
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .map(str::to_string);

    std::fs::create_dir_all(out_dir)?;
    let total_started = std::time::Instant::now();
    let fetch_started = std::time::Instant::now();
    let valid_isins = load_valid_isins(fundamental_csv)?;

    let conn = Connection::open_with_flags(
        duckdb_file,
        Config::default().access_mode(AccessMode::ReadOnly)?,
    )?;
    let exchange_predicate = match exchange_scope.as_str() {
        "NSE" => "e.Exchange = 'NSE'",
        "BSE" => {
            "e.Exchange = 'BSE' AND NOT EXISTS (
            SELECT 1 FROM nse_isins n
            WHERE n.isin = TRIM(UPPER(e.ISIN))
        )"
        }
        _ => {
            "(e.Exchange = 'NSE' OR (
            e.Exchange = 'BSE' AND NOT EXISTS (
                SELECT 1 FROM nse_isins n
                WHERE n.isin = TRIM(UPPER(e.ISIN))
            )
        ))"
        }
    };
    let escaped_fundamental_csv = fundamental_csv.replace('\'', "''");
    let bulk_eod_sql = format!(
        "WITH valid_isins AS (
             SELECT DISTINCT TRIM(UPPER(\"ISIN Code\")) AS isin
             FROM read_csv_auto('{escaped_fundamental_csv}', header=true, all_varchar=true)
             WHERE NULLIF(TRIM(\"ISIN Code\"), '') IS NOT NULL
         ),
         nse_isins AS (
             SELECT DISTINCT TRIM(UPPER(ISIN)) AS isin
             FROM eod_data
             WHERE Exchange = 'NSE'
         )
         SELECT e.Exchange, CAST(e.Date AS VARCHAR), e.Symbol, e.ISIN, \
         e.Open, e.High, e.Low, e.Close \
         FROM eod_data e
         INNER JOIN valid_isins f ON f.isin = TRIM(UPPER(e.ISIN))
         WHERE {exchange_predicate} \
         ORDER BY e.Exchange, e.Symbol, e.Date ASC"
    );
    let mut stmt = conn.prepare(&bulk_eod_sql)?;
    let mut rows = stmt.query([])?;

    let mut symbol_datasets: Vec<SymbolData> = Vec::new();
    let mut current_exchange = String::new();
    let mut current_symbol = String::new();
    let mut current_isin = String::new();
    let mut dates = Vec::new();
    let mut opens = Vec::new();
    let mut highs = Vec::new();
    let mut lows = Vec::new();
    let mut closes = Vec::new();

    let push_current = |target: &mut Vec<SymbolData>,
                        exchange: String,
                        symbol: String,
                        isin: String,
                        dates: Vec<String>,
                        opens: Vec<f64>,
                        highs: Vec<f64>,
                        lows: Vec<f64>,
                        closes: Vec<f64>| {
        if !symbol.is_empty() && !dates.is_empty() {
            target.push(SymbolData {
                exchange,
                symbol,
                isin,
                dates,
                opens,
                highs,
                lows,
                closes,
            });
        }
    };

    while let Some(row) = rows.next()? {
        let exchange: String = row.get(0)?;
        let date_raw: String = row.get(1)?;
        let symbol: String = row.get(2)?;
        let isin: String = row.get(3)?;
        let isin_clean = isin.trim().to_uppercase();
        let is_sme = symbol.trim().to_uppercase().ends_with("_SME");
        if (only_sme && !is_sme) || (!only_sme && !include_sme && is_sme) {
            continue;
        }
        let clean_date = date_raw.split('T').next().unwrap_or(&date_raw).to_string();
        if max_date.as_ref().is_some_and(|limit| clean_date > *limit) {
            continue;
        }

        if exchange != current_exchange || symbol != current_symbol {
            push_current(
                &mut symbol_datasets,
                current_exchange,
                current_symbol,
                current_isin,
                dates,
                opens,
                highs,
                lows,
                closes,
            );
            current_exchange = exchange;
            current_symbol = symbol;
            current_isin = isin_clean;
            dates = Vec::new();
            opens = Vec::new();
            highs = Vec::new();
            lows = Vec::new();
            closes = Vec::new();
        }

        dates.push(clean_date);
        opens.push(row.get(4)?);
        highs.push(row.get(5)?);
        lows.push(row.get(6)?);
        closes.push(row.get(7)?);
    }
    push_current(
        &mut symbol_datasets,
        current_exchange,
        current_symbol,
        current_isin,
        dates,
        opens,
        highs,
        lows,
        closes,
    );
    drop(rows);
    drop(stmt);
    drop(conn);

    println!(
        "⏱️ [1/3] One-pass DuckDB fetch: {:.2}s ({} eligible symbols)",
        fetch_started.elapsed().as_secs_f64(),
        symbol_datasets.len()
    );

    let screening_started = std::time::Instant::now();
    let screened: Vec<(String, Option<MarkResult>, Option<FlagResult>)> = symbol_datasets
        .par_iter()
        .map(|data| {
            if data.closes.len() < 252 {
                return (data.exchange.clone(), None, None);
            }
            let indicators = (!tight_flag_only).then(|| calculate_mark_indicators(data));
            let mark = indicators
                .as_ref()
                .and_then(|values| process_mark_screener(data, values));
            let flag = if mark_tight_only {
                None
            } else {
                process_flag_screener(data, flag_lookback, indicators.as_ref())
            };
            (data.exchange.clone(), mark, flag)
        })
        .collect();

    let target_exchanges: &[&str] = if exchange_scope == "ALL" {
        &["NSE", "BSE"]
    } else if exchange_scope == "NSE" {
        &["NSE"]
    } else {
        &["BSE"]
    };
    let mut results: HashMap<String, (Vec<MarkResult>, Vec<FlagResult>)> = target_exchanges
        .iter()
        .map(|exchange| (exchange.to_string(), (Vec::new(), Vec::new())))
        .collect();
    for (exchange, mark, flag) in screened {
        if let Some((marks, flags)) = results.get_mut(&exchange) {
            if let Some(value) = mark {
                marks.push(value);
            }
            if let Some(value) = flag {
                flags.push(value);
            }
        }
    }
    println!(
        "⏱️ [2/3] Shared parallel TA screening: {:.4}s",
        screening_started.elapsed().as_secs_f64()
    );

    let export_started = std::time::Instant::now();
    for exchange in target_exchanges {
        let selected: Vec<&SymbolData> = symbol_datasets
            .iter()
            .filter(|data| data.exchange == *exchange)
            .collect();
        let (mark_tight_qualified, tight_flag_qualified) =
            results.remove(*exchange).unwrap_or_default();
        let dataset = ScreenedDataset {
            exchange: exchange.to_string(),
            fundamental_isins: valid_isins.len(),
            selected_symbols: selected.len(),
            history_ready: selected
                .iter()
                .filter(|data| data.closes.len() >= 252)
                .count(),
            max_history: selected
                .iter()
                .map(|data| data.closes.len())
                .max()
                .unwrap_or(0),
            mark_tight_qualified,
            tight_flag_qualified,
        };
        let exchange_out_dir = if exchange_scope == "ALL" {
            out_dir.join(exchange)
        } else {
            out_dir.to_path_buf()
        };
        std::fs::create_dir_all(&exchange_out_dir)?;
        let out_file = exchange_out_dir.join(format!("{}_rust_screened.json", exchange));
        let writer = std::io::BufWriter::new(File::create(&out_file)?);
        serde_json::to_writer(writer, &dataset)?;
        println!(
            "   {}: {} Mark Tight | {} Tight Flag -> {}",
            exchange,
            dataset.mark_tight_qualified.len(),
            dataset.tight_flag_qualified.len(),
            out_file.display()
        );
    }
    println!(
        "⏱️ [3/3] Candidate JSON export: {:.3}s",
        export_started.elapsed().as_secs_f64()
    );
    println!(
        "🚀 TOTAL RUST SCREENING TIME: {:.2}s",
        total_started.elapsed().as_secs_f64()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{calculate_mark_indicators, process_mark_screener, SymbolData};

    fn symbol_data(closes: Vec<f64>, highs: Vec<f64>) -> SymbolData {
        let len = closes.len();
        SymbolData {
            exchange: "NSE".to_string(),
            symbol: "TEST".to_string(),
            isin: "INE000TEST".to_string(),
            dates: (0..len).map(|i| format!("2024-{:03}", i)).collect(),
            opens: closes.clone(),
            highs,
            lows: vec![100.0; len],
            closes,
        }
    }

    #[test]
    fn records_the_first_real_donchian_breakout_in_the_active_mark_run() {
        let mut closes = vec![100.0; 270];
        let mut highs = vec![100.0; 270];
        for i in 251..270 {
            closes[i] = 200.0;
            highs[i] = 200.0;
        }
        highs[252] = 220.0;

        let data = symbol_data(closes, highs);
        let indicators = calculate_mark_indicators(&data);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-251");
    }

    #[test]
    fn requires_the_latest_close_to_hold_the_breakout_level() {
        let mut closes = vec![100.0; 270];
        let mut highs = vec![100.0; 270];
        for i in 191..251 {
            highs[i] = 300.0;
        }
        closes[251] = 250.0;
        highs[251] = 250.0;
        closes[252] = 310.0;
        highs[252] = 310.0;
        for i in 253..270 {
            closes[i] = 250.0;
            highs[i] = 260.0;
        }

        let data = symbol_data(closes, highs);
        let indicators = calculate_mark_indicators(&data);

        assert!(process_mark_screener(&data, &indicators).is_none());
    }
}
