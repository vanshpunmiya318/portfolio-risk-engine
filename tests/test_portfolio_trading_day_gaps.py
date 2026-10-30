import pytest

from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema
from riskengine.db.portfolios import add_portfolio
from riskengine.db.instruments import add_instrument
from riskengine.db.transactions import (
    add_transaction,
    get_positions,
)
from riskengine.db.prices import (
    add_price,
    get_portfolio_trading_day_gaps,
)


@pytest.fixture
def portfolio_database(tmp_path):
    db_path = tmp_path / "portfolio_gaps.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        portfolio_id = add_portfolio(
            connection, "Coverage Test Portfolio", "USD"
        )

        spy_id = add_instrument(
            connection, "SPY", "SPDR S&P 500 ETF", "ETF", "USD"
        )
        aapl_id = add_instrument(
            connection, "AAPL", "Apple Inc.", "EQUITY", "USD"
        )

        for instrument_id in (spy_id, aapl_id):
            connection.execute(
                """
                UPDATE instruments
                SET primary_source = 'test'
                WHERE id = ?
                """,
                (instrument_id,),
            )

        def insert_price(instrument_id, price_date):
            add_price(
                connection,
                instrument_id,
                price_date,
                100,
                105,
                95,
                101,
                101,
                1000,
                "test",
                "2026-01-10T12:00:00",
            )

        for day in range(5, 10):
            insert_price(spy_id, f"2026-01-{day:02d}")

        for day in (5, 6, 8, 9):
            insert_price(aapl_id, f"2026-01-{day:02d}")

    return db_path, portfolio_id, aapl_id


def buy(connection, portfolio_id, instrument_id, date, quantity):
    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        date,
        "BUY",
        quantity,
        100,
        "USD",
        "test",
    )


def sell(connection, portfolio_id, instrument_id, date, quantity):
    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        date,
        "SELL",
        quantity,
        100,
        "USD",
        "test",
    )


def check_gaps(connection, portfolio_id):
    return get_portfolio_trading_day_gaps(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )


def test_missing_price_while_holding_is_detected(portfolio_database):
    db_path, portfolio_id, aapl_id = portfolio_database

    with get_connection(db_path) as connection:
        buy(connection, portfolio_id, aapl_id, "2026-01-05", 10)
        result = check_gaps(connection, portfolio_id)

    assert result["missing_prices"] == [
        {"date": "2026-01-07", "ticker": "AAPL"}
    ]
    assert result["missing_count"] == 1
    assert result["is_complete"] is False


def test_prices_before_purchase_are_not_required(portfolio_database):
    db_path, portfolio_id, aapl_id = portfolio_database

    with get_connection(db_path) as connection:
        buy(connection, portfolio_id, aapl_id, "2026-01-08", 10)
        result = check_gaps(connection, portfolio_id)

    assert result["missing_prices"] == []
    assert result["is_complete"] is True


def test_prices_after_complete_sale_are_not_required(portfolio_database):
    db_path, portfolio_id, aapl_id = portfolio_database

    with get_connection(db_path) as connection:
        buy(connection, portfolio_id, aapl_id, "2026-01-05", 10)
        sell(connection, portfolio_id, aapl_id, "2026-01-07", 10)
        result = check_gaps(connection, portfolio_id)

    assert result["missing_prices"] == []


def test_repurchase_reactivates_price_requirement(portfolio_database):
    db_path, portfolio_id, aapl_id = portfolio_database

    with get_connection(db_path) as connection:
        buy(connection, portfolio_id, aapl_id, "2026-01-05", 10)
        sell(connection, portfolio_id, aapl_id, "2026-01-06", 10)
        buy(connection, portfolio_id, aapl_id, "2026-01-07", 5)

        result = check_gaps(connection, portfolio_id)

    assert result["missing_prices"] == [
        {"date": "2026-01-07", "ticker": "AAPL"}
    ]


def test_empty_portfolio_has_no_missing_prices(portfolio_database):
    db_path, portfolio_id, _ = portfolio_database

    with get_connection(db_path) as connection:
        result = check_gaps(connection, portfolio_id)

    assert result["missing_count"] == 0
    assert result["is_complete"] is True
    assert result["trading_days_checked"] == 5


def test_missing_benchmark_raises_error(portfolio_database):
    db_path, portfolio_id, _ = portfolio_database

    with get_connection(db_path) as connection:
        with pytest.raises(ValueError, match="No benchmark prices"):
            get_portfolio_trading_day_gaps(
                connection,
                portfolio_id,
                "2026-02-01",
                "2026-02-10",
            )


def test_invalid_date_range_raises_error(portfolio_database):
    db_path, portfolio_id, _ = portfolio_database

    with get_connection(db_path) as connection:
        with pytest.raises(ValueError, match="End date"):
            get_portfolio_trading_day_gaps(
                connection,
                portfolio_id,
                "2026-01-09",
                "2026-01-05",
            )




def test_split_and_partial_sale_preserve_price_requirement(
    portfolio_database,
):
    db_path, portfolio_id, aapl_id = portfolio_database

    with get_connection(db_path) as connection:
        # Buy 10 shares before the split.
        buy(
            connection,
            portfolio_id,
            aapl_id,
            "2026-01-05",
            10,
        )

        # A 10-for-1 split occurs on January 6.
        # The position becomes 100 shares.
        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id,
                ex_date,
                action_type,
                ratio,
                source,
                fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                aapl_id,
                "2026-01-06",
                "SPLIT",
                10.0,
                "test",
                "2026-01-06T12:00:00",
            ),
        )

        # Sell 40 shares after the split.
        # The remaining position should be 60 shares.
        sell(
            connection,
            portfolio_id,
            aapl_id,
            "2026-01-06",
            40,
        )

        positions = get_positions(
            connection,
            portfolio_id,
            as_of_date="2026-01-07",
        )

        assert positions.iloc[0]["quantity"] == pytest.approx(60)

        # January 7 has no AAPL price.
        # Because 60 shares remain, it must be flagged.
        result = check_gaps(connection, portfolio_id)

    assert result["missing_prices"] == [
        {
            "date": "2026-01-07",
            "ticker": "AAPL",
        }
    ]

    assert result["missing_count"] == 1
    assert result["is_complete"] is False