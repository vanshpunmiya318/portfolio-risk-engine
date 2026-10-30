
import pytest

from riskengine.db.connection import get_connection
from riskengine.risk.attribution import calculate_performance_attribution


def test_historical_portfolio_attribution():
    with get_connection("data/historical_portfolio.db") as connection:
        result = calculate_performance_attribution(
            connection, 1, "2026-10-23"
        )

    assert len(result) == 6
    assert result["gain_usd"].sum() == pytest.approx(56975.11)
    assert result["initial_investment"].sum() == pytest.approx(99272.65)
    assert result.loc[
        result["ticker"] == "TLT", "gain_usd"
    ].iloc[0] < 0
