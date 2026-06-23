"""Mark Screener with 5-day breakout window + Priority Score.

Derived from ``Mark screener.py`` with two changes applied per
``mark_screener_changes_guide.md``:

  §3  — Stage-2 "Mark Tight" now qualifies if the stock broke above the
        60-day Donchian base on ANY of the last ``BREAKOUT_WINDOW`` days
        (frozen base, excludes those days). ``N=1`` reproduces the original
        "today-only" rule.

  §4  — ``add_priority_score()`` runs inside ``process_results()`` so BOTH
        outputs (mark and mark_tight) carry ``Priority_Score``,
        ``Tradeable`` and ``Turnover_cr``, ranked within their own set.

The original ``Mark screener.py`` is left untouched for A/B comparison.
Run via the unified orchestrator ``run_screener.py``.
"""

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
import talib as ta

import matplotlib
matplotlib.use('Agg')  # non-interactive backend
import matplotlib.pyplot as plt
import mplfinance as mpf

import config
from config import (
    DATA_DIR, MARK_CHART_DIR, MARK_TIGHT_CHART_DIR,
    MARK_OUT_DIR, MARK_TIGHT_OUT_DIR,
    FUNDAMENTAL_FILE, AMIBROKER_WATCHLIST_DIR,
    BREAKOUT_WINDOW, CURRENT_DATE, CURRENT_TIMESTAMP,
)

# ---------------------------------------------------------------------------
# Timing + inputs (mirror the original module-level behaviour)
# ---------------------------------------------------------------------------
start_time = time.time()

# Create output folders up front (same as original lines 32-35)
config.ensure_dirs()

# List all the CSV files in the data directory
files = [f.name for f in DATA_DIR.glob('*.csv') if f.name != 'isin.csv']


# ===========================================================================
# §4.1 — Priority score helper
# ===========================================================================
def add_priority_score(df):
    """Winner-centric score + hard Tradeable flag, ranked WITHIN this set.

    Computed inside ``process_results()`` AFTER the fundamentals merge, so the
    rich columns from all-company.csv are available. Applied to BOTH outputs.
    """
    def pr(s):
        # percentile rank 0-100, NaN -> median
        s = pd.to_numeric(s, errors='coerce')
        s = s.fillna(s.median())
        return s.rank(pct=True) * 100

    price = pd.to_numeric(df['Current Price'], errors='coerce')
    de = pd.to_numeric(df['Debt to equity'], errors='coerce')
    # ₹ cr/day turnover
    turn = (pd.to_numeric(df['Volume 1month average'], errors='coerce') * price) / 1e7

    mom = (pr(df['Return over 1year']) + pr(df['Return over 3months'])) / 2
    qual = (pr(df['Return on capital employed']) + pr(df['Return on equity'])
            + pr(-de) + pr(df['Piotroski score']) + pr(df['Altman Z Score'])) / 5
    fuel = (pr(df['YOY Quarterly sales growth']) + pr(df['YOY Quarterly profit growth'])) / 2
    spons = (pr(df['Change in FII holding']) + pr(df['Change in DII holding'])) / 2
    liq = pr(np.log1p(turn.clip(lower=0)))

    df['Priority_Score'] = (0.30 * mom + 0.28 * qual + 0.22 * fuel
                            + 0.12 * spons + 0.08 * liq).round(1)
    df['Turnover_cr'] = turn.round(1)
    pledge = pd.to_numeric(df['Pledged percentage'], errors='coerce').fillna(0)
    altman = pd.to_numeric(df['Altman Z Score'], errors='coerce')
    df['Tradeable'] = np.where(
        (turn >= 2.0) & (de <= 1.0) & (pledge <= 15) & (altman >= 1.8),
        'Y', 'N'
    )
    return df


# ===========================================================================
# Per-file processing
# ===========================================================================
def process_file(file):
    try:
        if file.endswith('.csv'):
            symbol = file.replace('.csv', '').upper()

            filepath = DATA_DIR / file
            # Optimization: keep only last 500 rows
            df = pd.read_csv(filepath, header=0)
            df = df.tail(500)

            df['symbol'] = symbol
            df.columns = df.columns.str.lower()
            df['date'] = pd.to_datetime(df['date'])

            # Moving averages
            df['EMA_50'] = ta.EMA(df['close'], timeperiod=50).round(2)
            df['EMA_150'] = ta.EMA(df['close'], timeperiod=150).round(2)
            df['EMA_200'] = ta.EMA(df['close'], timeperiod=200).round(2)

            # 52-week high / low
            df['52 week high'] = ta.MAX(df['high'], timeperiod=252)
            df['52 week low'] = ta.MIN(df['low'], timeperiod=252)

            # §3.2 — NEW frozen base column.
            # 60-day high of highs EXCLUDING the last BREAKOUT_WINDOW days.
            # At the last bar this equals max(high[-(60+N):-N]).
            df['Donchian_60_Base'] = ta.MAX(df['high'].shift(BREAKOUT_WINDOW), timeperiod=60)

            # Keep the original today-only column too (cheap, useful for
            # debugging / charting; the new logic uses Donchian_60_Base).
            df['Donchian_60_High'] = ta.MAX(df['high'].shift(1), timeperiod=60)

            # % down from 52w high
            df['Down from 52w high'] = (((df['52 week high'] - df['close'])
                                         / df['52 week high']) * 100).round(2)

            reversed_df = df.iloc[::-1]

            # 1. Mark Date (Stage 1: 20% buffer) — unchanged
            first_match_date_mark = pd.NaT
            for index, row in reversed_df.iterrows():
                if (pd.notna(row['EMA_50']) and pd.notna(row['EMA_150'])
                        and pd.notna(row['EMA_200'])
                        and pd.notna(row['52 week high'])
                        and pd.notna(row['52 week low'])):
                    ema_condition = row['close'] > row['EMA_50'] > row['EMA_150'] > row['EMA_200']
                    high_condition = row['close'] >= row['52 week high'] * 0.80
                    low_condition = row['close'] >= row['52 week low'] * 1.50

                    if ema_condition and high_condition and low_condition:
                        first_match_date_mark = row['date']
                    else:
                        break

            # §3.3 — Replace the Stage-2 reverse-walk with a direct window
            # computation. Qualifies if close broke above the frozen base on
            # ANY of the last BREAKOUT_WINDOW days; Mark_Tight_Date = the
            # most-recent such day.
            base_last = df['Donchian_60_Base'].iloc[-1]
            window = df.tail(BREAKOUT_WINDOW)
            bo_mask = window['close'] > base_last
            tight_breakout = bool(bo_mask.any())
            first_match_date_tight = (
                window.loc[bo_mask, 'date'].max() if tight_breakout else pd.NaT
            )

            # Stage-1 still gates entry — original behaviour preserved.
            if pd.isna(first_match_date_mark):
                return None

            last_row = df.iloc[[-1]].copy()
            last_row['Mark_Date'] = first_match_date_mark
            last_row['Mark_Tight_Date'] = first_match_date_tight
            last_row['Tight_Breakout'] = tight_breakout

            return last_row
    except Exception as e:
        logging.error(f"Error processing {file}: {e}")
        return None


# ===========================================================================
# Chart rendering (unchanged from original)
# ===========================================================================
def plot_save_chart(args):
    row, target_dir, date_col = args
    try:
        stock = row['symbol']
        idx_val = str(row['Index']) if 'Index' in row else "0"
        mark_date = row[date_col]

        stock_file = DATA_DIR / f"{stock.lower()}.csv"
        if not stock_file.exists():
            return

        stock_data = pd.read_csv(stock_file, header=0)
        stock_data = stock_data.tail(500)
        stock_data['Date'] = pd.to_datetime(stock_data['Date'])
        stock_data.set_index('Date', inplace=True)

        stock_data['EMA_50'] = ta.EMA(stock_data['Close'], timeperiod=50)
        stock_data['EMA_150'] = ta.EMA(stock_data['Close'], timeperiod=150)
        stock_data['EMA_200'] = ta.EMA(stock_data['Close'], timeperiod=200)

        stock_start_date = row['Plot Start']
        stock_end_date = row['Plot End']
        stock_data = stock_data.loc[stock_start_date:stock_end_date]

        if stock_data.empty:
            return

        from matplotlib.gridspec import GridSpec

        fig = plt.figure(figsize=(19.2, 10))
        fig.suptitle(stock, fontsize=18, fontweight='bold', y=0.995)

        gs = GridSpec(3, 1, figure=fig, height_ratios=[0.5, 2.5, 0.8],
                      hspace=0.02, top=0.96)

        ax_info = fig.add_subplot(gs[0])
        ax_info.axis('off')

        sf = row
        info_line1 = (
            f"{sf['Name']}  |  "
            f"Industry: {sf['Industry']}  |  "
            f"Price: ₹{sf['Current Price']}  |  "
            f"MCap: ₹{sf['Market Capitalization']} Cr  |  "
            f"PE: {sf.get('Price to Earning', 'N/A')}"
        )
        info_line2 = (
            f"ROE: {sf.get('Return on equity', 'N/A')}%  |  "
            f"ROCE: {sf.get('Return on capital employed', 'N/A')}%  |  "
            f"OPM: {sf.get('OPM', 'N/A')}%  |  "
            f"P/B: {sf.get('Price to book value', 'N/A')}  |  "
            f"Debt/Eq: {sf.get('Debt to equity', 'N/A')}"
        )
        info_line3 = (
            f"Sales 5Y: {sf['Sales growth 5Years']}%  |  "
            f"Profit 5Y: {sf['Profit growth 5Years']}%  |  "
            f"Promoter: {sf.get('Promoter holding', 'N/A')}%  |  "
            f"FII: {sf.get('FII holding', 'N/A')}%  |  "
            f"DII: {sf.get('DII holding', 'N/A')}%  |  "
            f"52w: -{sf.get('Down from 52w high_y', 0)}% / +{sf.get('Up from 52w low', 0)}%"
        )
        info_text = f"{info_line1}\n{info_line2}\n{info_line3}"

        ax_info.text(0.5, 0.5, info_text, transform=ax_info.transAxes,
                     fontsize=10, verticalalignment='center', horizontalalignment='center',
                     bbox=dict(boxstyle='round', facecolor='lightblue', alpha=0.3),
                     family='monospace', linespacing=1.8)

        ax_price = fig.add_subplot(gs[1])
        ax_volume = fig.add_subplot(gs[2], sharex=ax_price)

        ema_50 = mpf.make_addplot(stock_data['EMA_50'], ax=ax_price, color='blue', width=1.0)
        ema_150 = mpf.make_addplot(stock_data['EMA_150'], ax=ax_price, color='orange', width=1.0)
        ema_200 = mpf.make_addplot(stock_data['EMA_200'], ax=ax_price, color='red', width=1.0)

        mpf_style = mpf.make_mpf_style(base_mpf_style='charles',
                                       mavcolors=['blue', 'orange', 'red'])

        mpf.plot(stock_data, type='candle', style=mpf_style, ax=ax_price, volume=ax_volume,
                 addplot=[ema_50, ema_150, ema_200], ylabel='Price', ylabel_lower='Volume',
                 update_width_config=dict(volume_linewidth=0, volume_width=0.75))

        ax_volume.yaxis.tick_right()
        ax_volume.yaxis.set_label_position("right")

        ax_price.grid(True, linestyle=':', linewidth=0.3, color='gray', alpha=0.5)
        ax_volume.grid(True, linestyle=':', linewidth=0.3, color='gray', alpha=0.5)

        try:
            start_index = stock_data.index.get_loc(mark_date)
            ax_price.axvline(x=start_index, color='blue', linestyle=':', linewidth=0.8, alpha=0.7)
        except (KeyError, ValueError):
            pass

        plt.setp(ax_volume.xaxis.get_majorticklabels(), rotation=0, ha='center')

        image_name = f"{idx_val}_{stock}.png"
        image_path = target_dir / image_name
        plt.savefig(image_path, bbox_inches='tight', pad_inches=0.1, dpi=150)
        plt.close()
    except Exception as e:
        logging.error(f"Error plotting {row.get('symbol', 'unknown')}: {e}")


# ===========================================================================
# Results processing — with §4 priority score embedded
# ===========================================================================
def process_results(df_results, fundamental_df, suffix, out_dir, chart_dir, date_col):
    # Merge with fundamental data
    merged_df = pd.merge(df_results, fundamental_df, left_on='symbol',
                         right_on='NSE Code', how='left')
    merged_df = merged_df.dropna(subset=['NSE Code'])

    if merged_df.empty:
        logging.info(f"No stocks matched for {suffix}.")
        return None

    # §4.2 — compute priority score within this qualified set
    merged_df = add_priority_score(merged_df)

    merged_df = merged_df[~merged_df[date_col].isna()]
    # §4.3 — sort by Priority_Score descending (winner-centric ranking)
    df_sorted = merged_df.sort_values(by='Priority_Score', ascending=False)

    df_sorted['Plot Start'] = pd.to_datetime(df_sorted[date_col]) - pd.to_timedelta(500, unit='D')
    df_sorted['Plot End'] = pd.to_datetime(date.today())
    df_sorted['Days_In_Trend'] = (pd.to_datetime(df_sorted['date'])
                                  - pd.to_datetime(df_sorted[date_col])).dt.days
    df_sorted['Trend_Strength'] = (((df_sorted['EMA_50'] - df_sorted['EMA_200'])
                                    / df_sorted['EMA_200'] * 100).round(2))

    df_sorted = df_sorted.reset_index(drop=True)
    df_sorted['Index'] = df_sorted.index

    # §4.3 — new priority columns surfaced right after Trend_Strength.
    # They're listed explicitly AND excluded from fundamental_cols to avoid
    # duplication.
    csv_columns = ['Index', 'symbol', 'Priority_Score', 'Tradeable', 'Turnover_cr',
                   'Days_In_Trend', 'Trend_Strength']
    fundamental_cols = [col for col in df_sorted.columns if col not in [
        'Index', 'symbol', 'date', 'close', 'EMA_50', 'EMA_150', 'EMA_200',
        '52 week high', '52 week low', 'Mark_Date', 'Mark_Tight_Date',
        'Down from 52w high_x', 'Donchian_60_High', 'Donchian_60_Base',
        'Tight_Breakout',
        'Plot Start', 'Plot End', 'Days_In_Trend', 'Trend_Strength',
        'Priority_Score', 'Tradeable', 'Turnover_cr',
    ]]
    csv_columns.extend(fundamental_cols)

    # Filename carries a timestamp so a same-day evening run does not
    # overwrite the morning run's CSV (e.g. pre-open vs post-close screens).
    csv_filename = f"{CURRENT_TIMESTAMP}_{suffix}_qualified.csv"
    csv_filepath = out_dir / csv_filename
    df_sorted[csv_columns].to_csv(csv_filepath, index=False)
    logging.info(f"CSV exported: {csv_filepath} ({len(df_sorted)} stocks)")

    # Generate charts
    logging.info(f"Generating charts for {suffix} in {chart_dir}...")
    if chart_dir.exists():
        shutil.rmtree(chart_dir)
    chart_dir.mkdir(parents=True, exist_ok=True)

    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        list(executor.map(plot_save_chart,
                          [(row, chart_dir, date_col) for _, row in df_sorted.iterrows()]))

    return df_sorted


# ===========================================================================
# Main
# ===========================================================================
def main():
    logging.info(f"Stage 1: Running Mark Screening (BREAKOUT_WINDOW={BREAKOUT_WINDOW})...")
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        results_list = list(executor.map(process_file, files))

    results_list = [r for r in results_list if r is not None]
    if not results_list:
        logging.info("No stocks matched the initial technical conditions.")
        return

    results_all = pd.concat(results_list, ignore_index=True)

    # Load fundamental data once
    try:
        query_df = pd.read_csv(FUNDAMENTAL_FILE)
    except FileNotFoundError:
        logging.error(f"Fundamental file not found: {FUNDAMENTAL_FILE}")
        return

    # Process Stage 1 (Mark)
    logging.info("Processing Stage 1 results...")
    df_mark = process_results(results_all, query_df, "mark", MARK_OUT_DIR,
                              MARK_CHART_DIR, "Mark_Date")

    if df_mark is not None and not df_mark.empty:
        # Save mark watchlist
        mark_watchlist_filename = f"{CURRENT_TIMESTAMP}_mark.txt"
        mark_watchlist_filepath = AMIBROKER_WATCHLIST_DIR / mark_watchlist_filename
        with open(mark_watchlist_filepath, 'w') as f:
            for symbol in df_mark['symbol']:
                f.write(f"{symbol.upper()}\n")
        logging.info(f"Watchlist created: {mark_watchlist_filepath}")

    if df_mark is not None:
        # Stage 2: Mark Tight Screening
        logging.info("Stage 2: Running Mark Tight Screening (5-day breakout window)...")
        # §3.4 — filter uses Tight_Breakout flag instead of today's Donchian.
        results_stage2 = results_all[
            (results_all['close'] >= results_all['52 week high'] * 0.85) &
            (results_all['Tight_Breakout'] == True) &          # noqa: E712
            (~results_all['Mark_Tight_Date'].isna())
        ].copy()

        if not results_stage2.empty:
            logging.info(f"Found {len(results_stage2)} stocks matching Mark Tight criteria.")
            process_results(results_stage2, query_df, "mark_tight", MARK_TIGHT_OUT_DIR,
                            MARK_TIGHT_CHART_DIR, "Mark_Tight_Date")

            # Save mark_tight watchlist
            tight_watchlist_filename = f"{CURRENT_TIMESTAMP}_mark_tight.txt"
            tight_watchlist_filepath = AMIBROKER_WATCHLIST_DIR / tight_watchlist_filename
            with open(tight_watchlist_filepath, 'w') as f:
                for symbol in results_stage2['symbol']:
                    f.write(f"{symbol.upper()}\n")
            logging.info(f"Watchlist created: {tight_watchlist_filepath}")
        else:
            logging.info("No stocks matched the Mark Tight criteria.")

    end_time = time.time()
    logging.info(f"Total execution time: {round(end_time - start_time, 2)} seconds")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-7s  %(message)s', datefmt='%H:%M:%S')
    main()
