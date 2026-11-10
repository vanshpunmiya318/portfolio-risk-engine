
import numpy as np
import pandas as pd
import pytest

from riskengine.risk.dashboard_metrics import calculate_live_risk_metrics


def test_missing_benchmark_date_preserves_portfolio_risk_metrics():
    dates = pd.date_range("2024-01-01", periods=8, freq="B")

    frame = pd.DataFrame({
        "date": dates,
        "portfolio": [100, 102, 99, 104, 101, 106, 103, 108],
        "benchmark": [200, 202, 201, 205, 203, 207, 204, 210],
    })

    portfolio_returns = pd.Series(
        [0.02, -0.03, 0.05, -0.02, 0.04, -0.01, 0.03],
        index=dates[1:],
    )

    baseline = calculate_live_risk_metrics(frame, portfolio_returns)

    missing_benchmark = frame.copy()
    missing_benchmark.loc[3, "benchmark"] = np.nan

    result = calculate_live_risk_metrics(
        missing_benchmark, portfolio_returns
    )

    for metric in ("sharpe", "volatility", "drawdown", "var", "cvar"):
        assert result[metric] == pytest.approx(baseline[metric])

    assert np.isfinite(result["beta"])




def test_insufficient_benchmark_data_preserves_portfolio_metrics():
    dates = pd.date_range("2024-01-01", periods=8, freq="B")

    frame = pd.DataFrame({
        "date": dates,
        "portfolio": [100, 102, 99, 104, 101, 106, 103, 108],
        "benchmark": [200, np.nan, np.nan, np.nan,
                      np.nan, np.nan, np.nan, np.nan],
    })

    portfolio_returns = pd.Series(
        [0.02, -0.03, 0.05, -0.02, 0.04, -0.01, 0.03],
        index=dates[1:],
    )

    result = calculate_live_risk_metrics(frame, portfolio_returns)

    for metric in ("sharpe", "volatility", "drawdown", "var", "cvar"):
        assert np.isfinite(result[metric])

    assert np.isnan(result["beta"])
