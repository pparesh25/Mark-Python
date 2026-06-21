# Mark Screener — Change & Optimisation Guide

**Scope:** implementation spec for two changes to the manual screening suite in `Strong/mark/`, plus recommended optimisations. To be executed later in a code session.
**Companion:** mirrors the EMA Trend Template strategy change (`ema_trend_template.txt` v4.2, `breakout_window=5`).

---

## 0. Locked decisions

1. **`Mark_Tight_Date` under the 5‑day window** → the **most‑recent breakout day within the window** (so `Days_In_Trend` reflects the latest actual breakout). ✅
2. **Priority score** → added to **both** outputs (`mark` and `mark_tight`), each **ranked within its own qualified‑set**. ✅
3. **Path‑config + optional‑charts** → documented here as recommendations (§5); **decision deferred to the code session** (same project folder). ⏸

---

## 1. Current pipeline (as‑is)

`run_complete_screening.sh` runs 3 steps:

1. **EOD update** — `eod2-main/run_eod2.sh` (separate project) → daily OHLCV CSVs in `eod2_data/daily/`.
2. **Fundamentals** — `screener_all_fundamental.sh` → `screener_all_fundamental.py --headless` (Selenium logs into screener.in, exports **screen 3427564 "all‑company"** → `~/Downloads/all-company.csv`). The column set is whatever the screen is configured with — now the **rich 62‑column** set (Volume 1m, Change FII/DII, Return 1y/3m, Altman Z, etc.).
3. **Screening** — `Mark screener.py`:
   - Per symbol: EMA 50/150/200, 52w high/low (252), `Donchian_60_High = MAX(high.shift(1), 60)`.
   - **Stage‑1 "Mark"**: EMA stack `>` + `close ≥ 0.80×52wH` (20%) + `close ≥ 1.50×52wL`. → `Mark_Date` (trend start, via reverse walk).
   - **Stage‑2 "Mark Tight"**: EMA stack + `close ≥ 0.85×52wH` (15%) + **`close > Donchian_60_High`** (breakout **today**) + `close ≥ 1.50×52wL`. → `Mark_Tight_Date`.
   - `process_results()` merges with `all-company.csv` on `NSE Code`, computes `Days_In_Trend`, `Trend_Strength`, exports `{date}_{suffix}_qualified.csv`, renders a chart per stock, writes an AmiBroker `.tls`.

The merged tight output today is **75 columns** = 13 technical + 62 fundamental.

---

## 2. Code review — opinion

**Strengths:** clean two‑stage structure; `ProcessPoolExecutor` parallelism; fundamentals loaded once; 500‑row tail; `Donchian shift(1)` is correct.

**Issues / optimise:**

| # | Issue | Impact | Fix |
|---|-------|--------|-----|
| 1 | **Hard‑coded + stale paths** — `Mark screener.py` & the orchestrator still point at `/Users/paresh/Desktop/Mark Python/…`, `eod2-main`, `~/Downloads/all-company.csv`, AmiBroker wine path. Files now live in `Strong/mark/`. | Scripts won't run as‑is. | One `CONFIG` block / small `config.py`/JSON at top (see §5). |
| 2 | **Chart generation is the bottleneck** — one matplotlib PNG per qualified stock, re‑reading each CSV. | Slow on big match‑days. | `--charts/--no-charts` flag (see §5). |
| 3 | `Down from 52w high` `_x/_y` suffix (computed + fundamental both present). | Minor confusion. | Drop one; keep fundamental's. |
| 4 | Each CSV read twice (`process_file` + `plot_save_chart`). | Minor. | Optional cache; low priority. |

Both functional changes (§3, §4) are **cleanly localised and low‑risk**.

---

## 3. Change #1 — 5‑day breakout window (fixed base, Option X)

**Goal:** a stock qualifies if it broke out (close above the 60‑day base) on **any of the last 5 days**, not only today. The reference base **excludes** the last 5 days (frozen, pre‑breakout). `N=1` reproduces the current "today‑only" rule.

### 3.1 Add a parameter (top of `Mark screener.py`)
```python
BREAKOUT_WINDOW = 5   # breakout qualifies if it occurred on ANY of the last N days; N=1 = today-only
```

### 3.2 New base column (replaces the meaning of line 73)
```python
# was: df['Donchian_60_High'] = ta.MAX(df['high'].shift(1), timeperiod=60)
df['Donchian_60_Base'] = ta.MAX(df['high'].shift(BREAKOUT_WINDOW), timeperiod=60)
# 60-day high of highs, EXCLUDING the last BREAKOUT_WINDOW days (frozen base)
```
> At the last bar this equals `max(high[-(60+N):-N])` — matches the strategy's `max(high[-(60+bw):-bw])`.

### 3.3 Replace the tight reverse‑walk (lines 94‑106) with a direct window computation
Inside `process_file`, after the Mark loop, compute the flag and the breakout date directly (no loop needed for tight):
```python
base_last = df['Donchian_60_Base'].iloc[-1]
window    = df.tail(BREAKOUT_WINDOW)
bo_mask   = window['close'] > base_last
tight_breakout    = bool(bo_mask.any())
# Decision #1: most-recent breakout day in the window
first_match_date_tight = window.loc[bo_mask, 'date'].max() if tight_breakout else pd.NaT
```
Then on `last_row` (around lines 112‑114):
```python
last_row['Mark_Tight_Date'] = first_match_date_tight
last_row['Tight_Breakout']  = tight_breakout
```
> Keep `if pd.isna(first_match_date_mark): return None` (line 108) — Stage‑1 still gates entry into `results_all`.

### 3.4 Update the Stage‑2 final filter (lines 314‑318)
```python
results_stage2 = results_all[
    (results_all['close'] >= results_all['52 week high'] * 0.85) &
    (results_all['Tight_Breakout'] == True) &           # was: close > Donchian_60_High
    (~results_all['Mark_Tight_Date'].isna())
].copy()
```

### 3.5 Save as a NEW file
Create e.g. **`Mark screener_5dbreakout.py`** (don't overwrite the original until A/B‑checked). Point `Mark_screener.sh` at it when ready.

---

## 4. Change #2 — Priority score + Tradeable flag in the output

**Goal:** embed the winner‑centric ranking (the combined‑watchlist logic) into the pipeline so every `*_qualified.csv` carries `Priority_Score` and `Tradeable`. The rich columns are now present post‑merge, so this lives entirely inside `process_results()`.

### 4.1 Scoring helper (add near top of `Mark screener.py`)
```python
import numpy as np

def add_priority_score(df):
    """Winner-centric score + hard-filter Tradeable flag, ranked WITHIN this qualified set."""
    def pr(s):                                   # percentile rank 0-100, NaN -> median
        s = pd.to_numeric(s, errors='coerce'); s = s.fillna(s.median())
        return s.rank(pct=True) * 100
    price = pd.to_numeric(df['Current Price'], errors='coerce')
    de    = pd.to_numeric(df['Debt to equity'], errors='coerce')
    turn  = (pd.to_numeric(df['Volume 1month average'], errors='coerce') * price) / 1e7   # ₹ cr/day

    mom   = (pr(df['Return over 1year']) + pr(df['Return over 3months'])) / 2
    qual  = (pr(df['Return on capital employed']) + pr(df['Return on equity'])
             + pr(-de) + pr(df['Piotroski score']) + pr(df['Altman Z Score'])) / 5
    fuel  = (pr(df['YOY Quarterly sales growth']) + pr(df['YOY Quarterly profit growth'])) / 2
    spons = (pr(df['Change in FII holding']) + pr(df['Change in DII holding'])) / 2
    liq   = pr(np.log1p(turn.clip(lower=0)))

    df['Priority_Score'] = (0.30*mom + 0.28*qual + 0.22*fuel + 0.12*spons + 0.08*liq).round(1)
    df['Turnover_cr']    = turn.round(1)
    pledge = pd.to_numeric(df['Pledged percentage'], errors='coerce').fillna(0)
    altman = pd.to_numeric(df['Altman Z Score'], errors='coerce')
    df['Tradeable'] = np.where((turn >= 2.0) & (de <= 1.0) & (pledge <= 15) & (altman >= 1.8), 'Y', 'N')
    return df
```

### 4.2 Call it inside `process_results()` (after the merge, ~line 240)
```python
merged_df = merged_df.dropna(subset=['NSE Code'])
if merged_df.empty: ...
merged_df = add_priority_score(merged_df)      # <-- add here (ranks within this set)
```

### 4.3 Surface the columns + optional sort
- In `csv_columns` (line 258) put the new fields right after `Trend_Strength`:
  ```python
  csv_columns = ['Index', 'symbol', 'Priority_Score', 'Tradeable', 'Turnover_cr',
                 'Days_In_Trend', 'Trend_Strength']
  ```
  (They're new `df` columns, so also confirm they aren't dropped by the `fundamental_cols` exclude list — add them to the exclude list to avoid duplication, since they're already listed explicitly.)
- **Optional (recommended):** rank the output by score. Change the sort (line 247) to:
  ```python
  df_sorted = merged_df.sort_values(by=['Priority_Score'], ascending=False)
  ```
  …or keep `date_col` primary and `Priority_Score` secondary — your call in the code session.

### 4.4 Applies to BOTH outputs
`process_results()` is called for `mark` (line 303) and `mark_tight` (line 322); since the score is computed inside it, **both** files get ranked within their own set automatically. ✅ (decision #2)

---

## 5. Recommended optimisations (decision deferred — §0.3)

> Handle in the code session, in the same project folder.

**A. Centralise paths** — replace the scattered hard‑codes (lines 18‑26 + orchestrator 21‑24) with one block / file:
```python
# config.py (or top-of-file CONFIG dict)
BASE        = Path('/Users/paresh/Desktop/Strong/mark')        # <- update to real root
DATA_DIR    = Path('/Users/paresh/Desktop/eod2-main/src/eod2_data/daily')
FUNDAMENTAL = Path('/Users/paresh/Downloads/all-company.csv')
OUT_DIR     = BASE / 'qualified_stocks'
CHART_DIR   = BASE / 'chart_images'
AMIBROKER   = Path('/Users/paresh/.wine/drive_c/.../WatchLists')
```
Fixes the stale `Mark Python` paths so the suite runs from `Strong/mark/`.

**B. Optional charts** — add an arg so screening can skip the slow PNG render:
```python
import argparse
ap = argparse.ArgumentParser(); ap.add_argument('--no-charts', action='store_true')
args = ap.parse_args()
...
if not args.no_charts:
    # the ProcessPoolExecutor chart block (lines 271-278)
```
Big speed win when you only need the CSV.

---

## 6. Pre‑flight checklist (code session)

- [ ] Confirm exact column names in the live `all-company.csv` match those used in §4.1 (`Return over 1year`, `Return over 3months`, `YOY Quarterly sales/profit growth`, `Change in FII holding`, `Change in DII holding`, `Altman Z Score`, `Volume 1month average`, `Pledged percentage`, `Current Price`, `Debt to equity`, `Piotroski score`, `Return on capital employed/equity`).
- [ ] `BREAKOUT_WINDOW = 5`; verify `Donchian_60_Base` needs ≥ 60+5 bars (already satisfied by the 252‑bar 52w window).
- [ ] A/B: run old vs new screener on the same data — compare tight‑match counts (expect more with N=5) and confirm none fell >15 % off high (sanity, like 19‑Jun where all 16 add‑ons were within 0‑10 %).
- [ ] Verify `Priority_Score` / `Tradeable` appear in both `mark` and `mark_tight` CSVs and rank sensibly.
- [ ] Decide §5 (paths + charts) and the output sort order.
- [ ] Keep the original `Mark screener.py` intact; ship the change as `Mark screener_5dbreakout.py`.

---

## 7. Summary

Two localised edits to `Mark screener.py` (saved as a new file): **(1)** swap the today‑only Donchian check for a **5‑day fixed‑base breakout window** (`BREAKOUT_WINDOW`, `shift(N)` base, "any of last N closes > base", `Mark_Tight_Date` = most‑recent breakout day); **(2)** add an `add_priority_score()` step inside `process_results()` so **both** outputs carry `Priority_Score` + `Tradeable`, ranked within each set. Plus recommended path‑centralisation and an optional‑charts flag (deferred). Low risk, no change to the orchestrator logic — only the screener script.
