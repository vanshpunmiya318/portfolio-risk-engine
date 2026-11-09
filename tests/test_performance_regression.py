import numpy as np
import pandas as pd
import pytest

from riskengine.risk.performance import calculate_performance_metrics


def test_dashboard_performance_metrics():
    returns = pd.Series(
        [0.10, -0.20, 0.05, 0.03],
        index=pd.date_range("2026-01-05", periods=4, freq="B"),
    )

    result = calculate_performance_metrics(
        returns,
        initial_capital=1.0,
        risk_free_rate=0.0,
    )

    expected_volatility = returns.std(ddof=1) * np.sqrt(252)
    expected_sharpe = (
        returns.mean() / returns.std(ddof=1) * np.sqrt(252)
    )

    # Wealth: 1.00 -> 1.10 -> 0.88 -> 0.924 -> 0.95172
    expected_drawdown = 0.20

    assert result["annualized_volatility"] == pytest.approx(
        expected_volatility
    )
    assert result["sharpe_ratio"] == pytest.approx(expected_sharpe)
    assert result["max_drawdown"] == pytest.approx(expected_drawdown)
    assert result["observations"] == 4


def test_zero_volatility_sharpe():
    returns = pd.Series([0.01, 0.01, 0.01, 0.01])

    result = calculate_performance_metrics(
        returns,
        initial_capital=1.0,
    )

    assert result["annualized_volatility"] == pytest.approx(0.0)
    assert result["sharpe_ratio"] is None


def test_invalid_performance_returns():
    with pytest.raises(ValueError):
        calculate_performance_metrics(
            pd.Series([0.01, np.inf, 0.02])
        )