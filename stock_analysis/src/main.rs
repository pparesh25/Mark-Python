mod chart;
#[cfg(feature = "gui")]
mod gui;
mod pipeline;
mod screener;

use chrono::NaiveDate;
use rayon::prelude::*;
use serde::Deserialize;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

#[derive(Debug, Deserialize)]
struct ChartTask {
    #[serde(rename = "Index")]
    index: String,
    #[serde(rename = "symbol")]
    symbol: String,
    #[serde(rename = "Plot Start")]
    plot_start: String,
    #[serde(rename = "Plot End")]
    plot_end: String,
    #[serde(default)]
    #[serde(rename = "Chart Output")]
    chart_output: String,

    // Pattern dates (optional)
    #[serde(rename = "flag_start_date")]
    flag_start_date: Option<String>,
    #[serde(rename = "flag_peak_date")]
    flag_peak_date: Option<String>,
    #[serde(rename = "tight_start_date")]
    tight_start_date: Option<String>,
    #[serde(rename = "tight_peak_date")]
    tight_peak_date: Option<String>,
    #[serde(rename = "breakout_date")]
    breakout_date: Option<String>,

    // Screener dates (optional)
    #[serde(rename = "mark_tight_date", alias = "Mark_Tight_Date")]
    mark_tight_date: Option<String>,

    // Exchange (optional, default to NSE)
    #[serde(default)]
    #[serde(rename = "Exchange")]
    exchange_cap: String,
    #[serde(default)]
    #[serde(rename = "exchange")]
    exchange_low: String,

    // Fundamentals (optional/empty strings)
    #[serde(default)]
    #[serde(rename = "Name")]
    name: String,
    #[serde(default)]
    #[serde(rename = "Industry")]
    industry: String,
    #[serde(default)]
    #[serde(rename = "Current Price")]
    current_price: String,
    #[serde(default)]
    #[serde(rename = "Market Capitalization")]
    market_cap: String,
    #[serde(default)]
    #[serde(rename = "Price to Earning")]
    pe: String,
    #[serde(default)]
    #[serde(rename = "Return on equity")]
    roe: String,
    #[serde(default)]
    #[serde(rename = "Return on capital employed")]
    roce: String,
    #[serde(default)]
    #[serde(rename = "OPM")]
    opm: String,
    #[serde(default)]
    #[serde(rename = "Price to book value")]
    pb: String,
    #[serde(default)]
    #[serde(rename = "Debt to equity")]
    debt_eq: String,
    #[serde(default)]
    #[serde(rename = "Sales growth 5Years")]
    sales_5y: String,
    #[serde(default)]
    #[serde(rename = "Profit growth 5Years")]
    profit_5y: String,
    #[serde(default)]
    #[serde(rename = "Promoter holding")]
    promoter: String,
    #[serde(default)]
    #[serde(rename = "FII holding")]
    fii: String,
    #[serde(default)]
    #[serde(rename = "DII holding")]
    dii: String,
    #[serde(default)]
    #[serde(rename = "Down from 52w high_y")]
    down_52w: String,
    #[serde(default)]
    #[serde(rename = "Up from 52w low")]
    up_52w: String,
}

fn clean_opt_date(opt: &Option<String>) -> Option<&str> {
    match opt {
        Some(s) => {
            let trimmed = s.trim();
            if trimmed.is_empty()
                || trimmed.eq_ignore_ascii_case("nan")
                || trimmed.eq_ignore_ascii_case("nat")
            {
                None
            } else {
                Some(trimmed.split('T').next().unwrap().trim())
            }
        }
        None => None,
    }
}

/// Candles of one symbol from its exchange's history start (third parameter).
const CHART_EOD_SQL: &str = "
    SELECT CAST(Date AS VARCHAR), COALESCE(Open, 0.0), COALESCE(High, 0.0), COALESCE(Low, 0.0), COALESCE(Close, 0.0), COALESCE(Volume, 0.0)
    FROM eod_data
    WHERE Symbol = ? AND Exchange = ? AND Date >= CAST(? AS DATE)
    ORDER BY Date ASC
";

/// First chart date per exchange: the same fixed history anchor as the
/// screens, so chart EMAs match the rules.  Computed once per run.
fn chart_history_starts(
    conn: &duckdb::Connection,
) -> Result<HashMap<String, String>, Box<dyn std::error::Error>> {
    let mut stmt = conn
        .prepare("SELECT Exchange, CAST(MAX(Date) AS VARCHAR) FROM eod_data GROUP BY Exchange")?;
    let mut rows = stmt.query([])?;
    let mut starts = HashMap::new();
    while let Some(row) = rows.next()? {
        let exchange: String = row.get(0)?;
        let latest: String = row.get(1)?;
        if let Some(start) = screener::history_start(&latest) {
            starts.insert(exchange, start);
        }
    }
    Ok(starts)
}

fn load_chart_eod(
    stmt: &mut duckdb::Statement<'_>,
    history_starts: &HashMap<String, String>,
    symbol: &str,
    exchange: &str,
) -> Result<chart::EodData, Box<dyn std::error::Error>> {
    // An exchange without any candle has no start and therefore no rows.
    let start = history_starts
        .get(exchange)
        .map_or("9999-12-31", String::as_str);
    let mut rows = stmt.query([symbol, exchange, start])?;
    let mut eod = chart::EodData {
        dates: Vec::new(),
        opens: Vec::new(),
        highs: Vec::new(),
        lows: Vec::new(),
        closes: Vec::new(),
        volumes: Vec::new(),
    };
    while let Some(row) = rows.next()? {
        eod.dates.push(row.get::<_, String>(0)?);
        eod.opens.push(row.get::<_, f64>(1)?);
        eod.highs.push(row.get::<_, f64>(2)?);
        eod.lows.push(row.get::<_, f64>(3)?);
        eod.closes.push(row.get::<_, f64>(4)?);
        eod.volumes.push(row.get::<_, f64>(5)?);
    }
    Ok(eod)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    // One global Rayon pool owns the machine for screening or charting.
    let _ = rayon::ThreadPoolBuilder::new()
        .stack_size(8 * 1024 * 1024)
        .build_global();
    if args.len() == 2 && matches!(args[1].as_str(), "--version" | "-V") {
        println!("stock_analysis {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    #[cfg(feature = "gui")]
    {
        if args.len() >= 3 && args[1] == "gui" {
            let exchange = args.get(3).map(String::as_str).unwrap_or("NSE");
            return gui::run_gui(&args[2], exchange);
        }
    }
    if args.len() >= 2 && args[1] == "screen" {
        return screener::run_screener_cmd(&args[2..]);
    }
    if args.len() >= 2 && args[1] == "pipeline" {
        return pipeline::run_pipeline_cmd(&args[2..]);
    }

    let (tasks_csv, duckdb_file, output_dir) = if args.len() >= 5 && args[1] == "chart" {
        (&args[3], &args[2], &args[4])
    } else if args.len() >= 4 {
        (&args[1], &args[2], &args[3])
    } else {
        eprintln!("Usage: {} <tasks_csv> <duckdb_file> <output_dir>", args[0]);
        eprintln!(
            "   OR: {} chart <duckdb_file> <tasks_json/csv> <output_dir>",
            args[0]
        );
        eprintln!(
            "   OR: {} screen <duckdb_file> <fundamental_csv> <NSE|BSE|ALL> <out_dir>",
            args[0]
        );
        eprintln!(
            "   OR: {} pipeline <duckdb_file> <fundamental_csv> <output_root> <chart_root> <timestamp> <opentrader_root>",
            args[0]
        );
        #[cfg(feature = "gui")]
        eprintln!("   OR: {} gui <duckdb_file> [NSE|BSE]", args[0]);
        std::process::exit(1);
    };

    let start_time = std::time::Instant::now();

    fs::create_dir_all(output_dir)?;

    println!("Loading chart tasks list from: {}", tasks_csv);
    let mut rdr = csv::Reader::from_path(tasks_csv)?;
    let mut tasks: Vec<ChartTask> = Vec::new();
    for result in rdr.deserialize() {
        let task: ChartTask = result?;
        tasks.push(task);
    }
    println!("Loaded {} chart tasks to render.", tasks.len());

    println!("Loading EOD data from DuckDB...");
    let conn = duckdb::Connection::open_with_flags(
        duckdb_file,
        duckdb::Config::default().access_mode(duckdb::AccessMode::ReadOnly)?,
    )?;

    let mut eod_cache = HashMap::new();

    let history_starts = chart_history_starts(&conn)?;
    let mut stmt = conn.prepare(CHART_EOD_SQL)?;

    for task in &tasks {
        let symbol = &task.symbol;
        let mut exchange = "NSE";
        if !task.exchange_cap.is_empty() {
            exchange = task.exchange_cap.trim();
        } else if !task.exchange_low.is_empty() {
            exchange = task.exchange_low.trim();
        }

        let cache_key = format!("{}_{}", symbol, exchange);
        if eod_cache.contains_key(&cache_key) {
            continue;
        }

        let eod = load_chart_eod(&mut stmt, &history_starts, symbol, exchange)?;
        if eod.dates.is_empty() {
            eprintln!("No EOD data found in DuckDB for {} ({})", symbol, exchange);
        }
        eod_cache.insert(cache_key, eod);
    }

    std::mem::drop(stmt);
    std::mem::drop(conn);
    println!("Loaded EOD data for {} unique symbols.", eod_cache.len());

    println!("Generating charts in parallel...");
    tasks.par_iter().for_each(|task| {
        let symbol = &task.symbol;
        let index_val = &task.index;
        let file_name = format!("{}_{}.png", index_val, symbol);
        let task_output = if task.chart_output.trim().is_empty() {
            Path::new(&output_dir)
        } else {
            Path::new(task.chart_output.trim())
        };
        if let Err(error) = fs::create_dir_all(task_output) {
            eprintln!("Failed to create chart directory for {}: {}", symbol, error);
            return;
        }
        let out_path = task_output.join(file_name);

        // Determine Exchange (NSE/BSE)
        let mut exchange = "NSE";
        if !task.exchange_cap.is_empty() {
            exchange = task.exchange_cap.trim();
        } else if !task.exchange_low.is_empty() {
            exchange = task.exchange_low.trim();
        }

        let cache_key = format!("{}_{}", symbol, exchange);
        let stock_eod = match eod_cache.get(&cache_key) {
            Some(data) => data,
            None => return,
        };

        if stock_eod.dates.is_empty() {
            return;
        }

        // Parse date range
        let clean_plot_start = task.plot_start.split('T').next().unwrap().trim();
        let clean_plot_end = task.plot_end.split('T').next().unwrap().trim();
        let plot_start = NaiveDate::parse_from_str(clean_plot_start, "%Y-%m-%d").unwrap();
        let plot_end = NaiveDate::parse_from_str(clean_plot_end, "%Y-%m-%d").unwrap();

        // Layout vertical lines based on task date fields
        // Prefer the qualifying tight pole, which the breakout date belongs to.
        let p_start = clean_opt_date(&task.tight_start_date)
            .or_else(|| clean_opt_date(&task.flag_start_date))
            .and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok());
        let p_end = clean_opt_date(&task.tight_peak_date)
            .or_else(|| clean_opt_date(&task.flag_peak_date))
            .and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok());
        let breakout = clean_opt_date(&task.breakout_date)
            .or_else(|| clean_opt_date(&task.mark_tight_date))
            .and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok());

        // Construct fundamental info lines exactly like Python
        let info_line1 = format!(
            "{}  |  Industry: {}  |  Price: ₹{}  |  MCap: ₹{} Cr  |  PE: {}",
            task.name, task.industry, task.current_price, task.market_cap, if task.pe.is_empty() { "N/A" } else { &task.pe }
        );
        let info_line2 = format!(
            "ROE: {}%  |  ROCE: {}%  |  OPM: {}%  |  P/B: {}  |  Debt/Eq: {}",
            if task.roe.is_empty() { "N/A" } else { &task.roe },
            if task.roce.is_empty() { "N/A" } else { &task.roce },
            if task.opm.is_empty() { "N/A" } else { &task.opm },
            if task.pb.is_empty() { "N/A" } else { &task.pb },
            if task.debt_eq.is_empty() { "N/A" } else { &task.debt_eq }
        );
        let info_line3 = format!(
            "Sales 5Y: {}%  |  Profit 5Y: {}%  |  Promoter: {}%  |  FII: {}%  |  DII: {}%  |  52w: -{}% / +{}%",
            if task.sales_5y.is_empty() { "N/A" } else { &task.sales_5y },
            if task.profit_5y.is_empty() { "N/A" } else { &task.profit_5y },
            if task.promoter.is_empty() { "N/A" } else { &task.promoter },
            if task.fii.is_empty() { "N/A" } else { &task.fii },
            if task.dii.is_empty() { "N/A" } else { &task.dii },
            if task.down_52w.is_empty() { "0" } else { &task.down_52w },
            if task.up_52w.is_empty() { "0" } else { &task.up_52w }
        );

        if let Err(e) = chart::create_stock_chart(
            stock_eod,
            symbol,
            &out_path,
            plot_start,
            plot_end,
            p_start,
            p_end,
            breakout,
            &info_line1,
            &info_line2,
            &info_line3,
        ) {
            eprintln!("Failed to create chart for {}: {}", symbol, e);
        }
    });

    let duration = start_time.elapsed();
    println!(
        "Completed in {:.2} seconds. Output at: {}",
        duration.as_secs_f64(),
        output_dir
    );

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{chart_history_starts, load_chart_eod, CHART_EOD_SQL};

    /// NSE runs 800 days from 2025-01-01; BSE stops 30 days earlier.  The
    /// chart command keeps each exchange's own last 700 days.
    #[test]
    fn chart_command_uses_each_exchange_history_window() {
        let conn = duckdb::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE eod_data AS
             SELECT CAST(DATE '2025-01-01' + CAST(i AS INTEGER) AS DATE) AS Date,
                    'ABC' AS Symbol, Exchange,
                    100.0 AS Open, 101.0 AS High, 99.0 AS Low, 100.0 AS Close, 1.0 AS Volume
             FROM range(0, 800) AS r(i), (VALUES ('NSE'), ('BSE')) AS x(Exchange)
             WHERE Exchange = 'NSE' OR i < 770",
        )
        .unwrap();
        let starts = chart_history_starts(&conn).unwrap();
        assert_eq!(starts["NSE"], "2025-04-10");
        assert_eq!(starts["BSE"], "2025-03-11");

        let mut stmt = conn.prepare(CHART_EOD_SQL).unwrap();
        let nse = load_chart_eod(&mut stmt, &starts, "ABC", "NSE").unwrap();
        assert_eq!(nse.dates.first().map(String::as_str), Some("2025-04-10"));
        assert_eq!(nse.dates.last().map(String::as_str), Some("2027-03-11"));
        assert_eq!(nse.dates.len(), 701);
        let bse = load_chart_eod(&mut stmt, &starts, "ABC", "BSE").unwrap();
        assert_eq!(bse.dates.first().map(String::as_str), Some("2025-03-11"));
        assert_eq!(bse.dates.len(), 701);
        assert!(load_chart_eod(&mut stmt, &starts, "ABC", "MCX")
            .unwrap()
            .dates
            .is_empty());
    }
}
