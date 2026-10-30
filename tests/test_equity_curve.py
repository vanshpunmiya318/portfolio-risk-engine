
import pandas as pd
import pytest

from riskengine.risk.equity_curve import build_equity_curves


def test_equity_curves():
    dates = pd.date_range("2024-01-03", periods=3)

    portfolio = pd.Series(
        [0.10, -0.05, 0.02], index=dates
    )
    benchmark = pd.Series(
        [0.05, 0.01, -0.02], index=dates
    )

    curves = build_equity_curves(
        portfolio, benchmark, initial_capital=100_000
    )

    assert len(curves) == 3
    assert curves["portfolio"].iloc[-1] == pytest.approx(106590)
    assert curves["benchmark"].iloc[-1] == pytest.approx(103929)
