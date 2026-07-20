"""Fundamental ranking and tradeability scoring."""

import numpy as np
import pandas as pd


def add_priority_score(df):
    """Add Priority_Score, Turnover_cr and Tradeable columns to a result set."""

    def percentile(series):
        series = pd.to_numeric(series, errors="coerce")
        median = series.median()
        return series.fillna(median if pd.notna(median) else 0).rank(pct=True) * 100

    price = pd.to_numeric(df["Current Price"], errors="coerce")
    debt_to_equity = pd.to_numeric(df["Debt to equity"], errors="coerce")
    turnover = (
        pd.to_numeric(df["Volume 1month average"], errors="coerce") * price
    ) / 1e7

    momentum = (percentile(df["Return over 1year"]) + percentile(df["Return over 3months"])) / 2
    quality = (
        percentile(df["Return on capital employed"])
        + percentile(df["Return on equity"])
        + percentile(-debt_to_equity)
        + percentile(df["Piotroski score"])
        + percentile(df["Altman Z Score"])
    ) / 5
    fuel = (percentile(df["YOY Quarterly sales growth"]) + percentile(df["YOY Quarterly profit growth"])) / 2
    sponsorship = (percentile(df["Change in FII holding"]) + percentile(df["Change in DII holding"])) / 2
    liquidity = percentile(np.log1p(turnover.clip(lower=0)))

    df["Priority_Score"] = (
        0.30 * momentum + 0.28 * quality + 0.22 * fuel
        + 0.12 * sponsorship + 0.08 * liquidity
    ).round(1)
    df["Turnover_cr"] = turnover.round(1)
    pledge = pd.to_numeric(df["Pledged percentage"], errors="coerce").fillna(0)
    altman = pd.to_numeric(df["Altman Z Score"], errors="coerce")
    df["Tradeable"] = np.where(
        (turnover >= 2.0) & (debt_to_equity <= 1.0)
        & (pledge <= 15) & (altman >= 1.8),
        "Y",
        "N",
    )
    return df

