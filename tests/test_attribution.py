
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




def test_attribution_includes_realized_and_unrealized_gains(tmp_path):
    from riskengine.db.schema.loader import create_schema
    from riskengine.db.portfolios import add_portfolio
    from riskengine.db.transactions import add_cash_flow, add_transaction

    db_path = tmp_path / "attribution_test.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments
            (ticker, name, asset_type, currency, primary_source)
            VALUES ('TEST', 'Test Asset', 'Equity', 'USD', 'test')
            """
        )
        instrument_id = connection.execute(
            "SELECT id FROM instruments WHERE ticker = 'TEST'"
        ).fetchone()[0]

        portfolio_id = add_portfolio(connection, "Test Portfolio", "USD")

        add_cash_flow(
            connection, portfolio_id, "2026-01-02",
            "DEPOSIT", 1000, "USD", "test"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2026-01-02", "BUY", 10, 20, "USD", "test"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2026-01-05", "SELL", 4, 30, "USD", "test"
        )

        connection.execute(
            """
            INSERT INTO prices
            (instrument_id, date, open, high, low, close,
             adjusted_close, volume, source, fetched_at)
            VALUES (?, '2026-01-06', 25, 25, 25, 25, 25,
                    1000, 'test', '2026-01-06')
            """,
            (instrument_id,)
        )

        result = calculate_performance_attribution(
            connection, portfolio_id, "2026-01-06"
        )

    row = result.iloc[0]

    # Sold 4 shares: (30 - 20) * 4 = 40 realized profit.
    assert row["realized_gain_usd"] == pytest.approx(40)

    # Remaining 6 shares: (25 - 20) * 6 = 30 unrealized profit.
    assert row["unrealized_gain_usd"] == pytest.approx(30)

    # Total contribution: 40 + 30 = 70.
    assert row["gain_usd"] == pytest.approx(70)
