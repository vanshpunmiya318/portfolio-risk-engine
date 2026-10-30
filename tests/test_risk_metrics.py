import numpy as np
import pandas as pd
import pytest

from riskengine.risk.metrics import (
    annualized_volatility,
    maximum_drawdown,
    historical_var,
    historical_cvar,
    portfolio_hhi,
    portfolio_beta,
)


def test_annualized_volatility():
    returns = pd.Series([0.01, -0.01, 0.01, -0.01])

    expected = np.sqrt(4 / 3) * 0.01 * np.sqrt(252)

    assert annualized_volatility(returns) == pytest.approx(
        expected,
        abs=1e-12,
    )


def test_zero_volatility():
    returns = pd.Series([0.01, 0.01, 0.01])

    assert annualized_volatility(returns) == pytest.approx(0.0)


def test_maximum_drawdown():
    returns = pd.Series([0.10, -0.20, 0.05])

    # Wealth: 1.00 -> 1.10 -> 0.88 -> 0.924
    # Maximum drawdown: 0.88 / 1.10 - 1 = -20%

    assert maximum_drawdown(returns) == pytest.approx(-0.20)


def test_no_drawdown():
    returns = pd.Series([0.01, 0.02, 0.03])

    assert maximum_drawdown(returns) == pytest.approx(0.0)


def test_insufficient_returns():
    with pytest.raises(ValueError):
        annualized_volatility(pd.Series([0.01]))

    with pytest.raises(ValueError):
        maximum_drawdown(pd.Series(dtype=float))




def test_historical_var():
    returns = pd.Series([
        -0.10, -0.08, -0.06, -0.04, -0.02,
        0.00, 0.02, 0.04, 0.06, 0.08,
    ])

    assert historical_var(
        returns,
        confidence=0.80,
    ) == pytest.approx(0.064, abs=1e-12)


def test_historical_cvar():
    returns = pd.Series([
        -0.10, -0.08, -0.06, -0.04, -0.02,
        0.00, 0.02, 0.04, 0.06, 0.08,
    ])

    # Worst 20% = -10% and -8%.
    assert historical_cvar(
        returns,
        confidence=0.80,
    ) == pytest.approx(0.09, abs=1e-12)


def test_fractional_tail_cvar():
    returns = pd.Series([-0.10, -0.05, 0.00, 0.05])

    # Worst 30% = one full observation and
    # 20% of the second observation.
    expected = -((-0.10 + 0.2 * -0.05) / 1.2)

    assert historical_cvar(
        returns,
        confidence=0.70,
    ) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize(
    "confidence",
    [0, 1, -0.1, 1.1],
)
def test_invalid_confidence(confidence):
    returns = pd.Series([-0.02, 0.01])

    with pytest.raises(ValueError):
        historical_var(returns, confidence)

    with pytest.raises(ValueError):
        historical_cvar(returns, confidence)


def test_empty_var_cvar():
    returns = pd.Series(dtype=float)

    with pytest.raises(ValueError):
        historical_var(returns)

    with pytest.raises(ValueError):
        historical_cvar(returns)


def test_nonfinite_var_cvar():
    returns = pd.Series([-0.05, np.inf, 0.02])

    with pytest.raises(ValueError):
        historical_var(returns)

    with pytest.raises(ValueError):
        historical_cvar(returns)


def test_95_percent_var_cvar():
    returns = pd.Series(
        np.arange(-100, 100, dtype=float) / 1000
    )

    assert historical_var(
        returns, 0.95
    ) == pytest.approx(0.09005, abs=1e-12)

    assert historical_cvar(
        returns, 0.95
    ) == pytest.approx(0.0955, abs=1e-12)



def test_hhi_equal_weights():
    weights = pd.Series([0.25, 0.25, 0.25, 0.25])

    assert portfolio_hhi(weights) == pytest.approx(
        0.25,
        abs=1e-12,
    )


def test_hhi_single_asset():
    weights = pd.Series([1.0])

    assert portfolio_hhi(weights) == pytest.approx(1.0)


def test_hhi_concentrated_portfolio():
    weights = pd.Series([0.70, 0.20, 0.10])

    expected = 0.70**2 + 0.20**2 + 0.10**2

    assert portfolio_hhi(weights) == pytest.approx(
        0.54,
        abs=1e-12,
    )

    assert portfolio_hhi(weights) == pytest.approx(expected)


def test_hhi_with_cash():
    weights = pd.Series({
        "AAPL": 0.40,
        "MSFT": 0.30,
        "NVDA": 0.20,
        "CASH": 0.10,
    })

    assert portfolio_hhi(weights) == pytest.approx(
        0.30,
        abs=1e-12,
    )


@pytest.mark.parametrize(
    "weights",
    [
        pd.Series(dtype=float),
        pd.Series([0.50, 0.30]),
        pd.Series([1.10, -0.10]),
        pd.Series([np.nan, 1.0]),
        pd.Series([np.inf, 0.0]),
    ],
)
def test_hhi_invalid_weights(weights):
    with pytest.raises(ValueError):
        portfolio_hhi(weights)




def test_beta_twice_benchmark():
    benchmark = pd.Series(
        [0.01, -0.02, 0.03, -0.01],
        index=pd.date_range("2026-01-05", periods=4),
    )

    portfolio = benchmark * 2

    assert portfolio_beta(
        portfolio,
        benchmark,
    ) == pytest.approx(2.0, abs=1e-12)


def test_beta_negative():
    benchmark = pd.Series(
        [0.01, -0.02, 0.03, -0.01]
    )

    portfolio = benchmark * -0.5

    assert portfolio_beta(
        portfolio,
        benchmark,
    ) == pytest.approx(-0.5, abs=1e-12)


def test_beta_aligns_dates():
    dates = pd.date_range("2026-01-05", periods=5)

    benchmark = pd.Series(
        [0.01, 0.02, -0.01, 0.03, -0.02],
        index=dates,
    )

    portfolio = pd.Series(
        [0.04, -0.02, 0.06],
        index=dates[1:4],
    )

    # Matching benchmark values:
    # 0.02, -0.01, 0.03
    # Portfolio values are exactly twice these.
    assert portfolio_beta(
        portfolio,
        benchmark,
    ) == pytest.approx(2.0, abs=1e-12)


def test_beta_zero_benchmark_variance():
    benchmark = pd.Series([0.01, 0.01, 0.01])
    portfolio = pd.Series([0.02, -0.01, 0.03])

    with pytest.raises(ValueError):
        portfolio_beta(portfolio, benchmark)


def test_beta_insufficient_overlap():
    portfolio = pd.Series(
        [0.01, 0.02],
        index=["2026-01-05", "2026-01-06"],
    )

    benchmark = pd.Series(
        [0.01, 0.02],
        index=["2026-01-07", "2026-01-08"],
    )

    with pytest.raises(ValueError):
        portfolio_beta(portfolio, benchmark)


def test_beta_nonfinite_returns():
    portfolio = pd.Series([0.01, np.inf, 0.03])
    benchmark = pd.Series([0.01, 0.02, 0.03])

    with pytest.raises(ValueError):
        portfolio_beta(portfolio, benchmark)