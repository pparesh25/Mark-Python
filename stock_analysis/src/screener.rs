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
    /// Latest session stored for this exchange (after any max_date limit).
    pub latest_session: Option<String>,
    /// Symbols skipped because their last candle is too old to screen.
    pub stale_skipped: usize,
    pub mark_tight_qualified: Vec<MarkResult>,
    pub tight_flag_qualified: Vec<FlagResult>,
}

#[derive(Debug, Clone)]
pub struct SymbolData {
    pub exchange: String,
    pub symbol: String,
    pub isin: String,
    pub dates: Vec<String>,
    pub opens: Vec<f64>,
    pub highs: Vec<f64>,
    pub lows: Vec<f64>,
    pub closes: Vec<f64>,
    pub volumes: Vec<f64>,
}

#[derive(Debug, Clone)]
pub struct ScreenOptions {
    pub include_sme: bool,
    pub only_sme: bool,
    pub flag_lookback: usize,
    /// A symbol is screened only when its last candle is within this many
    /// sessions of its exchange's latest session.
    pub max_stale_sessions: usize,
    pub mark_tight_only: bool,
    pub tight_flag_only: bool,
    pub max_date: Option<String>,
}

impl Default for ScreenOptions {
    fn default() -> Self {
        Self {
            include_sme: false,
            only_sme: false,
            flag_lookback: 90,
            max_stale_sessions: 5,
            mark_tight_only: false,
            tight_flag_only: false,
            max_date: None,
        }
    }
}

pub struct ScreenRun {
    pub symbol_datasets: Vec<SymbolData>,
    pub datasets: HashMap<String, ScreenedDataset>,
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

/// Rounded EMA exactly as the screens evaluate it: SMA seed, NaN during the
/// warm-up period.  Charts reuse it so the plotted lines match the rules.
pub fn ema_series(closes: &[f64], period: usize) -> Vec<f64> {
    let mut ema = EmaAccumulator::new(period, closes.len());
    for (index, close) in closes.iter().copied().enumerate() {
        ema.push(index, close);
    }
    ema.values
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
        ema_50: ema_series(&data.closes, 50),
        ema_150: ema_series(&data.closes, 150),
        ema_200: ema_series(&data.closes, 200),
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

/// Mark Tight is the latest 60-session Donchian breakout that still holds
/// inside the current uninterrupted Mark trend.  A breakout is a candle whose
/// close crossed above the prior 60-session high (the first candle of the run
/// counts when it is already above); the reported one is the most recent
/// breakout whose level is still below the latest close.  The latest candle
/// must also satisfy Mark.
fn process_mark_screener(data: &SymbolData, indicators: &MarkIndicators) -> Option<MarkResult> {
    let n = data.closes.len();
    if n < 252 || !mark_condition(data, indicators, n - 1) {
        return None;
    }

    let mut mark_start = n - 1;
    while mark_start > 0 && mark_condition(data, indicators, mark_start - 1) {
        mark_start -= 1;
    }

    let above_donchian = |index: usize| {
        let level = indicators.donchian_60[index];
        level.is_finite() && data.closes[index] > level
    };
    let latest_close = data.closes[n - 1];
    // A newer crossing the price has since fallen back below does not hide
    // an older one that still holds.
    let breakout_index = (mark_start..n).rev().find(|&index| {
        above_donchian(index)
            && (index == mark_start || !above_donchian(index - 1))
            && latest_close > indicators.donchian_60[index]
    })?;

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

/// Deepest allowed pullback from the pole high between the peak and the
/// breakout candle (0.20 = the flag's lows stay at or above 80% of the peak).
pub const MAX_FLAG_DEPTH: f64 = 0.20;

/// Indicators and rules use only the candles of the last this many calendar
/// days before each exchange's latest session.  The store keeps 730 days, so
/// every complete store has them, and machines whose stores start on
/// different days still compute the same SMA-seeded EMAs.
pub const HISTORY_DAYS: i64 = 700;

/// First date used for indicators, given an exchange's latest session.
pub fn history_start(latest_session: &str) -> Option<String> {
    let latest = chrono::NaiveDate::parse_from_str(latest_session, "%Y-%m-%d").ok()?;
    Some(
        (latest - chrono::Duration::days(HISTORY_DAYS))
            .format("%Y-%m-%d")
            .to_string(),
    )
}

/// Whether qualifying pole `candidate` (k, j_peak, low_i, i) beats `current`:
/// the later breakout wins, then the later peak, then the start with the
/// lowest low, then the later start.
fn later_pole(candidate: (usize, usize, f64, usize), current: (usize, usize, f64, usize)) -> bool {
    let (k, j, low, i) = candidate;
    let (best_k, best_j, best_low, best_i) = current;
    if k != best_k {
        return k > best_k;
    }
    if j != best_j {
        return j > best_j;
    }
    if low != best_low {
        return low < best_low;
    }
    i > best_i
}

/// Stage 1 ("Flag") -> Stage 2 ("Tight Flag") Hierarchical Screening.
///
/// `flag_*` describe the first 90%-in-45-candles pole of the lookback window
/// (the internal stage-1 gate).  Every candidate pole is then checked, and the
/// qualifying pole whose breakout candle is the most recent supplies the
/// `tight_*` dates and `breakout_date` (see `later_pole` for ties).
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
        let high = data.highs[n_total - 252..]
            .iter()
            .copied()
            .fold(f64::NEG_INFINITY, f64::max);
        (ema_series(&data.closes, 200)[n_total - 1], high)
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

    // The qualifying pole with the latest breakout wins: (k, j_peak, low, i).
    let mut best: Option<(usize, usize, f64, usize)> = None;

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
        for (candidate_j, &candidate_high) in highs.iter().enumerate().take(max_j + 1).skip(i + 1) {
            if candidate_high > max_h {
                max_h = candidate_high;
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
                        for (k, &candidate_close) in closes
                            .iter()
                            .enumerate()
                            .take(max_breakout_k)
                            .skip(j_peak + 1)
                        {
                            if candidate_close > highs[j_peak] {
                                breakout_k = Some(k);
                                break;
                            }
                        }

                        if let Some(k_idx) = breakout_k {
                            // 5. Flag depth: no candle between the peak and
                            // the breakout trades more than MAX_FLAG_DEPTH
                            // below the pole high.
                            let depth_floor = highs[j_peak] * (1.0 - MAX_FLAG_DEPTH);
                            let shallow = lows[j_peak + 1..k_idx]
                                .iter()
                                .all(|&low| low >= depth_floor);
                            // 6. Breakout Holding Rule: Latest close > Pole Peak High
                            if shallow && closes[n - 1] > highs[j_peak] {
                                let candidate = (k_idx, j_peak, lows[i], i);
                                if best.is_none_or(|current| later_pole(candidate, current)) {
                                    best = Some(candidate);
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if let Some((k_idx, j_peak, _, i)) = best {
        is_tight_flag = true;
        tight_flag_date = Some(dates[j_peak].clone());
        tight_start_date = Some(dates[i].clone());
        tight_peak_date = Some(dates[j_peak].clone());
        breakout_date = Some(dates[k_idx].clone());
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
        .ok_or("ISIN Code column missing in fundamental CSV")?;

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

struct ExchangeFreshness {
    latest: String,
    cutoff: String,
}

/// Latest session and the oldest acceptable last-candle date per exchange.
fn exchange_freshness(
    conn: &Connection,
    date_limit: &str,
    max_stale_sessions: usize,
) -> Result<HashMap<String, ExchangeFreshness>, Box<dyn std::error::Error>> {
    let sql = format!(
        "SELECT Exchange, CAST(MAX(Date) AS VARCHAR), CAST(MIN(Date) AS VARCHAR)
         FROM (
             SELECT Exchange, Date,
                    DENSE_RANK() OVER (PARTITION BY Exchange ORDER BY Date DESC) AS session_rank
             FROM (SELECT DISTINCT Exchange, Date FROM eod_data WHERE Date <= {date_limit})
         )
         WHERE session_rank <= {}
         GROUP BY Exchange",
        max_stale_sessions + 1
    );
    let mut stmt = conn.prepare(&sql)?;
    let mut rows = stmt.query([])?;
    let mut freshness = HashMap::new();
    while let Some(row) = rows.next()? {
        freshness.insert(
            row.get::<_, String>(0)?,
            ExchangeFreshness {
                latest: row.get(1)?,
                cutoff: row.get(2)?,
            },
        );
    }
    Ok(freshness)
}

fn sql_date_limit(max_date: Option<&str>) -> Result<String, Box<dyn std::error::Error>> {
    let Some(value) = max_date else {
        return Ok("DATE '9999-12-31'".to_string());
    };
    let parsed = chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d").map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("max_date must be YYYY-MM-DD, got {value}"),
        )
    })?;
    Ok(format!("DATE '{}'", parsed.format("%Y-%m-%d")))
}

fn validate_exchange_scope(exchange_scope: &str) -> Result<(), Box<dyn std::error::Error>> {
    if !matches!(exchange_scope, "NSE" | "BSE" | "ALL") {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "exchange must be NSE, BSE, or ALL",
        )
        .into());
    }
    Ok(())
}

pub fn screen_market(
    duckdb_file: &str,
    fundamental_csv: &str,
    exchange_scope: &str,
    options: &ScreenOptions,
) -> Result<ScreenRun, Box<dyn std::error::Error>> {
    let exchange_scope = exchange_scope.trim().to_uppercase();
    validate_exchange_scope(&exchange_scope)?;
    if options.mark_tight_only && options.tight_flag_only {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "mark-tight-only and tight-flag-only cannot be combined",
        )
        .into());
    }
    if options.flag_lookback == 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "flag lookback must be greater than zero",
        )
        .into());
    }

    let total_started = std::time::Instant::now();
    let fetch_started = std::time::Instant::now();
    let valid_isins = load_valid_isins(fundamental_csv)?;

    let conn = Connection::open_with_flags(
        duckdb_file,
        Config::default().access_mode(AccessMode::ReadOnly)?,
    )?;
    let date_limit = sql_date_limit(options.max_date.as_deref())?;
    let freshness = exchange_freshness(&conn, &date_limit, options.max_stale_sessions)?;
    // Only NSE listings that are still trading hide the BSE listing of the
    // same ISIN; a suspended or delisted NSE series must not do so.
    let nse_cutoff = freshness
        .get("NSE")
        .map_or("9999-12-31", |value| value.cutoff.as_str());
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
    // Fixed history anchor: every machine starts the indicators on the same
    // day, whatever the first candle its store happens to hold.
    let mut history_terms: Vec<String> = freshness
        .iter()
        .filter_map(|(exchange, limits)| {
            history_start(&limits.latest).map(|start| {
                format!(
                    "(e.Exchange = '{}' AND e.Date >= DATE '{start}')",
                    exchange.replace('\'', "''")
                )
            })
        })
        .collect();
    history_terms.sort();
    let history_predicate = if history_terms.is_empty() {
        "FALSE".to_string()
    } else {
        format!("({})", history_terms.join(" OR "))
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
               AND Date >= DATE '{nse_cutoff}'
               AND Date <= {date_limit}
         )
         SELECT e.Exchange, CAST(e.Date AS VARCHAR), e.Symbol, e.ISIN, \
         COALESCE(e.Open, 0.0), COALESCE(e.High, 0.0), \
         COALESCE(e.Low, 0.0), COALESCE(e.Close, 0.0), \
         COALESCE(e.Volume, 0.0) \
         FROM eod_data e
         INNER JOIN valid_isins f ON f.isin = TRIM(UPPER(e.ISIN))
         WHERE {exchange_predicate}
           AND {history_predicate}
           AND e.Date <= {date_limit} \
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
    let mut volumes = Vec::new();

    let push_current = |target: &mut Vec<SymbolData>,
                        exchange: String,
                        symbol: String,
                        isin: String,
                        dates: Vec<String>,
                        opens: Vec<f64>,
                        highs: Vec<f64>,
                        lows: Vec<f64>,
                        closes: Vec<f64>,
                        volumes: Vec<f64>| {
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
                volumes,
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
        if (options.only_sme && !is_sme) || (!options.only_sme && !options.include_sme && is_sme) {
            continue;
        }
        let clean_date = date_raw.split('T').next().unwrap_or(&date_raw).to_string();
        if options
            .max_date
            .as_ref()
            .is_some_and(|limit| clean_date > *limit)
        {
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
                volumes,
            );
            current_exchange = exchange;
            current_symbol = symbol;
            current_isin = isin_clean;
            dates = Vec::new();
            opens = Vec::new();
            highs = Vec::new();
            lows = Vec::new();
            closes = Vec::new();
            volumes = Vec::new();
        }

        dates.push(clean_date);
        opens.push(row.get(4)?);
        highs.push(row.get(5)?);
        lows.push(row.get(6)?);
        closes.push(row.get(7)?);
        volumes.push(row.get(8)?);
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
        volumes,
    );
    drop(rows);
    drop(stmt);
    drop(conn);

    // A series that stopped trading (suspension, delisting, a ticker that
    // moved) would otherwise be screened on an old candle.
    let mut stale_skipped: HashMap<String, usize> = HashMap::new();
    symbol_datasets.retain(|data| {
        let fresh = match (freshness.get(&data.exchange), data.dates.last()) {
            (Some(limits), Some(last)) => last.as_str() >= limits.cutoff.as_str(),
            _ => false,
        };
        if !fresh {
            *stale_skipped.entry(data.exchange.clone()).or_default() += 1;
        }
        fresh
    });

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
            let indicators = (!options.tight_flag_only).then(|| calculate_mark_indicators(data));
            let mark = indicators
                .as_ref()
                .and_then(|values| process_mark_screener(data, values));
            let flag = if options.mark_tight_only {
                None
            } else {
                process_flag_screener(data, options.flag_lookback, indicators.as_ref())
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

    let mut datasets = HashMap::new();
    for exchange in target_exchanges {
        let selected: Vec<&SymbolData> = symbol_datasets
            .iter()
            .filter(|data| data.exchange == *exchange)
            .collect();
        let (mark_tight_qualified, tight_flag_qualified) =
            results.remove(*exchange).unwrap_or_default();
        let dataset = ScreenedDataset {
            exchange: exchange.to_string(),
            latest_session: freshness.get(*exchange).map(|value| value.latest.clone()),
            stale_skipped: stale_skipped.get(*exchange).copied().unwrap_or(0),
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
        println!(
            "   {}: {} Mark Tight | {} Tight Flag | latest session {} | {} stale symbols skipped",
            exchange,
            dataset.mark_tight_qualified.len(),
            dataset.tight_flag_qualified.len(),
            dataset.latest_session.as_deref().unwrap_or("none"),
            dataset.stale_skipped,
        );
        datasets.insert(exchange.to_string(), dataset);
    }
    println!(
        "🚀 RUST DATA + SCREENING TIME: {:.2}s",
        total_started.elapsed().as_secs_f64()
    );
    Ok(ScreenRun {
        symbol_datasets,
        datasets,
    })
}

pub fn run_screener_cmd(args: &[String]) -> Result<(), Box<dyn std::error::Error>> {
    let mut positional: Vec<&String> = Vec::new();
    let mut options = ScreenOptions::default();
    let mut index = 0;

    while index < args.len() {
        match args[index].as_str() {
            "--include-sme" => options.include_sme = true,
            "--only-sme" => options.only_sme = true,
            "--mark-tight-only" => options.mark_tight_only = true,
            "--tight-flag-only" => options.tight_flag_only = true,
            "--max-stale-sessions" => {
                let value = args.get(index + 1).ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "--max-stale-sessions requires a non-negative integer",
                    )
                })?;
                options.max_stale_sessions = value.parse::<usize>().map_err(|_| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "--max-stale-sessions requires a non-negative integer",
                    )
                })?;
                index += 1;
            }
            "--flag-lookback" => {
                let value = args.get(index + 1).ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "--flag-lookback requires a positive integer",
                    )
                })?;
                options.flag_lookback = value.parse::<usize>().map_err(|_| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "--flag-lookback requires a positive integer",
                    )
                })?;
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
        eprintln!("Usage: stock_analysis screen <duckdb_file> <fundamental_csv> <NSE|BSE|ALL> <out_dir> [max_date] [--mark-tight-only|--tight-flag-only] [--include-sme|--only-sme] [--flag-lookback N] [--max-stale-sessions N]");
        std::process::exit(1);
    }
    options.max_date = positional
        .get(4)
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .map(str::to_string);

    let out_dir = Path::new(positional[3]);
    std::fs::create_dir_all(out_dir)?;
    let run = screen_market(positional[0], positional[1], positional[2], &options)?;
    let export_started = std::time::Instant::now();
    for (exchange, dataset) in run.datasets {
        let exchange_out_dir = if positional[2].eq_ignore_ascii_case("ALL") {
            out_dir.join(&exchange)
        } else {
            out_dir.to_path_buf()
        };
        std::fs::create_dir_all(&exchange_out_dir)?;
        let out_file = exchange_out_dir.join(format!("{}_rust_screened.json", exchange));
        let writer = std::io::BufWriter::new(File::create(&out_file)?);
        serde_json::to_writer(writer, &dataset)?;
        println!("   {} candidate JSON -> {}", exchange, out_file.display());
    }
    println!(
        "⏱️ Candidate JSON export: {:.3}s",
        export_started.elapsed().as_secs_f64()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        calculate_mark_indicators, ema_series, history_start, process_flag_screener,
        process_mark_screener, screen_market, ScreenOptions, SymbolData, HISTORY_DAYS,
        MAX_FLAG_DEPTH,
    };

    /// Candle builder for Tight Flag layouts: `PREFIX` flat candles at 50,
    /// then a `LOOKBACK`-candle window addressed by local index.
    const PREFIX: usize = 200;
    const LOOKBACK: usize = 200;

    struct Bars {
        opens: Vec<f64>,
        highs: Vec<f64>,
        lows: Vec<f64>,
        closes: Vec<f64>,
    }

    impl Bars {
        fn new() -> Self {
            let len = PREFIX + LOOKBACK;
            Self {
                opens: vec![50.0; len],
                highs: vec![50.5; len],
                lows: vec![49.5; len],
                closes: vec![50.0; len],
            }
        }

        fn set(&mut self, local: usize, open: f64, high: f64, low: f64, close: f64) {
            let index = PREFIX + local;
            self.opens[index] = open;
            self.highs[index] = high;
            self.lows[index] = low;
            self.closes[index] = close;
        }

        fn flat(
            &mut self,
            locals: std::ops::RangeInclusive<usize>,
            close: f64,
            high: f64,
            low: f64,
        ) {
            for local in locals {
                self.set(local, close, high, low, close);
            }
        }

        /// Rising candles whose highs go linearly from `first` to `last`.
        fn ramp(&mut self, from: usize, to: usize, first: f64, last: f64) {
            let step = (last - first) / (to - from) as f64;
            for local in from..=to {
                let high = first + step * (local - from) as f64;
                self.set(local, high - 0.5, high, high - 1.0, high - 0.5);
            }
        }

        fn data(self) -> SymbolData {
            let len = self.closes.len();
            let start = chrono::NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
            SymbolData {
                exchange: "NSE".to_string(),
                symbol: "FLAG".to_string(),
                isin: "INE000FLAG".to_string(),
                dates: (0..len)
                    .map(|i| {
                        (start + chrono::Duration::days(i as i64))
                            .format("%Y-%m-%d")
                            .to_string()
                    })
                    .collect(),
                opens: self.opens,
                highs: self.highs,
                lows: self.lows,
                closes: self.closes,
                volumes: vec![0.0; len],
            }
        }
    }

    fn local_date(local: usize) -> Option<String> {
        let start = chrono::NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
        Some(
            (start + chrono::Duration::days((PREFIX + local) as i64))
                .format("%Y-%m-%d")
                .to_string(),
        )
    }

    /// Pole A: start 0 (low 49.5), peak 40 (high 100), flag 41-47 with lows
    /// `a_flag_low`, breakout 48.  A >50% gap at 100 walls it off from pole B:
    /// start 100 (low 158), peak 140 (high 320), flag 141-145 with lows
    /// `b_flag_low`, breakout 146, then holding at 326.
    fn two_pole_layout(a_flag_low: f64, b_flag_low: f64) -> Bars {
        let mut bars = Bars::new();
        bars.ramp(1, 39, 51.0, 98.0);
        bars.set(40, 98.0, 100.0, 97.0, 98.0);
        bars.flat(41..=47, 95.0, 96.0, a_flag_low);
        bars.set(48, 95.0, 105.0, 94.0, 101.0);
        bars.flat(49..=99, 102.0, 104.0, 101.0);
        bars.set(100, 160.0, 162.0, 158.0, 160.0);
        bars.ramp(101, 139, 165.0, 315.0);
        bars.set(140, 314.0, 320.0, 310.0, 315.0);
        bars.flat(141..=145, 290.0, 300.0, b_flag_low);
        bars.set(146, 300.0, 330.0, 300.0, 325.0);
        bars.flat(147..=199, 326.0, 328.0, 324.0);
        bars
    }

    #[test]
    fn tight_flag_reports_the_pole_with_the_latest_breakout() {
        let data = two_pole_layout(90.0, 270.0).data();
        let result = process_flag_screener(&data, LOOKBACK, None).unwrap();

        // flag_* still describe the first 90% pole of the window.
        assert_eq!(result.flag_start_date, local_date(0));
        assert_eq!(result.flag_peak_date, local_date(40));
        // The older pole A also qualifies (breakout 48); the fresh one wins.
        assert_eq!(result.tight_start_date, local_date(100));
        assert_eq!(result.tight_peak_date, local_date(140));
        assert_eq!(result.tight_flag_date, local_date(140));
        assert_eq!(result.breakout_date, local_date(146));
    }

    #[test]
    fn tight_flag_depth_allows_19_percent_and_rejects_21_percent() {
        assert_eq!(MAX_FLAG_DEPTH, 0.20);
        let passing = two_pole_layout(81.0, 320.0 * 0.81).data();
        let result = process_flag_screener(&passing, LOOKBACK, None).unwrap();
        assert_eq!(result.breakout_date, local_date(146));

        // Both flags pull back 21%: nothing qualifies.
        let failing = two_pole_layout(79.0, 320.0 * 0.79).data();
        assert!(process_flag_screener(&failing, LOOKBACK, None).is_none());
    }

    #[test]
    fn a_too_deep_fresh_flag_falls_back_to_an_older_passing_pole() {
        let data = two_pole_layout(90.0, 320.0 * 0.79).data();
        let result = process_flag_screener(&data, LOOKBACK, None).unwrap();

        assert_eq!(result.tight_start_date, local_date(0));
        assert_eq!(result.tight_peak_date, local_date(40));
        assert_eq!(result.breakout_date, local_date(48));
    }

    /// After the gap at 100: starts 100-102 see peak 145 (high 320), starts
    /// 103-105 see peak 148 (high 321); both groups break out at 151.
    #[test]
    fn tight_flag_ties_prefer_the_later_peak_then_the_lowest_low_then_the_later_start() {
        let mut bars = two_pole_layout(90.0, 270.0);
        bars.set(100, 160.0, 162.0, 158.0, 160.0);
        bars.set(101, 160.0, 162.0, 150.0, 160.0); // lowest low, earlier peak
        bars.set(102, 160.0, 162.0, 157.0, 160.0);
        bars.set(103, 160.0, 162.0, 155.0, 160.0);
        bars.set(104, 160.0, 162.0, 155.0, 160.0);
        bars.set(105, 160.0, 162.0, 157.0, 160.0);
        bars.ramp(106, 144, 165.0, 315.0);
        bars.set(145, 314.0, 320.0, 310.0, 315.0);
        bars.flat(146..=147, 290.0, 300.0, 270.0);
        bars.set(148, 295.0, 321.0, 290.0, 300.0);
        bars.flat(149..=150, 290.0, 300.0, 270.0);
        bars.set(151, 300.0, 330.0, 300.0, 325.0);
        bars.flat(152..=199, 326.0, 328.0, 324.0);
        let result = process_flag_screener(&bars.data(), LOOKBACK, None).unwrap();

        assert_eq!(result.breakout_date, local_date(151));
        assert_eq!(result.tight_peak_date, local_date(148));
        // 103 and 104 share the lowest low of the later-peak group.
        assert_eq!(result.tight_start_date, local_date(104));
    }

    #[test]
    fn history_start_is_a_fixed_number_of_days_before_the_latest_session() {
        assert_eq!(HISTORY_DAYS, 700);
        assert_eq!(history_start("2026-10-14").as_deref(), Some("2024-11-13"));
        assert_eq!(history_start("not a date"), None);
    }

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
            volumes: vec![0.0; len],
        }
    }

    /// A close that equals the prior 60-session high is not a breakout.
    #[test]
    fn records_a_real_donchian_breakout_in_the_active_mark_run() {
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

    /// Crossings at 251, 260 and 265 inside one Mark run: the latest wins.
    #[test]
    fn mark_tight_reports_the_latest_crossing_in_the_run() {
        let mut closes = vec![100.0; 270];
        for (i, close) in closes.iter_mut().enumerate().skip(251) {
            *close = match i {
                251..=259 => 200.0,
                260..=264 => 210.0,
                _ => 215.0,
            };
        }
        let highs = closes.clone();
        let data = symbol_data(closes, highs);
        let indicators = calculate_mark_indicators(&data);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-265");
    }

    /// Same crossings, but the latest close (205) has fallen back below the
    /// 265 crossing's level (210): the 260 crossing (level 200) still holds.
    #[test]
    fn mark_tight_reports_the_latest_crossing_that_still_holds() {
        let mut closes = vec![100.0; 270];
        for (i, close) in closes.iter_mut().enumerate().skip(251) {
            *close = match i {
                251..=259 => 200.0,
                260..=264 => 210.0,
                265..=268 => 215.0,
                _ => 205.0,
            };
        }
        let highs = closes.clone();
        let data = symbol_data(closes, highs);
        let indicators = calculate_mark_indicators(&data);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-260");
    }

    /// Closes rise above the prior 60-session high every day from 251, but a
    /// 1000 high at index 10 keeps the Mark condition false until it leaves
    /// the 52-week window at 262.
    fn streak_from_before_the_run(closes_after_251: &[f64]) -> SymbolData {
        let mut closes = vec![100.0; 251];
        closes.extend_from_slice(closes_after_251);
        let mut highs = closes.clone();
        highs[10] = 1000.0;
        symbol_data(closes, highs)
    }

    #[test]
    fn mark_tight_counts_the_first_day_of_the_run_inside_an_older_streak() {
        let rising: Vec<f64> = (0..19).map(|step| 200.0 + step as f64).collect();
        let data = streak_from_before_the_run(&rising);
        let indicators = calculate_mark_indicators(&data);
        let result = process_mark_screener(&data, &indicators).unwrap();

        // The streak began at 251, before the run; later streak days are not
        // new crossings, so the run's first day is the breakout.
        assert_eq!(result.mark_tight_date, "2024-262");
    }

    #[test]
    fn mark_tight_moves_only_when_the_close_crosses_again() {
        let mut path: Vec<f64> = (0..15).map(|step| 200.0 + step as f64).collect(); // 251-265
        path.extend_from_slice(&[205.0, 220.0, 221.0, 222.0]); // 266 below, 267 crosses
        let data = streak_from_before_the_run(&path);
        let indicators = calculate_mark_indicators(&data);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-267");
    }

    /// The run's only crossing (252, level 300) no longer holds at 250.
    #[test]
    fn mark_tight_does_not_qualify_when_no_crossing_holds() {
        let mut closes = vec![100.0; 270];
        let mut highs = vec![100.0; 270];
        for high in highs.iter_mut().take(251).skip(191) {
            *high = 300.0;
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

    #[test]
    fn ema_series_seeds_with_the_simple_average() {
        let values = ema_series(&[1.0, 2.0, 3.0, 4.0], 3);
        assert!(values[0].is_nan() && values[1].is_nan());
        assert_eq!(values[2], 2.0);
        assert_eq!(values[3], 3.0);
    }

    /// NSE `STALE` stopped trading 20 sessions ago while the same ISIN keeps
    /// trading on BSE as `STALEB`; `LAGGY` is only 3 sessions behind.
    #[test]
    fn stale_series_are_skipped_and_do_not_hide_active_bse_listings() {
        let root = std::env::temp_dir().join(format!(
            "mark-stale-screen-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("prices.duckdb");
        let fundamentals = root.join("fundamentals.csv");
        std::fs::write(
            &fundamentals,
            "Name,ISIN Code\nLive,INE111A01011\nStale,INE222B01012\nLaggy,INE333C01013\n",
        )
        .unwrap();
        {
            let conn = duckdb::Connection::open(&database).unwrap();
            conn.execute_batch(
                "CREATE TABLE eod_data AS
                 WITH series(Symbol, Exchange, ISIN, sessions) AS (
                     VALUES ('LIVE', 'NSE', 'INE111A01011', 300),
                            ('STALE', 'NSE', 'INE222B01012', 280),
                            ('LAGGY', 'NSE', 'INE333C01013', 297),
                            ('STALEB', 'BSE', 'INE222B01012', 300)
                 )
                 SELECT CAST(DATE '2025-01-01' + CAST(i AS INTEGER) AS DATE) AS Date,
                        Symbol, Exchange, ISIN,
                        100 * POW(1.003, i) AS Open,
                        100 * POW(1.003, i) * 1.001 AS High,
                        100 * POW(1.003, i) * 0.99 AS Low,
                        100 * POW(1.003, i) AS Close,
                        1000.0 AS Volume
                 FROM series, range(0, 300) AS r(i)
                 WHERE i < sessions",
            )
            .unwrap();
        }

        let run = screen_market(
            database.to_str().unwrap(),
            fundamentals.to_str().unwrap(),
            "ALL",
            &ScreenOptions::default(),
        )
        .unwrap();
        std::fs::remove_dir_all(&root).ok();

        let symbols = |exchange: &str| -> Vec<String> {
            run.datasets[exchange]
                .mark_tight_qualified
                .iter()
                .map(|result| result.symbol.clone())
                .collect()
        };
        assert_eq!(symbols("NSE"), ["LAGGY", "LIVE"]);
        assert_eq!(symbols("BSE"), ["STALEB"]);
        assert_eq!(run.datasets["NSE"].stale_skipped, 1);
        assert_eq!(
            run.datasets["NSE"].latest_session.as_deref(),
            Some("2025-10-27")
        );
    }

    /// Screens a one-symbol store whose daily candles run from
    /// 2025-01-01 + `first_day` to 2025-01-01 + 799.
    fn screen_store_starting_at(first_day: usize) -> super::ScreenRun {
        let root = std::env::temp_dir().join(format!(
            "mark-history-anchor-{}-{}-{}",
            first_day,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("prices.duckdb");
        let fundamentals = root.join("fundamentals.csv");
        std::fs::write(&fundamentals, "Name,ISIN Code\nLong,INE444D01014\n").unwrap();
        {
            let conn = duckdb::Connection::open(&database).unwrap();
            conn.execute_batch(&format!(
                "CREATE TABLE eod_data AS
                 SELECT CAST(DATE '2025-01-01' + CAST(i AS INTEGER) AS DATE) AS Date,
                        'LONG' AS Symbol, 'NSE' AS Exchange, 'INE444D01014' AS ISIN,
                        100 + 20 * SIN(i / 9.0) + i * 0.2 AS Open,
                        102 + 20 * SIN(i / 9.0) + i * 0.2 AS High,
                        98 + 20 * SIN(i / 9.0) + i * 0.2 AS Low,
                        100 + 20 * SIN(i / 9.0) + i * 0.2 AS Close,
                        1000.0 AS Volume
                 FROM range({first_day}, 800) AS r(i)"
            ))
            .unwrap();
        }
        let run = screen_market(
            database.to_str().unwrap(),
            fundamentals.to_str().unwrap(),
            "NSE",
            &ScreenOptions::default(),
        )
        .unwrap();
        std::fs::remove_dir_all(&root).ok();
        run
    }

    #[test]
    fn history_anchor_drops_candles_older_than_history_days() {
        let full = screen_store_starting_at(0);
        let pruned = screen_store_starting_at(40);

        let latest = full.datasets["NSE"].latest_session.clone().unwrap();
        assert_eq!(latest, "2027-03-11");
        let dates = &full.symbol_datasets[0].dates;
        assert_eq!(dates.first().cloned(), history_start(&latest));
        assert_eq!(dates.len() as i64, HISTORY_DAYS + 1);
        // A store that lost its earliest days screens the same candles.
        assert_eq!(pruned.symbol_datasets[0].dates, *dates);
        assert_eq!(
            pruned.symbol_datasets[0].closes,
            full.symbol_datasets[0].closes
        );
    }
}
