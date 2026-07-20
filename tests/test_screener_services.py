"""Tests for non-strategy screener services."""

import pandas as pd

from mark_screener.scoring import add_priority_score


def test_priority_score_service_adds_shared_output_columns():
    frame = pd.DataFrame({
        "Current Price": [100, 200],
        "Debt to equity": [0.5, 1.5],
        "Volume 1month average": [1_000_000, 2_000_000],
        "Return over 1year": [20, 10],
        "Return over 3months": [8, 4],
        "Return on capital employed": [18, 10],
        "Return on equity": [16, 8],
        "Piotroski score": [8, 5],
        "Altman Z Score": [3, 1],
        "YOY Quarterly sales growth": [20, 5],
        "YOY Quarterly profit growth": [25, 3],
        "Change in FII holding": [2, -1],
        "Change in DII holding": [1, -2],
        "Pledged percentage": [5, 20],
    })

    result = add_priority_score(frame)

    assert {"Priority_Score", "Turnover_cr", "Tradeable"}.issubset(result.columns)
    assert result.loc[0, "Tradeable"] == "Y"
    assert result.loc[1, "Tradeable"] == "N"
    assert result["Priority_Score"].notna().all()

