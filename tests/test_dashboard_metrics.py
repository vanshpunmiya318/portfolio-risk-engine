
import numpy as np
import pandas as pd
import pytest

from riskengine.risk.dashboard_metrics import calculate_live_risk_metrics
from test_cost_basis_cutoff import ledger
from riskengine.db.transactions import add_cash_flow
from riskengine.db.portfolios import get_total_portfolio_equity
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns


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


KEYS = {"volatility", "drawdown", "sharpe", "var", "cvar", "beta"}


def assert_undefined(result):
    assert set(result) == KEYS
    assert all(isinstance(value, float) and np.isnan(value) for value in result.values())


@pytest.mark.parametrize("equity", [
    [100, 200, 200],  # Deposit only, not a 100% investment gain.
    [200, 100, 100],  # Withdrawal only, not a 50% investment loss.
    [100, 100, 100],  # Flat equity still does not establish cash flows.
    [100, 250, 50],
])
def test_raw_equity_is_never_used_when_adjusted_returns_are_missing(equity):
    frame = pd.DataFrame({
        "date": pd.bdate_range("2026-01-05", periods=3),
        "portfolio": equity, "benchmark": [100, 101, 102],
    })
    assert_undefined(calculate_live_risk_metrics(frame))
    assert_undefined(calculate_live_risk_metrics(frame, None))


@pytest.mark.parametrize("invalid", [
    [], [0.01], [np.nan, np.nan], [0.01, np.nan, 0.02],
    [0.01, np.inf], [0.01, -np.inf], [0.01, -1.01], ["invalid", "returns"],
])
def test_unusable_adjusted_returns_are_undefined_without_equity_fallback(invalid):
    frame = pd.DataFrame({
        "date": pd.bdate_range("2026-01-05", periods=3),
        "portfolio": [100, 200, 300], "benchmark": [100, 101, 102],
    })
    assert_undefined(calculate_live_risk_metrics(frame, invalid))


def test_duplicate_return_dates_are_undefined():
    returns = pd.Series([.01, .02], index=[pd.Timestamp("2026-01-06")] * 2)
    assert_undefined(calculate_live_risk_metrics(pd.DataFrame(), returns))


def test_missing_returns_do_not_require_or_inspect_an_equity_frame():
    assert_undefined(calculate_live_risk_metrics(pd.DataFrame()))


@pytest.mark.parametrize("flow_type,initial,amount,equity", [
    ("DEPOSIT", 100, 100, [100, 200, 200]),
    ("WITHDRAWAL", 200, 100, [200, 100, 100]),
])
def test_cash_only_flows_use_verified_portfolio_return_pipeline(ledger, flow_type, initial, amount, equity):
    _, connection = ledger
    add_cash_flow(connection, 1, "2026-01-05", "DEPOSIT", initial, "USD", "test")
    add_cash_flow(connection, 1, "2026-01-06", flow_type, amount, "USD", "test")
    dates = pd.bdate_range("2026-01-05", periods=3)
    actual_equity = [get_total_portfolio_equity(connection, 1, day.strftime("%Y-%m-%d")) for day in dates]
    assert actual_equity == equity
    returns = get_portfolio_daily_returns(connection, 1, dates)
    assert returns.to_numpy() == pytest.approx([0, 0])
    frame = pd.DataFrame({"date": dates, "portfolio": actual_equity, "benchmark": [100, 101, 102]})
    assert_undefined(calculate_live_risk_metrics(frame))
    result = calculate_live_risk_metrics(frame, returns)
    for key in ("volatility", "drawdown", "var", "cvar", "beta"):
        assert result[key] == pytest.approx(0)
    assert np.isnan(result["sharpe"])


def test_constant_adjusted_returns_preserve_zero_volatility_and_loss_convention():
    dates = pd.bdate_range("2026-01-05", periods=5)
    frame = pd.DataFrame({"date": dates, "portfolio": [100, 200, 300, 150, 200],
                          "benchmark": [100, 102, 101, 103, 104]})
    result = calculate_live_risk_metrics(frame, pd.Series([.01] * 4, index=dates[1:]))
    assert result["volatility"] == pytest.approx(0)
    assert result["drawdown"] == pytest.approx(0)
    assert np.isnan(result["sharpe"])
    assert result["var"] == pytest.approx(-1)
    assert result["cvar"] == pytest.approx(-1)


def test_supplied_return_metrics_are_independent_of_raw_equity():
    dates = pd.bdate_range("2026-01-05", periods=5)
    returns = pd.Series([.01, -.02, .03, .005], index=dates[1:])
    frame = pd.DataFrame({"date": dates, "portfolio": [100, 101, 99, 102, 103],
                          "benchmark": [100, 102, 101, 103, 104]})
    expected = calculate_live_risk_metrics(frame, returns)
    changed = frame.assign(portfolio=[100, 1000, 500, 1500, 50])
    assert calculate_live_risk_metrics(changed, returns) == expected
    assert calculate_live_risk_metrics(frame.drop(columns="portfolio"), returns) == expected
