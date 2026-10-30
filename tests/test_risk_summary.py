import pandas as pd
import pytest

from test_golden_portfolio import (
    golden_db,
    cash_flow_golden_db,
)

from riskengine.risk.metrics import (
    annualized_volatility,
    maximum_drawdown,
    historical_var,
    historical_cvar,
)
from riskengine.risk.summary import get_portfolio_risk_summary


def test_risk_summary_golden_portfolio(golden_db):
    connection, instruments, portfolio_id = golden_db

    dates = pd.date_range("2026-01-05", "2026-01-09")

    result = get_portfolio_risk_summary(
        connection,
        portfolio_id,
        dates,
    )

    returns = pd.Series([
        20140 / 20000 - 1,
        20205 / 20140 - 1,
        20238 / 20205 - 1,
        20292 / 20238 - 1,
    ])

    assert result["portfolio_id"] == portfolio_id
    assert result["observations"] == 4
    assert result["start_date"] == "2026-01-06"
    assert result["end_date"] == "2026-01-09"
    assert result["beta"] is None

    assert result["annualized_volatility"] == pytest.approx(
        annualized_volatility(returns)
    )
    assert result["maximum_drawdown"] == pytest.approx(
        maximum_drawdown(returns)
    )
    assert result["historical_var"] == pytest.approx(
        historical_var(returns)
    )
    assert result["historical_cvar"] == pytest.approx(
        historical_cvar(returns)
    )


def test_risk_summary_ignores_intermediate_deposit(cash_flow_golden_db):
    connection, portfolio_id = cash_flow_golden_db

    result = get_portfolio_risk_summary(
        connection,
        portfolio_id,
        pd.date_range("2026-01-05", "2026-01-08"),
    )

    expected_returns = pd.Series([0.05, 0.0, 0.02])

    assert result["observations"] == 3
    assert result["annualized_volatility"] == pytest.approx(
        annualized_volatility(expected_returns)
    )
    assert result["maximum_drawdown"] == pytest.approx(0.0)


def test_risk_summary_beta(golden_db):
    connection, instruments, portfolio_id = golden_db

    dates = pd.date_range("2026-01-05", "2026-01-09")

    portfolio_returns = pd.Series(
        [
            20140 / 20000 - 1,
            20205 / 20140 - 1,
            20238 / 20205 - 1,
            20292 / 20238 - 1,
        ],
        index=dates[1:],
    )

    benchmark_returns = portfolio_returns / 2

    result = get_portfolio_risk_summary(
        connection,
        portfolio_id,
        dates,
        benchmark_returns=benchmark_returns,
    )

    assert result["beta"] == pytest.approx(2.0, abs=1e-10)


def test_risk_summary_invalid_confidence(golden_db):
    connection, instruments, portfolio_id = golden_db

    with pytest.raises(ValueError, match="confidence"):
        get_portfolio_risk_summary(
            connection,
            portfolio_id,
            pd.date_range("2026-01-05", "2026-01-09"),
            confidence=1.5,
        )


def test_risk_summary_invalid_trading_days(golden_db):
    connection, instruments, portfolio_id = golden_db

    with pytest.raises(ValueError, match="trading_days"):
        get_portfolio_risk_summary(
            connection,
            portfolio_id,
            pd.date_range("2026-01-05", "2026-01-09"),
            trading_days=0,
        )