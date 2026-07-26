use std::collections::{HashSet, HashMap};
use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};
use serde::{Serialize, Deserialize};
use rayon::prelude::*;
use duckdb::{Connection, Config, AccessMode};
use chrono::NaiveDate;

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
    pub mark_date: String,
    pub mark_tight_date: Option<String>,
    pub tight_breakout: bool,
    pub donchian_60_base: f64,
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
    pub mark_qualified: Vec<MarkResult>,
    pub mark_tight_qualified: Vec<MarkResult>,
    pub flag_qualified: Vec<FlagResult>,
    pub tight_flag_qualified: Vec<FlagResult>,
}

pub struct SymbolData {
    pub symbol: String,
    pub isin: String,
    pub dates: Vec<String>,
    pub opens: Vec<f64>,
    pub highs: Vec<f64>,
    pub lows: Vec<f64>,
    pub closes: Vec<f64>,
    pub volumes: Vec<f64>,
}

fn calculate_ema(closes: &[f64], period: usize) -> Vec<f64> {
    let n = closes.len();
    let mut ema = vec![f64::NAN; n];
    if n < period {
        return ema;
    }
    let sma: f64 = closes[0..period].iter().sum::<f64>() / (period as f64);
    ema[period - 1] = (sma * 100.0).round() / 100.0;
    let k = 2.0 / (period as f64 + 1.0);
    let mut prev = sma;
    for i in period..n {
        let val = closes[i] * k + prev * (1.0 - k);
        prev = val;
        ema[i] = (val * 100.0).round() / 100.0;
    }
    ema
}

/// Stage 1 ("Mark") -> Stage 2 ("Mark Tight") Hierarchical Screening
fn process_mark_screener(data: &SymbolData, breakout_window: usize) -> Option<MarkResult> {
    let n = data.closes.len();
    if n < 10 {
        return None;
    }

    let ema_50 = calculate_ema(&data.closes, 50);
    let ema_150 = calculate_ema(&data.closes, 150);
    let ema_200 = calculate_ema(&data.closes, 200);

    let period_52w = std::cmp::min(252, n);
    let mut high_52w = vec![f64::NAN; n];
    let mut low_52w = vec![f64::NAN; n];

    for i in 0..n {
        if i + 1 >= period_52w {
            let start = i + 1 - period_52w;
            let h_max = data.highs[start..=i].iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let l_min = data.lows[start..=i].iter().cloned().fold(f64::INFINITY, f64::min);
            high_52w[i] = h_max;
            low_52w[i] = l_min;
        }
    }

    // --- STAGE 1: Mark Date Calculation ---
    let mut first_match_date_mark: Option<String> = None;
    for i in (0..n).rev() {
        if ema_50[i].is_nan() || ema_150[i].is_nan() || ema_200[i].is_nan() || high_52w[i].is_nan() || low_52w[i].is_nan() {
            break;
        }
        let ema_cond = data.closes[i] > ema_50[i] && ema_50[i] > ema_150[i] && ema_150[i] > ema_200[i];
        let high_cond = data.closes[i] >= high_52w[i] * 0.80;
        let low_cond = data.closes[i] >= low_52w[i] * 1.50;

        if ema_cond && high_cond && low_cond {
            first_match_date_mark = Some(data.dates[i].clone());
        } else {
            break;
        }
    }

    // EARLY EXIT: If Stage 1 ("Mark") fails, exit immediately without Donchian computation!
    let mark_date = match first_match_date_mark {
        Some(d) => d,
        None => return None,
    };

    // --- STAGE 2: Mark Tight Refinement (Computed ONLY for Stage 1 Candidates) ---
    let mut base_last = f64::NAN;
    if n >= 65 { // Requires at least 60 + 5 bars for 60-day shifted Donchian
        let shift_end = n - 1 - breakout_window;
        if shift_end >= 59 {
            let start = shift_end + 1 - 60;
            base_last = data.highs[start..=shift_end].iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        }
    }

    let mut tight_breakout = false;
    let mut first_match_date_tight: Option<String> = None;

    if !base_last.is_nan() {
        let start_w = if n >= breakout_window { n - breakout_window } else { 0 };
        for i in start_w..n {
            if data.closes[i] > base_last {
                tight_breakout = true;
                first_match_date_tight = Some(data.dates[i].clone());
            }
        }
    }

    let down_52w_high = (((high_52w[n - 1] - data.closes[n - 1]) / high_52w[n - 1]) * 100.0 * 100.0).round() / 100.0;

    Some(MarkResult {
        symbol: data.symbol.clone(),
        isin: data.isin.clone(),
        date: data.dates[n - 1].clone(),
        close: data.closes[n - 1],
        ema_50: ema_50[n - 1],
        ema_150: ema_150[n - 1],
        ema_200: ema_200[n - 1],
        high_52w: high_52w[n - 1],
        low_52w: low_52w[n - 1],
        mark_date,
        mark_tight_date: first_match_date_tight,
        tight_breakout,
        donchian_60_base: base_last,
        down_52w_high,
    })
}

/// Stage 1 ("Flag") -> Stage 2 ("Tight Flag") Hierarchical Screening
fn process_flag_screener(data: &SymbolData) -> Option<FlagResult> {
    let n_total = data.closes.len();
    if n_total < 10 {
        return None;
    }

    let tail_len = std::cmp::min(90, n_total);
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
                let mut has_extreme_gap = false;
                for p_idx in (i + 1)..=j_peak {
                    let prev_c = closes[p_idx - 1];
                    let open_gap = if prev_c > 0.0 { (opens[p_idx] - prev_c).abs() / prev_c } else { 0.0 };
                    if open_gap > 0.50 {
                        has_extreme_gap = true;
                        break;
                    }
                }

                if !has_extreme_gap {
                    // 3. Circuit Trap Check (Circuit candles <= 40% in pole)
                    let mut circuit_count = 0;
                    let pole_candles = j_peak - i + 1;
                    for p_idx in i..=j_peak {
                        if (highs[p_idx] - lows[p_idx]).abs() < 1e-4 {
                            circuit_count += 1;
                        }
                    }
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
                                // 6. Stage-2 Mechanical Hard Filters for Tight Flag
                                let last_close = closes[n - 1];
                                if last_close >= 5.0 {
                                    let ema_200 = calculate_ema(&data.closes, 200);
                                    let last_ema200 = ema_200[n - 1];
                                    if last_ema200.is_nan() || last_close >= last_ema200 {
                                        let period_52w = std::cmp::min(252, n);
                                        let start_52w = if n > period_52w { n - period_52w } else { 0 };
                                        let high_52w = highs[start_52w..n].iter().cloned().fold(f64::NEG_INFINITY, f64::max);
                                        let dist_52w = if high_52w > 0.0 { (high_52w - last_close) / high_52w * 100.0 } else { 0.0 };

                                        if dist_52w <= 45.0 {
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
                    }
                }
            }

            if is_flag && is_tight_flag {
                break;
            }
        }
    }

    // EARLY EXIT: If neither Flag nor Tight Flag matched
    if !is_flag && !is_tight_flag {
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
    
    let isin_idx = headers.iter().position(|h| h.trim() == "ISIN Code")
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

fn export_qualified_csvs(
    dataset: &ScreenedDataset,
    base_out_dir: &Path,
    timestamp: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let ex = &dataset.exchange;

    let mark_dir = base_out_dir.join(ex).join("mark");
    let mark_tight_dir = base_out_dir.join(ex).join("mark_tight");
    let flag_dir = base_out_dir.join(ex).join("Flag");
    let tight_flag_dir = base_out_dir.join(ex).join("Tight Flag");

    std::fs::create_dir_all(&mark_dir)?;
    std::fs::create_dir_all(&mark_tight_dir)?;
    std::fs::create_dir_all(&flag_dir)?;
    std::fs::create_dir_all(&tight_flag_dir)?;

    // 1. Mark CSV
    let mark_file = mark_dir.join(format!("{}_{}_mark_qualified.csv", timestamp, ex.to_lowercase()));
    let mut w_mark = csv::Writer::from_path(&mark_file)?;
    w_mark.write_record(&["symbol", "isin", "date", "close", "ema_50", "ema_150", "ema_200", "high_52w", "low_52w", "mark_date", "down_52w_high"])?;
    for r in &dataset.mark_qualified {
        w_mark.write_record(&[
            &r.symbol, &r.isin, &r.date, &r.close.to_string(),
            &r.ema_50.to_string(), &r.ema_150.to_string(), &r.ema_200.to_string(),
            &r.high_52w.to_string(), &r.low_52w.to_string(), &r.mark_date, &r.down_52w_high.to_string()
        ])?;
    }
    w_mark.flush()?;

    // 2. Mark Tight CSV
    let mark_tight_file = mark_tight_dir.join(format!("{}_{}_mark_tight_qualified.csv", timestamp, ex.to_lowercase()));
    let mut w_mt = csv::Writer::from_path(&mark_tight_file)?;
    w_mt.write_record(&["symbol", "isin", "date", "close", "ema_50", "ema_150", "ema_200", "high_52w", "low_52w", "mark_date", "mark_tight_date", "down_52w_high"])?;
    for r in &dataset.mark_tight_qualified {
        w_mt.write_record(&[
            &r.symbol, &r.isin, &r.date, &r.close.to_string(),
            &r.ema_50.to_string(), &r.ema_150.to_string(), &r.ema_200.to_string(),
            &r.high_52w.to_string(), &r.low_52w.to_string(), &r.mark_date,
            r.mark_tight_date.as_deref().unwrap_or(""), &r.down_52w_high.to_string()
        ])?;
    }
    w_mt.flush()?;

    // 3. Flag CSV
    let flag_file = flag_dir.join(format!("{}_{}_flag_qualified.csv", timestamp, ex.to_lowercase()));
    let mut w_flag = csv::Writer::from_path(&flag_file)?;
    w_flag.write_record(&["symbol", "isin", "date", "close", "flag_date", "flag_start_date", "flag_peak_date"])?;
    for r in &dataset.flag_qualified {
        w_flag.write_record(&[
            &r.symbol, &r.isin, &r.date, &r.close.to_string(),
            r.flag_date.as_deref().unwrap_or(""),
            r.flag_start_date.as_deref().unwrap_or(""),
            r.flag_peak_date.as_deref().unwrap_or("")
        ])?;
    }
    w_flag.flush()?;

    // 4. Tight Flag CSV
    let tight_flag_file = tight_flag_dir.join(format!("{}_{}_tight_flag_qualified.csv", timestamp, ex.to_lowercase()));
    let mut w_tf = csv::Writer::from_path(&tight_flag_file)?;
    w_tf.write_record(&["symbol", "isin", "date", "close", "tight_flag_date", "tight_start_date", "tight_peak_date", "breakout_date"])?;
    for r in &dataset.tight_flag_qualified {
        w_tf.write_record(&[
            &r.symbol, &r.isin, &r.date, &r.close.to_string(),
            r.tight_flag_date.as_deref().unwrap_or(""),
            r.tight_start_date.as_deref().unwrap_or(""),
            r.tight_peak_date.as_deref().unwrap_or(""),
            r.breakout_date.as_deref().unwrap_or("")
        ])?;
    }
    w_tf.flush()?;

    Ok(())
}

fn render_symbol_chart(
    dataset: &SymbolData,
    out_dir: &Path,
    plot_days: i64,
    mark_date: Option<&str>,
    tight_breakout_date: Option<&str>,
    flag_start_date: Option<&str>,
    flag_peak_date: Option<&str>,
    tight_start_date: Option<&str>,
    tight_peak_date: Option<&str>,
    breakout_date: Option<&str>,
) -> Result<(), Box<dyn std::error::Error>> {
    let eod = crate::chart::EodData {
        dates: dataset.dates.clone(),
        opens: dataset.opens.clone(),
        highs: dataset.highs.clone(),
        lows: dataset.lows.clone(),
        closes: dataset.closes.clone(),
        volumes: dataset.volumes.clone(),
    };

    let n = dataset.dates.len();
    if n == 0 {
        return Ok(());
    }

    let parse_d = |s: Option<&str>| -> Option<NaiveDate> {
        s.and_then(|d| NaiveDate::parse_from_str(d, "%Y-%m-%d").ok())
    };

    let last_d_str = &dataset.dates[n - 1];
    let end_d = NaiveDate::parse_from_str(last_d_str, "%Y-%m-%d")
        .unwrap_or_else(|_| chrono::Local::now().date_naive());
    let start_d = end_d - chrono::Duration::days(plot_days);

    let p_start = parse_d(flag_start_date).or_else(|| parse_d(tight_start_date));
    let p_end = parse_d(flag_peak_date).or_else(|| parse_d(tight_peak_date));
    let bo_d = parse_d(breakout_date).or_else(|| parse_d(tight_breakout_date));
    let m_d = parse_d(mark_date);

    let img_name = format!("{}.png", dataset.symbol);
    let output_path = out_dir.join(img_name);

    crate::chart::create_stock_chart(
        &eod,
        &dataset.symbol,
        &output_path,
        start_d,
        end_d,
        p_start,
        p_end,
        bo_d,
        m_d,
        "",
        "",
        "",
    )
}

pub fn run_screener_cmd(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    if args.len() < 4 {
        eprintln!("Usage: stock_analysis screen <duckdb_file> <fundamental_csv> <exchange> <out_dir> [max_date] [chart_out_base]");
        std::process::exit(1);
    }

    let duckdb_file = &args[0];
    let fundamental_csv = &args[1];
    let exchange = args[2].to_uppercase();
    let out_dir = &args[3];

    let max_date = if args.len() >= 5 && !args[4].trim().is_empty() { Some(args[4].trim().to_string()) } else { None };
    let chart_out_base = if args.len() >= 6 && !args[5].trim().is_empty() { Some(PathBuf::from(args[5].trim())) } else { None };

    std::fs::create_dir_all(out_dir)?;

    let t_total = std::time::Instant::now();
    let t0 = std::time::Instant::now();
    let valid_isins = load_valid_isins(fundamental_csv)?;

    let conn = Connection::open_with_flags(
        duckdb_file,
        Config::default().access_mode(AccessMode::ReadOnly)?,
    )?;

    // --- STEP 1: BULK SINGLE-PASS DUCKDB QUERY ---
    let bulk_eod_sql = if exchange == "NSE" {
        "SELECT CAST(Date AS VARCHAR), Symbol, ISIN, Open, High, Low, Close, Volume FROM eod_data WHERE Exchange = 'NSE' ORDER BY Symbol, Date ASC"
    } else {
        "SELECT CAST(Date AS VARCHAR), Symbol, ISIN, Open, High, Low, Close, Volume FROM eod_data WHERE Exchange = 'BSE' AND ISIN NOT IN (SELECT DISTINCT ISIN FROM eod_data WHERE Exchange = 'NSE') ORDER BY Symbol, Date ASC"
    };

    let mut stmt = conn.prepare(bulk_eod_sql)?;
    let mut rows = stmt.query([])?;

    let include_sme = args.iter().any(|a| a == "--include-sme");
    let only_sme = args.iter().any(|a| a == "--only-sme");

    let mut symbol_datasets: Vec<SymbolData> = Vec::new();
    let mut dataset_map: HashMap<String, usize> = HashMap::new();

    let mut curr_sym = String::new();
    let mut curr_isin = String::new();
    let mut curr_dates = Vec::new();
    let mut curr_opens = Vec::new();
    let mut curr_highs = Vec::new();
    let mut curr_lows = Vec::new();
    let mut curr_closes = Vec::new();
    let mut curr_vols = Vec::new();

    while let Some(row) = rows.next()? {
        let d_str: String = row.get(0)?;
        let sym: String = row.get(1)?;
        let is_sme = sym.trim().to_uppercase().ends_with("_SME");
        if only_sme {
            if !is_sme { continue; }
        } else if !include_sme {
            if is_sme { continue; }
        }

        let isin: String = row.get(2)?;
        let isin_clean = isin.trim().to_uppercase();

        if !valid_isins.contains(&isin_clean) {
            continue;
        }

        let clean_date = d_str.split('T').next().unwrap_or(&d_str).to_string();
        if let Some(ref md) = max_date {
            if clean_date > *md {
                continue;
            }
        }

        let open: f64 = row.get(3)?;
        let high: f64 = row.get(4)?;
        let low: f64 = row.get(5)?;
        let close: f64 = row.get(6)?;
        let vol: f64 = row.get(7)?;

        if sym != curr_sym {
            if !curr_sym.is_empty() && !curr_dates.is_empty() {
                let idx = symbol_datasets.len();
                dataset_map.insert(curr_sym.clone(), idx);
                symbol_datasets.push(SymbolData {
                    symbol: curr_sym,
                    isin: curr_isin,
                    dates: curr_dates,
                    opens: curr_opens,
                    highs: curr_highs,
                    lows: curr_lows,
                    closes: curr_closes,
                    volumes: curr_vols,
                });
            }
            curr_sym = sym;
            curr_isin = isin_clean;
            curr_dates = Vec::new();
            curr_opens = Vec::new();
            curr_highs = Vec::new();
            curr_lows = Vec::new();
            curr_closes = Vec::new();
            curr_vols = Vec::new();
        }

        curr_dates.push(clean_date);
        curr_opens.push(open);
        curr_highs.push(high);
        curr_lows.push(low);
        curr_closes.push(close);
        curr_vols.push(vol);
    }

    if !curr_sym.is_empty() && !curr_dates.is_empty() {
        let idx = symbol_datasets.len();
        dataset_map.insert(curr_sym.clone(), idx);
        symbol_datasets.push(SymbolData {
            symbol: curr_sym,
            isin: curr_isin,
            dates: curr_dates,
            opens: curr_opens,
            highs: curr_highs,
            lows: curr_lows,
            closes: curr_closes,
            volumes: curr_vols,
        });
    }

    let elapsed_db = t0.elapsed().as_secs_f64();
    println!("⏱️ [1/4] Bulk DuckDB EOD Fetch: {:.2}s (Loaded {} valid {} symbols)", elapsed_db, symbol_datasets.len(), exchange);

    // --- STEP 2: PARALLEL HIERARCHICAL TA SCREENING ---
    let t1 = std::time::Instant::now();

    let mark_results: Vec<MarkResult> = symbol_datasets
        .par_iter()
        .filter_map(|d| process_mark_screener(d, 5))
        .collect();

    let flag_results: Vec<FlagResult> = symbol_datasets
        .par_iter()
        .filter_map(|d| process_flag_screener(d))
        .collect();

    let mark_tight: Vec<MarkResult> = mark_results
        .iter()
        .filter(|m| m.tight_breakout && m.close >= m.high_52w * 0.85)
        .cloned()
        .collect();

    let flag_qualified: Vec<FlagResult> = flag_results
        .iter()
        .filter(|f| f.is_flag)
        .cloned()
        .collect();

    let tight_flag_qualified: Vec<FlagResult> = flag_results
        .iter()
        .filter(|f| f.is_tight_flag)
        .cloned()
        .collect();

    let elapsed_calc = t1.elapsed().as_secs_f64();
    println!("⏱️ [2/4] Hierarchical TA Screening: {:.4}s", elapsed_calc);
    println!("   └─ Mark: {} | Mark Tight: {} | Flag: {} | Tight Flag: {}",
             mark_results.len(), mark_tight.len(), flag_qualified.len(), tight_flag_qualified.len());

    // --- STEP 3: CANDIDATE DATASET JSON & CSV EXPORT ---
    let t2 = std::time::Instant::now();
    let dataset = ScreenedDataset {
        exchange: exchange.clone(),
        mark_qualified: mark_results.clone(),
        mark_tight_qualified: mark_tight.clone(),
        flag_qualified: flag_qualified.clone(),
        tight_flag_qualified: tight_flag_qualified.clone(),
    };

    let now_ts = chrono::Local::now().format("%Y%m%d_%H%M%S").to_string();

    let out_file = Path::new(out_dir).join(format!("{}_rust_screened.json", exchange));
    let f = File::create(&out_file)?;
    serde_json::to_writer_pretty(f, &dataset)?;

    // Also export qualified CSVs to base output dir if available
    let base_qualified_dir = Path::new(duckdb_file).parent()
        .and_then(|p| p.parent())
        .map(|p| p.join("qualified_stocks"))
        .unwrap_or_else(|| PathBuf::from(out_dir));

    let _ = export_qualified_csvs(&dataset, &base_qualified_dir, &now_ts);

    println!("⏱️ [3/4] Candidate Dataset Export: {:.3}s -> {}", t2.elapsed().as_secs_f64(), out_file.display());

    // --- STEP 4: PARALLEL RAM CHART RENDERING (EXACT FOLDER PATH ALIGNMENT) ---
    if let Some(ref base_chart_dir) = chart_out_base {
        let t_render = std::time::Instant::now();

        let mark_chart_dir = base_chart_dir.join(&exchange).join("mark");
        let mark_tight_chart_dir = base_chart_dir.join(&exchange).join("mark_tight");
        let flag_chart_dir = base_chart_dir.join(&exchange).join("Flag");
        let tight_flag_chart_dir = base_chart_dir.join(&exchange).join("Tight Flag");

        if mark_chart_dir.exists() { let _ = std::fs::remove_dir_all(&mark_chart_dir); }
        if mark_tight_chart_dir.exists() { let _ = std::fs::remove_dir_all(&mark_tight_chart_dir); }
        if flag_chart_dir.exists() { let _ = std::fs::remove_dir_all(&flag_chart_dir); }
        if tight_flag_chart_dir.exists() { let _ = std::fs::remove_dir_all(&tight_flag_chart_dir); }

        std::fs::create_dir_all(&mark_chart_dir)?;
        std::fs::create_dir_all(&mark_tight_chart_dir)?;
        std::fs::create_dir_all(&flag_chart_dir)?;
        std::fs::create_dir_all(&tight_flag_chart_dir)?;

        // Render Mark Charts
        mark_results.par_iter().for_each(|res| {
            if let Some(&idx) = dataset_map.get(&res.symbol) {
                let data = &symbol_datasets[idx];
                let _ = render_symbol_chart(
                    data, &mark_chart_dir, 500,
                    Some(&res.mark_date), res.mark_tight_date.as_deref(),
                    None, None, None, None, None
                );
            }
        });

        // Render Mark Tight Charts
        mark_tight.par_iter().for_each(|res| {
            if let Some(&idx) = dataset_map.get(&res.symbol) {
                let data = &symbol_datasets[idx];
                let _ = render_symbol_chart(
                    data, &mark_tight_chart_dir, 500,
                    Some(&res.mark_date), res.mark_tight_date.as_deref(),
                    None, None, None, None, None
                );
            }
        });

        // Render Flag Charts
        flag_qualified.par_iter().for_each(|res| {
            if let Some(&idx) = dataset_map.get(&res.symbol) {
                let data = &symbol_datasets[idx];
                let _ = render_symbol_chart(
                    data, &flag_chart_dir, 200,
                    None, None,
                    res.flag_start_date.as_deref(), res.flag_peak_date.as_deref(),
                    res.tight_start_date.as_deref(), res.tight_peak_date.as_deref(),
                    res.breakout_date.as_deref()
                );
            }
        });

        // Render Tight Flag Charts
        tight_flag_qualified.par_iter().for_each(|res| {
            if let Some(&idx) = dataset_map.get(&res.symbol) {
                let data = &symbol_datasets[idx];
                let _ = render_symbol_chart(
                    data, &tight_flag_chart_dir, 200,
                    None, None,
                    res.flag_start_date.as_deref(), res.flag_peak_date.as_deref(),
                    res.tight_start_date.as_deref(), res.tight_peak_date.as_deref(),
                    res.breakout_date.as_deref()
                );
            }
        });

        let total_charts = mark_results.len() + mark_tight.len() + flag_qualified.len() + tight_flag_qualified.len();
        println!("⏱️ [4/4] Parallel RAM Chart Rendering: {:.2}s (Created {} charts)", t_render.elapsed().as_secs_f64(), total_charts);
    }

    println!("----------------------------------------------------------------------");
    println!("🚀 TOTAL RUST SINGLE-PASS PIPELINE TIME: {:.2}s", t_total.elapsed().as_secs_f64());
    println!("----------------------------------------------------------------------");

    Ok(())
}
