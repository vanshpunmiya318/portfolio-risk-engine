import pytest

from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema
from riskengine.db.prices import (
    add_price,
    get_missing_trading_days,
)


@pytest.fixture
def price_database(tmp_path):
    db_path = tmp_path / "trading_days.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments
                (ticker, name, asset_type, currency, primary_source)
            VALUES
                ('SPY', 'SPDR S&P 500 ETF', 'ETF', 'USD', 'test'),
                ('AAPL', 'Apple Inc.', 'EQUITY', 'USD', 'test')
            """
        )

        instrument_ids = dict(
            connection.execute(
                "SELECT ticker, id FROM instruments"
            ).fetchall()
        )

        def insert_price(ticker, date):
            add_price(
                connection=connection,
                instrument_id=instrument_ids[ticker],
                date=date,
                open_price=100,
                high=105,
                low=95,
                close=101,
                adjusted_close=101,
                volume=1000,
                source="test",
                fetched_at="2026-01-10T12:00:00",
            )

        # Monday through Friday, January 5–9, 2026.
        for day in range(5, 10):
            insert_price("SPY", f"2026-01-{day:02d}")

        # AAPL is missing Wednesday, January 7.
        for day in (5, 6, 8, 9):
            insert_price("AAPL", f"2026-01-{day:02d}")

    return db_path


def test_detects_missing_trading_day(price_database):
    with get_connection(price_database) as connection:
        missing = get_missing_trading_days(
            connection,
            "AAPL",
            "2026-01-05",
            "2026-01-09",
        )

    assert missing == ["2026-01-07"]


def test_complete_coverage_has_no_gaps(price_database):
    with get_connection(price_database) as connection:
        missing = get_missing_trading_days(
            connection,
            "SPY",
            "2026-01-05",
            "2026-01-09",
        )

    assert missing == []


def test_weekends_are_not_missing(price_database):
    with get_connection(price_database) as connection:
        missing = get_missing_trading_days(
            connection,
            "AAPL",
            "2026-01-03",
            "2026-01-11",
        )

    assert missing == ["2026-01-07"]


def test_missing_benchmark_raises_error(price_database):
    with get_connection(price_database) as connection:
        with pytest.raises(ValueError, match="No benchmark prices"):
            get_missing_trading_days(
                connection,
                "AAPL",
                "2026-02-01",
                "2026-02-10",
            )


def test_invalid_date_range_raises_error(price_database):
    with get_connection(price_database) as connection:
        with pytest.raises(ValueError, match="End date"):
            get_missing_trading_days(
                connection,
                "AAPL",
                "2026-01-09",
                "2026-01-05",
            )


def test_missing_prices_for_entire_period(price_database):
    with get_connection(price_database) as connection:
        missing = get_missing_trading_days(
            connection,
            "MSFT",
            "2026-01-05",
            "2026-01-09",
        )

    assert missing == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ]