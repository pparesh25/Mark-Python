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

/// Whether every indicator the Mark condition reads exists at `index`.
fn mark_inputs_finite(indicators: &MarkIndicators, index: usize) -> bool {
    [
        indicators.ema_50[index],
        indicators.ema_150[index],
        indicators.ema_200[index],
        indicators.high_52w[index],
        indicators.low_52w[index],
    ]
    .iter()
    .all(|value| value.is_finite())
}

fn mark_condition(data: &SymbolData, indicators: &MarkIndicators, index: usize) -> bool {
    let close = data.closes[index];
    let ema_50 = indicators.ema_50[index];
    let ema_150 = indicators.ema_150[index];
    let ema_200 = indicators.ema_200[index];
    let high_52w = indicators.high_52w[index];
    let low_52w = indicators.low_52w[index];
    mark_inputs_finite(indicators, index)
        && close > ema_50
        && ema_50 > ema_150
        && ema_150 > ema_200
        && close >= high_52w * 0.80
        && close >= low_52w * 1.50
}

/// Mark Tight (0.5.0): a signal is a candle on which the Mark template holds
/// and the close is above the prior 60-session Donchian high.  The reported
/// signal is the first one after the latest close below EMA 50, and the
/// symbol stays listed with that date until a close below EMA 50 ends the
/// trend: a pullback under the Donchian level or a template condition other
/// than the EMA 50 close does not remove it, and a later new high does not
/// move the date.
fn process_mark_screener(data: &SymbolData, indicators: &MarkIndicators) -> Option<MarkResult> {
    let n = data.closes.len();
    if n < 252 {
        return None;
    }

    let below_ema_50 = |index: usize| {
        let ema_50 = indicators.ema_50[index];
        ema_50.is_finite() && data.closes[index] < ema_50
    };
    let above_donchian = |index: usize| {
        let level = indicators.donchian_60[index];
        level.is_finite() && data.closes[index] > level
    };
    let trend_start = (0..n)
        .rev()
        .find(|&index| below_ema_50(index))
        .map_or(0, |index| index + 1);
    let breakout_index = (trend_start..n)
        .find(|&index| mark_condition(data, indicators, index) && above_donchian(index))?;

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

/// A pole spans 4 to 45 candles from its start to its peak.
const POLE_MIN_CANDLES: usize = 4;
const POLE_MAX_CANDLES: usize = 45;
/// The breakout close comes at most this many candles after the peak.
const MAX_FLAG_CANDLES: usize = 20;

/// Pole start for a peak at `j_peak`: the lowest positive low among the
/// candles 45..=4 before it (inside the window); ties go to the later candle.
fn pole_start(lows: &[f64], j_peak: usize) -> Option<usize> {
    let first = j_peak.saturating_sub(POLE_MAX_CANDLES);
    let last = j_peak.checked_sub(POLE_MIN_CANDLES)?;
    (first..=last)
        .filter(|&index| lows[index] > 0.0)
        .fold(None, |best: Option<usize>, index| match best {
            Some(current) if lows[current] < lows[index] => Some(current),
            _ => Some(index),
        })
}

/// Stage 1 ("Flag") -> Stage 2 ("Tight Flag") Hierarchical Screening.
///
/// Every candle j of the lookback window is a candidate peak.  Its pole starts
/// at the lowest low 4..=45 candles earlier (`pole_start`), and no candle of
/// the pole may trade above high[j].  `flag_*` describe the first candidate
/// pole with a 90% run-up (the internal stage-1 gate).  A candidate qualifies
/// when the pole has no >50% gap, <= 40% circuit candles, a close above
/// high[j] within 20 candles with no flag candle trading above high[j] or
/// more than MAX_FLAG_DEPTH below it (a breakout on the candle after the peak
/// has no flag candles and qualifies), and the latest close above high[j].  The window is the exchange's last
/// `flag_lookback` sessions (`window_start`, its first date), so a thinly
/// traded symbol does not reach back months; without it, the last
/// `flag_lookback` candles.
/// The qualifying pole with the latest breakout (then the later peak)
/// supplies the `tight_*` dates and `breakout_date`.
fn process_flag_screener(
    data: &SymbolData,
    flag_lookback: usize,
    window_start: Option<&str>,
    shared_indicators: Option<&MarkIndicators>,
) -> Option<FlagResult> {
    let n_total = data.closes.len();
    if n_total < 252 {
        return None;
    }

    let start_offset = match window_start {
        Some(first) => data.dates.partition_point(|date| date.as_str() < first),
        None => n_total.saturating_sub(flag_lookback),
    };
    let tail_len = n_total - start_offset;
    if tail_len == 0 {
        return None;
    }

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

    // Candidates are ordered by breakout, then peak: the last one wins.
    let mut best: Option<(usize, usize, usize)> = None; // (k, j_peak, i)

    for j_peak in POLE_MIN_CANDLES..n {
        // --- Pole: start = lowest positive low 4..=45 candles before the peak.
        let Some(i) = pole_start(lows, j_peak) else {
            continue;
        };
        let peak_high = highs[j_peak];
        if highs[i..j_peak].iter().any(|&high| high > peak_high) {
            continue;
        }
        let run_up = (peak_high - lows[i]) / lows[i];
        if run_up < 0.90 {
            continue;
        }
        // --- STAGE 1 ("Flag"): the first 90% pole of the window.
        if !is_flag {
            is_flag = true;
            flag_date = Some(dates[j_peak].clone());
            flag_start_date = Some(dates[i].clone());
            flag_peak_date = Some(dates[j_peak].clone());
        }

        // --- STAGE 2 ("Tight Flag") ---
        // No single-day open gap > 50% inside the pole.
        if gap_prefix[j_peak + 1] > gap_prefix[i + 1] {
            continue;
        }
        // Circuit candles <= 40% of the pole.
        let pole_candles = j_peak - i + 1;
        let circuit_count = circuit_prefix[j_peak + 1] - circuit_prefix[i];
        if (circuit_count as f64) / (pole_candles as f64) * 100.0 > 40.0 {
            continue;
        }
        // Breakout: the first close above the peak within 20 candles.  A flag
        // candle trading above the peak is a later candidate peak instead.
        let last_k = std::cmp::min(n - 1, j_peak + MAX_FLAG_CANDLES);
        let Some(k_idx) = (j_peak + 1..=last_k).find(|&k| closes[k] > peak_high) else {
            continue;
        };
        let flag = j_peak + 1..k_idx;
        if highs[flag.clone()].iter().any(|&high| high > peak_high) {
            continue;
        }
        // Flag depth: no flag low more than MAX_FLAG_DEPTH below the peak.
        let depth_floor = (1.0 - MAX_FLAG_DEPTH) * peak_high;
        if lows[flag].iter().any(|&low| low < depth_floor) {
            continue;
        }
        // Holding: the latest close is still above the peak.
        if closes[n - 1] <= peak_high {
            continue;
        }
        if best.is_none_or(|(best_k, best_j, _)| (k_idx, j_peak) > (best_k, best_j)) {
            best = Some((k_idx, j_peak, i));
        }
    }

    if let Some((k_idx, j_peak, i)) = best {
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
    /// First date of the exchange's last `flag_lookback` sessions.
    flag_window_start: String,
}

/// Latest session and the oldest acceptable last-candle date per exchange.
fn exchange_freshness(
    conn: &Connection,
    date_limit: &str,
    max_stale_sessions: usize,
    flag_lookback: usize,
) -> Result<HashMap<String, ExchangeFreshness>, Box<dyn std::error::Error>> {
    let stale_rank = max_stale_sessions + 1;
    let sql = format!(
        "SELECT Exchange, CAST(MAX(Date) AS VARCHAR),
                CAST(MIN(Date) FILTER (WHERE session_rank <= {stale_rank}) AS VARCHAR),
                CAST(MIN(Date) FILTER (WHERE session_rank <= {flag_lookback}) AS VARCHAR)
         FROM (
             SELECT Exchange, Date,
                    DENSE_RANK() OVER (PARTITION BY Exchange ORDER BY Date DESC) AS session_rank
             FROM (SELECT DISTINCT Exchange, Date FROM eod_data WHERE Date <= {date_limit})
         )
         WHERE session_rank <= {}
         GROUP BY Exchange",
        stale_rank.max(flag_lookback)
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
                flag_window_start: row.get(3)?,
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
    let freshness = exchange_freshness(
        &conn,
        &date_limit,
        options.max_stale_sessions,
        options.flag_lookback,
    )?;
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
                let window_start = freshness
                    .get(&data.exchange)
                    .map(|limits| limits.flag_window_start.as_str());
                process_flag_screener(
                    data,
                    options.flag_lookback,
                    window_start,
                    indicators.as_ref(),
                )
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

        /// A pole from `start` (low `low`) to `peak` (high `high`, close
        /// `peak_close`).  The candles between close at low + 1 and their
        /// highs stay below 1.5 x low, so none of them is a 90% peak itself.
        fn pole(&mut self, start: usize, peak: usize, low: f64, high: f64, peak_close: f64) {
            self.set(start, low + 1.0, low + 2.0, low, low + 1.0);
            let step = (0.5 * low - 3.0) / (peak - start) as f64;
            for local in start + 1..peak {
                let candle_high = low + 3.0 + step * (local - start - 1) as f64;
                self.set(local, low + 1.0, candle_high, low + 0.5, low + 1.0);
            }
            self.set(peak, low + 1.0, high, low + 0.5, peak_close);
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

    /// Pole A: start 20 (low 45), peak 30 (high 100), flag 31-37 with lows
    /// `a_flag_low`, breakout 38, holding at 103.5.  Pole B: start 100 (low
    /// 102), peak 110 (high 200), flag 111..`b_breakout` with lows
    /// `b_flag_low`, breakout `b_breakout` (close 205), holding at 206.
    fn two_pole_layout(a_flag_low: f64, b_flag_low: f64, b_breakout: usize) -> Bars {
        let mut bars = Bars::new();
        bars.pole(20, 30, 45.0, 100.0, 92.0);
        bars.flat(31..=37, 90.0, 95.0, a_flag_low);
        bars.set(38, 90.0, 106.0, 89.0, 101.0);
        bars.flat(39..=99, 103.5, 104.0, 103.0);
        bars.pole(100, 110, 102.0, 200.0, 192.0);
        bars.flat(111..=b_breakout - 1, 190.0, 195.0, b_flag_low);
        bars.set(b_breakout, 190.0, 210.0, 189.0, 205.0);
        bars.flat(b_breakout + 1..=199, 206.0, 208.0, 205.0);
        bars
    }

    fn screen(bars: Bars) -> Option<super::FlagResult> {
        process_flag_screener(&bars.data(), LOOKBACK, None, None)
    }

    /// (start, peak, breakout) of the reported pole.
    fn pole_dates(result: &super::FlagResult) -> (Option<String>, Option<String>, Option<String>) {
        (
            result.tight_start_date.clone(),
            result.tight_peak_date.clone(),
            result.breakout_date.clone(),
        )
    }

    fn local_pole(
        start: usize,
        peak: usize,
        breakout: usize,
    ) -> (Option<String>, Option<String>, Option<String>) {
        (local_date(start), local_date(peak), local_date(breakout))
    }

    #[test]
    fn tight_flag_reports_the_pole_with_the_latest_breakout() {
        let result = screen(two_pole_layout(90.0, 180.0, 118)).unwrap();

        // flag_* describe the first 90% pole of the window (pole A).
        assert_eq!(result.flag_start_date, local_date(20));
        assert_eq!(result.flag_peak_date, local_date(30));
        // Pole A also qualifies (breakout 38); the fresh pole B wins.
        assert_eq!(pole_dates(&result), local_pole(100, 110, 118));
        assert_eq!(result.tight_flag_date, local_date(110));
    }

    /// A 10-candle pole breaking out 8 candles after its peak: under the old
    /// definition a breakout had to come more than 45 candles after the start.
    #[test]
    fn a_ten_candle_v_pole_qualifies() {
        let mut bars = Bars::new();
        for (step, local) in (10..20).enumerate() {
            let close = 50.0 - 0.4 * step as f64;
            bars.set(local, close, close + 0.5, close - 0.5, close);
        }
        bars.pole(20, 30, 45.0, 100.0, 92.0);
        bars.flat(31..=37, 90.0, 95.0, 90.0);
        bars.set(38, 90.0, 106.0, 89.0, 101.0);
        bars.flat(39..=199, 103.5, 104.0, 103.0);
        let result = screen(bars).unwrap();

        assert_eq!(pole_dates(&result), local_pole(20, 30, 38));
    }

    #[test]
    fn tight_flag_depth_boundary_is_exactly_80_percent_of_the_peak() {
        assert_eq!(MAX_FLAG_DEPTH, 0.20);
        // B's flag low exactly 80% of 200 passes.
        let result = screen(two_pole_layout(90.0, 160.0, 118)).unwrap();
        assert_eq!(pole_dates(&result), local_pole(100, 110, 118));

        // 79.99% fails: the fresh pole is rejected and the older pole A,
        // whose flag low is exactly 80% of 100, is reported instead.
        let result = screen(two_pole_layout(80.0, 159.98, 118)).unwrap();
        assert_eq!(pole_dates(&result), local_pole(20, 30, 38));

        // Both at 79.99%: nothing qualifies.
        assert!(screen(two_pole_layout(79.99, 159.98, 118)).is_none());
    }

    #[test]
    fn deep_lows_on_the_peak_or_breakout_candle_are_not_flag_lows() {
        let mut bars = two_pole_layout(90.0, 180.0, 118);
        bars.set(110, 103.0, 200.0, 150.0, 192.0); // peak candle low 75% of 200
        bars.set(118, 190.0, 210.0, 150.0, 205.0); // breakout candle low 75%
        let result = screen(bars).unwrap();

        assert_eq!(pole_dates(&result), local_pole(100, 110, 118));
    }

    #[test]
    fn breakout_may_come_20_but_not_21_candles_after_the_peak() {
        let result = screen(two_pole_layout(90.0, 180.0, 130)).unwrap();
        assert_eq!(pole_dates(&result), local_pole(100, 110, 130));

        // B breaks out one candle too late; only pole A remains.
        let result = screen(two_pole_layout(90.0, 180.0, 131)).unwrap();
        assert_eq!(pole_dates(&result), local_pole(20, 30, 38));
    }

    #[test]
    fn a_fast_breakout_needs_no_flag_candles() {
        // B: flag 111-113 (3 candles), breakout 114.
        let result = screen(two_pole_layout(90.0, 180.0, 114)).unwrap();
        assert_eq!(pole_dates(&result), local_pole(100, 110, 114));

        // B closes above its peak after 2 flag candles, or on the very next
        // candle: still B's breakout.
        let result = screen(two_pole_layout(90.0, 180.0, 113)).unwrap();
        assert_eq!(pole_dates(&result), local_pole(100, 110, 113));
        let result = screen(two_pole_layout(90.0, 180.0, 111)).unwrap();
        assert_eq!(pole_dates(&result), local_pole(100, 110, 111));
    }

    /// The window starts on the exchange's session date given, however many
    /// candles the symbol has before it.
    #[test]
    fn the_flag_window_starts_at_the_given_session() {
        let layout = || two_pole_layout(90.0, 159.98, 118); // B fails the depth
        let data = layout().data();
        let result = process_flag_screener(&data, LOOKBACK, None, None).unwrap();
        assert_eq!(pole_dates(&result), local_pole(20, 30, 38));

        let start = local_date(95).unwrap();
        assert!(process_flag_screener(&data, LOOKBACK, Some(start.as_str()), None).is_none());

        // A window start after the last candle leaves nothing to screen.
        assert!(process_flag_screener(&data, LOOKBACK, Some("2099-01-01"), None).is_none());
    }

    #[test]
    fn pole_start_is_the_lowest_low_45_to_4_candles_before_the_peak() {
        let mut bars = two_pole_layout(90.0, 180.0, 118);
        bars.lows[PREFIX + 64] = 90.0; // 46 candles before the peak: outside
        bars.lows[PREFIX + 103] = 101.0;
        bars.lows[PREFIX + 106] = 101.0; // ties with 103: the later one wins
        bars.lows[PREFIX + 107] = 95.0; // 3 candles before the peak: outside
        let result = screen(bars).unwrap();

        assert_eq!(pole_dates(&result), local_pole(106, 110, 118));
    }

    #[test]
    fn a_higher_high_inside_the_flag_becomes_the_peak() {
        let mut bars = two_pole_layout(90.0, 180.0, 118);
        bars.set(113, 190.0, 202.0, 180.0, 190.0);
        let result = screen(bars).unwrap();

        // 110 no longer qualifies (a flag candle trades above it); 113 does.
        assert_eq!(pole_dates(&result), local_pole(100, 113, 118));
    }

    #[test]
    fn peaks_sharing_a_breakout_report_the_later_peak() {
        let mut bars = two_pole_layout(90.0, 180.0, 118);
        bars.set(113, 190.0, 200.0, 180.0, 190.0); // equals the 110 high
        let result = screen(bars).unwrap();

        assert_eq!(pole_dates(&result), local_pole(100, 113, 118));
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

    /// The 252 high (220) is above the close; the signal is still 251.
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

    /// New highs at 260 and 265 do not move the signal off 251.
    #[test]
    fn mark_tight_keeps_the_first_signal_of_the_trend() {
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

        assert_eq!(result.mark_tight_date, "2024-251");
    }

    /// The signal (252, level 300) stays listed after the close falls back
    /// below the Donchian level, as long as it stays above EMA 50.
    #[test]
    fn mark_tight_stays_listed_below_the_donchian_level() {
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
        assert!(data.closes[251] < indicators.donchian_60[251]); // template, no breakout
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-252");
    }

    /// Only a close below EMA 50 ends the trend: a latest candle that fails
    /// the template otherwise (below 80% of the 52-week high) stays listed.
    #[test]
    fn a_latest_candle_failing_the_template_above_ema_50_stays_listed() {
        let mut closes = vec![100.0; 251];
        closes.extend_from_slice(&[200.0; 15]); // 251-265
        closes.extend_from_slice(&[155.0; 4]); // 266-269
        let highs = closes.clone();
        let data = symbol_data(closes, highs);
        let indicators = calculate_mark_indicators(&data);
        assert!(!super::mark_condition(&data, &indicators, 269));
        assert!(data.closes[269] > indicators.ema_50[269]);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-251");
    }

    /// A close equal to EMA 50 is not below it.
    #[test]
    fn a_close_equal_to_ema_50_does_not_end_the_trend() {
        let mut closes = vec![100.0; 251];
        closes.extend_from_slice(&[200.0; 15]); // 251-265
        let mut highs = closes.clone();
        let data = symbol_data(closes.clone(), highs.clone());
        // A close equal to the previous EMA leaves the EMA unchanged.
        let ema_50 = calculate_mark_indicators(&data).ema_50[264];
        closes[265] = ema_50;
        highs[265] = ema_50;
        let data = symbol_data(closes, highs);
        let indicators = calculate_mark_indicators(&data);
        assert_eq!(data.closes[265], indicators.ema_50[265]);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-251");
    }

    /// Signal at 251; the 260 close (50) is below EMA 50 and ends it.
    /// `after_260` holds the closes from 261 on.
    fn trend_broken_at_260(after_260: &[f64]) -> SymbolData {
        let mut closes = vec![100.0; 251];
        closes.extend_from_slice(&[200.0; 9]); // 251-259
        closes.push(50.0); // 260
        closes.extend_from_slice(after_260);
        let highs = closes.clone();
        symbol_data(closes, highs)
    }

    #[test]
    fn a_close_below_ema_50_ends_the_signal() {
        let data = trend_broken_at_260(&[150.0; 9]); // below the 200 level
        let indicators = calculate_mark_indicators(&data);
        assert!(data.closes[260] < indicators.ema_50[260]);

        assert!(process_mark_screener(&data, &indicators).is_none());
    }

    #[test]
    fn after_a_close_below_ema_50_the_next_breakout_is_a_new_signal() {
        let mut after = vec![150.0; 4]; // 261-264
        after.extend_from_slice(&[210.0; 5]); // 265-269 above the 200 level
        let data = trend_broken_at_260(&after);
        let indicators = calculate_mark_indicators(&data);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-265");
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

    /// The signal needs the template and the Donchian breakout on the same
    /// candle: the breakouts from 251 count only once the template holds.
    #[test]
    fn the_signal_is_the_first_candle_with_template_and_breakout() {
        let rising: Vec<f64> = (0..19).map(|step| 200.0 + step as f64).collect();
        let data = streak_from_before_the_run(&rising);
        let indicators = calculate_mark_indicators(&data);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-262");
    }

    #[test]
    fn a_dip_that_stays_above_ema_50_does_not_move_the_signal() {
        let mut path: Vec<f64> = (0..15).map(|step| 200.0 + step as f64).collect(); // 251-265
        path.extend_from_slice(&[205.0, 220.0, 221.0, 222.0]); // 266 dips, 267 new high
        let data = streak_from_before_the_run(&path);
        let indicators = calculate_mark_indicators(&data);
        assert!(data.closes[266] > indicators.ema_50[266]);
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-262");
    }

    /// A trend already running when the indicators begin is signalled on the
    /// first candle they can judge (251, the first 52-week value).
    #[test]
    fn a_trend_through_the_warm_up_is_signalled_at_the_first_judged_candle() {
        let mut closes = vec![100.0; 240];
        closes.extend((0..30).map(|step| 150.0 + step as f64)); // 240-269
        let highs = closes.clone();
        let data = symbol_data(closes, highs);
        let indicators = calculate_mark_indicators(&data);
        assert!(!super::mark_inputs_finite(&indicators, 250));
        let result = process_mark_screener(&data, &indicators).unwrap();

        assert_eq!(result.mark_tight_date, "2024-251");
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
                        100 * POW(1.003, i) * dip AS Open,
                        100 * POW(1.003, i) * dip * 1.001 AS High,
                        100 * POW(1.003, i) * dip * 0.99 AS Low,
                        100 * POW(1.003, i) * dip AS Close,
                        1000.0 AS Volume
                 -- The steady rise signals at 251; the 1% dip at 270-274 stays
                 -- above EMA 50 and changes nothing.
                 FROM series, range(0, 300) AS r(i),
                      LATERAL (SELECT CASE WHEN i BETWEEN 270 AND 274 THEN 0.99 ELSE 1.0 END AS dip)
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

    /// NSE's latest session is 2027-03-11, BSE's 30 days earlier: each
    /// exchange keeps its own last 700 days.
    #[test]
    fn history_anchor_is_per_exchange() {
        let root = std::env::temp_dir().join(format!(
            "mark-history-exchanges-{}-{}",
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
            "Name,ISIN Code\nN,INE555E01015\nB,INE666F01016\n",
        )
        .unwrap();
        {
            let conn = duckdb::Connection::open(&database).unwrap();
            conn.execute_batch(
                "CREATE TABLE eod_data AS
                 WITH series(Symbol, Exchange, ISIN, sessions) AS (
                     VALUES ('NSEONLY', 'NSE', 'INE555E01015', 800),
                            ('BSEONLY', 'BSE', 'INE666F01016', 770)
                 )
                 SELECT CAST(DATE '2025-01-01' + CAST(i AS INTEGER) AS DATE) AS Date,
                        Symbol, Exchange, ISIN,
                        100.0 AS Open, 101.0 AS High, 99.0 AS Low, 100.0 AS Close,
                        1000.0 AS Volume
                 FROM series, range(0, 800) AS r(i)
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

        let first_date = |exchange: &str| {
            let data = run
                .symbol_datasets
                .iter()
                .find(|data| data.exchange == exchange)
                .unwrap();
            (data.dates.first().cloned().unwrap(), data.dates.len())
        };
        assert_eq!(
            run.datasets["BSE"].latest_session.as_deref(),
            Some("2027-02-09")
        );
        assert_eq!(first_date("NSE"), ("2025-04-10".to_string(), 701));
        assert_eq!(first_date("BSE"), ("2025-03-11".to_string(), 701));
    }

    #[test]
    fn the_flag_window_starts_at_the_nth_latest_session() {
        let conn = duckdb::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE eod_data AS
             SELECT CAST(DATE '2025-01-01' + CAST(i AS INTEGER) AS DATE) AS Date,
                    'NSE' AS Exchange, 'A' AS Symbol
             FROM range(0, 10) AS r(i)",
        )
        .unwrap();
        let freshness = super::exchange_freshness(&conn, "DATE '9999-12-31'", 1, 3).unwrap();
        let nse = &freshness["NSE"];
        assert_eq!(nse.latest, "2025-01-10");
        assert_eq!(nse.cutoff, "2025-01-09");
        assert_eq!(nse.flag_window_start, "2025-01-08");

        // More sessions asked for than stored: the whole store.
        let freshness = super::exchange_freshness(&conn, "DATE '9999-12-31'", 1, 50).unwrap();
        assert_eq!(freshness["NSE"].flag_window_start, "2025-01-01");
    }

    /// A thinly traded symbol (every second session after local 40) has its
    /// only qualifying pole (A, peak local 30) inside its own last 190
    /// candles but outside the exchange's last 190 sessions.
    #[test]
    fn screen_market_uses_the_exchange_session_window_for_flags() {
        let root = std::env::temp_dir().join(format!(
            "mark-flag-window-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let database = root.join("prices.duckdb");
        let fundamentals = root.join("fundamentals.csv");
        std::fs::write(&fundamentals, "Name,ISIN Code\nThin,INE000THIN1\n").unwrap();

        let bars = two_pole_layout(90.0, 159.98, 118); // pole B fails the depth
        let first = chrono::NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
        let day_of = |index: usize| {
            let local = index as i64 - PREFIX as i64;
            let offset = if local < 40 {
                index as i64
            } else {
                PREFIX as i64 + 40 + 2 * (local - 40)
            };
            first + chrono::Duration::days(offset)
        };
        let mut rows = Vec::new();
        for index in 0..bars.closes.len() {
            rows.push(format!(
                "(DATE '{}', 'THIN', 'NSE', 'INE000THIN1', {}, {}, {}, {}, 1000.0)",
                day_of(index).format("%Y-%m-%d"),
                bars.opens[index],
                bars.highs[index],
                bars.lows[index],
                bars.closes[index]
            ));
        }
        let last = day_of(bars.closes.len() - 1);
        {
            let conn = duckdb::Connection::open(&database).unwrap();
            conn.execute_batch(&format!(
                "CREATE TABLE eod_data (Date DATE, Symbol VARCHAR, Exchange VARCHAR, ISIN VARCHAR,
                     Open DOUBLE, High DOUBLE, Low DOUBLE, Close DOUBLE, Volume DOUBLE);
                 INSERT INTO eod_data VALUES {};
                 -- A liquid symbol trading every day defines the sessions.
                 INSERT INTO eod_data
                 SELECT CAST(DATE '2024-01-01' + CAST(i AS INTEGER) AS DATE), 'LIQ', 'NSE',
                        'INE000LIQ01', 10, 10, 10, 10, 1000.0
                 FROM range(0, {}) AS r(i);",
                rows.join(", "),
                (last - first).num_days() + 1
            ))
            .unwrap();
        }
        let screen = |flag_lookback: usize| {
            let options = ScreenOptions {
                flag_lookback,
                tight_flag_only: true,
                ..ScreenOptions::default()
            };
            screen_market(
                database.to_str().unwrap(),
                fundamentals.to_str().unwrap(),
                "NSE",
                &options,
            )
            .unwrap()
            .datasets["NSE"]
                .tight_flag_qualified
                .iter()
                .map(pole_dates)
                .collect::<Vec<_>>()
        };
        let pole_a = (
            Some(day_of(PREFIX + 20).format("%Y-%m-%d").to_string()),
            Some(day_of(PREFIX + 30).format("%Y-%m-%d").to_string()),
            Some(day_of(PREFIX + 38).format("%Y-%m-%d").to_string()),
        );
        let wide = screen(600);
        let narrow = screen(190);
        std::fs::remove_dir_all(&root).ok();

        assert_eq!(wide, vec![pole_a]);
        assert!(narrow.is_empty());
    }
}
