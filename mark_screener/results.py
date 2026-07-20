"""Fundamental merge, ranking, CSV export and chart dispatch."""

import logging
import os
import shutil
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

import config
from config import CURRENT_TIMESTAMP
from .charts import plot_save_chart
from .scoring import add_priority_score


def process_results(df_results, fundamental_df, suffix, out_dir, chart_dir, date_col, data_dir, plot_days=500):
    merged_df = pd.merge(df_results, fundamental_df, left_on="symbol", right_on="NSE Code", how="left")
    merged_df = merged_df.dropna(subset=["NSE Code"])
    if merged_df.empty:
        logging.info(f"No stocks matched for {suffix}.")
        return None

    merged_df = add_priority_score(merged_df)
    merged_df = merged_df[~merged_df[date_col].isna()]
    df_sorted = merged_df.sort_values(by="Priority_Score", ascending=False)
    df_sorted["Plot Start"] = pd.to_datetime(df_sorted[date_col]) - pd.to_timedelta(plot_days, unit="D")
    df_sorted["Plot End"] = pd.to_datetime(date.today())
    df_sorted["Days_In_Trend"] = (pd.to_datetime(df_sorted["date"]) - pd.to_datetime(df_sorted[date_col])).dt.days
    df_sorted["Trend_Strength"] = ((df_sorted["EMA_50"] - df_sorted["EMA_200"]) / df_sorted["EMA_200"] * 100).round(2)
    df_sorted = df_sorted.reset_index(drop=True)
    df_sorted["Index"] = df_sorted.index

    csv_columns = ["Index", "symbol", "Priority_Score", "Tradeable", "Turnover_cr", "Days_In_Trend", "Trend_Strength"]
    excluded = {
        "Index", "symbol", "date", "close", "EMA_50", "EMA_150", "EMA_200",
        "52 week high", "52 week low", "Mark_Date", "Mark_Tight_Date",
        "Down from 52w high_x", "Donchian_60_High", "Donchian_60_Base", "Tight_Breakout",
        "Plot Start", "Plot End", "Days_In_Trend", "Trend_Strength",
        "Priority_Score", "Tradeable", "Turnover_cr",
    }
    csv_columns.extend(col for col in df_sorted.columns if col not in excluded)
    csv_filepath = Path(out_dir) / f"{CURRENT_TIMESTAMP}_{suffix}_qualified.csv"
    df_sorted[csv_columns].to_csv(csv_filepath, index=False)
    logging.info(f"CSV exported: {csv_filepath} ({len(df_sorted)} stocks)")

    logging.info(f"Generating charts for {suffix} in {chart_dir}...")
    try:
        Path(chart_dir).relative_to(config.CHART_DIR)
    except ValueError:
        logging.error(f"Refusing to rmtree {chart_dir} — not under CHART_DIR")
        return None
    if Path(chart_dir).exists():
        shutil.rmtree(chart_dir)
    Path(chart_dir).mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as executor:
        list(executor.map(plot_save_chart, [(row, chart_dir, date_col, data_dir) for _, row in df_sorted.iterrows()]))
    return df_sorted

