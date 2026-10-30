from riskengine.db.portfolios import (
    add_portfolio,
    get_portfolio_id,
    get_or_create_portfolio,
    get_portfolio_value,
    get_total_portfolio_value,
    get_portfolio_weights,
    get_portfolio_return,
    get_total_portfolio_equity,
    get_cash_flow_adjusted_return,
    get_portfolio_subperiod_return,
    get_time_weighted_return,
    get_portfolio_equity_curve,
)

from riskengine.db.transactions import (
    add_transaction,
    get_transactions,
    get_positions,
    validate_transactions,
    get_cost_basis,
    add_cash_flow,
    get_cash_flows,
    get_net_cash_flows,
    get_cash_balance,
    get_cash_flow_dates,
    get_cash_flow_on_date,
)

from riskengine.db.prices import (
    add_price,
    get_prices,
    add_prices_from_dataframe,
    get_latest_prices,
    get_daily_returns,
    get_portfolio_price_dates,
    get_portfolio_price_coverage,
    get_price_with_max_age,
)

import sqlite3
import pytest

from riskengine.db.connection import get_connection
from riskengine.db.instruments import get_or_create_instrument

from riskengine.db import instruments as instrument_module

@pytest.fixture(autouse=True)
def use_test_primary_source(monkeypatch):
    original_add_instrument = instrument_module.add_instrument

    def add_test_instrument(
        connection,
        ticker,
        name,
        asset_type,
        currency,
        sector=None,
    ):
        instrument_id = original_add_instrument(
            connection,
            ticker,
            name,
            asset_type,
            currency,
            sector,
        )

        connection.execute(
            """
            UPDATE instruments
            SET primary_source = 'test'
            WHERE id = ?
            """,
            (instrument_id,),
        )

        return instrument_id

    monkeypatch.setattr(
        instrument_module,
        "add_instrument",
        add_test_instrument,
    )

    # test_connection.py imports add_instrument directly.
    monkeypatch.setattr(
        __import__(__name__, fromlist=["add_instrument"]),
        "add_instrument",
        add_test_instrument,
    )
    

def test_foreign_keys_are_enabled():
    connection = get_connection(":memory:")

    result = connection.execute("PRAGMA foreign_keys").fetchone()

    assert result == (1,)
    connection.close()


def test_foreign_key_constraint_is_enforced():
    connection = get_connection(":memory:")

    connection.execute(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY)"
    )
    connection.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER, "
        "FOREIGN KEY (parent_id) REFERENCES parent(id))"
    )

    try:
        connection.execute(
            "INSERT INTO child (id, parent_id) VALUES (1, 999)"
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("Foreign key constraint was not enforced")

    connection.close()






def test_create_schema_creates_instruments_table(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)
    result = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'instruments'"
    ).fetchone()

    assert result == ("instruments",)
    connection.close()




def test_create_schema_can_run_twice(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)
    create_schema(db_path)

    connection = get_connection(db_path)
    result = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'instruments'"
    ).fetchone()

    assert result == ("instruments",)
    connection.close()



def test_create_schema_creates_prices_table(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)
    result = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'prices'"
    ).fetchone()

    assert result == ("prices",)
    connection.close()



def test_price_requires_existing_instrument(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO prices (
                instrument_id,
                date,
                open,
                high,
                low,
                close,
                adjusted_close,
                volume,
                source,
                fetched_at
            )
            VALUES (999, '2026-01-02', 100, 105, 99, 103, 103, 1000,
                    'test', '2026-01-02T12:00:00')
            """
        )

    connection.close()






def test_price_allows_different_dates_for_same_instrument(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    connection.execute(
        """
        INSERT INTO instruments (id, ticker, name, asset_type, currency, sector)
        VALUES (1, 'AAPL', 'Apple Inc.', 'EQUITY', 'USD', 'Technology')
        """
    )

    connection.execute(
        """
        INSERT INTO prices (
            instrument_id,
            date,
            open,
            high,
            low,
            close,
            adjusted_close,
            volume,
            source,
            fetched_at
        )
        VALUES (1, '2026-01-02', 100, 105, 99, 103, 103, 1000,
                'test', '2026-01-02T12:00:00')
        """
    )

    connection.execute(
        """
        INSERT INTO prices (
            instrument_id,
            date,
            open,
            high,
            low,
            close,
            adjusted_close,
            volume,
            source,
            fetched_at
        )
        VALUES (1, '2026-01-03', 103, 107, 102, 106, 106, 1200,
                'test', '2026-01-03T12:00:00')
        """
    )

    result = connection.execute(
        "SELECT COUNT(*) FROM prices"
    ).fetchone()

    assert result == (2,)

    connection.close()



def test_add_instrument(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.instruments import add_instrument

    with get_connection(db_path) as connection:
        instrument_id = add_instrument(
            connection,
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
            "Technology",
        )

        result = connection.execute(
            """
            SELECT ticker, name, asset_type, currency, sector
            FROM instruments
            WHERE id = ?
            """,
            (instrument_id,),
        ).fetchone()

    assert result == (
        "AAPL",
        "Apple Inc.",
        "EQUITY",
        "USD",
        "Technology",
    )



def test_price_date_must_be_unique_per_instrument(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    connection.execute(
        """
        INSERT INTO instruments (id, ticker, name, asset_type, currency, sector)
        VALUES (1, 'AAPL', 'Apple Inc.', 'EQUITY', 'USD', 'Technology')
        """
    )

    connection.execute(
        """
        INSERT INTO prices (
            instrument_id,
            date,
            open,
            high,
            low,
            close,
            adjusted_close,
            volume,
            source,
            fetched_at
        )
        VALUES (1, '2026-01-02', 100, 105, 99, 103, 103, 1000,
                'test', '2026-01-02T12:00:00')
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO prices (
                instrument_id,
                date,
                open,
                high,
                low,
                close,
                adjusted_close,
                volume,
                source,
                fetched_at
            )
            VALUES (1, '2026-01-02', 101, 106, 100, 104, 104, 1100,
                    'test', '2026-01-02T13:00:00')
            """
        )

    connection.close()



def test_add_instrument_rejects_duplicate_ticker(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.instruments import add_instrument

    with get_connection(db_path) as connection:
        add_instrument(
            connection,
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
            "Technology",
        )

        with pytest.raises(sqlite3.IntegrityError):
            add_instrument(
                connection,
                "AAPL",
                "Apple Inc.",
                "EQUITY",
                "USD",
                "Technology",
            )




def test_add_price(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.prices import add_price

    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
             id,
             ticker,
             name,
             asset_type,
             currency,
             sector,
             primary_source
            )
            VALUES (1, 'AAPL', 'Apple Inc.', 'EQUITY', 'USD', 'Technology', 'test')
            """
        )

        price_id = add_price(
            connection,
            1,
            "2026-01-02",
            100,
            105,
            99,
            103,
            103,
            1000,
            "test",
            "2026-01-02T12:00:00",
        )

        result = connection.execute(
            """
            SELECT instrument_id, date, open, high, low, close,
                   adjusted_close, volume, source, fetched_at
            FROM prices
            WHERE id = ?
            """,
            (price_id,),
        ).fetchone()

    assert result == (
        1,
        "2026-01-02",
        100,
        105,
        99,
        103,
        103,
        1000,
        "test",
        "2026-01-02T12:00:00",
    )









def test_price_rejects_non_positive_close(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                id,
                ticker,
                name,
                asset_type,
                currency,
                sector
            )
            VALUES (1, 'AAPL', 'Apple Inc.', 'EQUITY', 'USD', 'Technology')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO prices (
                    instrument_id,
                    date,
                    open,
                    high,
                    low,
                    close,
                    adjusted_close,
                    volume,
                    source,
                    fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "2026-01-02",
                    100,
                    105,
                    99,
                    0,
                    100,
                    1000,
                    "test",
                    "2026-01-02T12:00:00",
                ),
            )





def test_price_rejects_non_positive_adjusted_close(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                id, ticker, name, asset_type, currency, sector
            )
            VALUES (1, 'AAPL', 'Apple Inc.', 'EQUITY', 'USD', 'Technology')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO prices (
                    instrument_id, date, open, high, low, close,
                    adjusted_close, volume, source, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "2026-01-02",
                    100,
                    105,
                    99,
                    103,
                    0,
                    1000,
                    "test",
                    "2026-01-02T12:00:00",
                ),
            )





def test_price_rejects_high_below_low(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                id, ticker, name, asset_type, currency, sector
            )
            VALUES (1, 'AAPL', 'Apple Inc.', 'EQUITY', 'USD', 'Technology')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO prices (
                    instrument_id, date, open, high, low, close,
                    adjusted_close, volume, source, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "2026-01-02",
                    100,
                    98,
                    99,
                    100,
                    100,
                    1000,
                    "test",
                    "2026-01-02T12:00:00",
                ),
            )




def test_add_price_updates_existing_date(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.instruments import add_instrument
    from riskengine.db.prices import add_price

    with get_connection(db_path) as connection:
        instrument_id = add_instrument(
            connection,
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
            "Technology",
        )

        add_price(
            connection,
            instrument_id,
            "2026-01-02",
            100,
            105,
            99,
            103,
            103,
            1000,
            "test",
            "2026-01-02T12:00:00",
        )

        add_price(
            connection,
            instrument_id,
            "2026-01-02",
            101,
            106,
            100,
            104,
            104,
            1100,
            "test",
            "2026-01-02T13:00:00",
        )

        result = connection.execute(
            """
            SELECT open, high, low, close, adjusted_close,
                   volume, source, fetched_at
            FROM prices
            WHERE instrument_id = ? AND date = ?
            """,
            (instrument_id, "2026-01-02"),
        ).fetchone()

    assert result == (
        101,
        106,
        100,
        104,
        104,
        1100,
        "test",
        "2026-01-02T13:00:00",
    )



def test_add_price_returns_same_id_when_existing_date_is_updated(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.instruments import add_instrument
    from riskengine.db.prices import add_price

    with get_connection(db_path) as connection:
        instrument_id = add_instrument(
            connection,
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
            "Technology",
        )

        first_id = add_price(
            connection,
            instrument_id,
            "2026-01-02",
            100,
            105,
            99,
            103,
            103,
            1000,
            "test",
            "2026-01-02T12:00:00",
        )

        second_id = add_price(
            connection,
            instrument_id,
            "2026-01-02",
            101,
            106,
            100,
            104,
            104,
            1100,
            "test",
            "2026-01-02T13:00:00",
        )

    assert first_id == second_id



def test_get_prices_returns_filtered_dataframe(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.instruments import add_instrument
    from riskengine.db.prices import add_price, get_prices

    with get_connection(db_path) as connection:
        instrument_id = add_instrument(
            connection,
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
            "Technology",
        )

        add_price(
            connection,
            instrument_id,
            "2026-01-02",
            100,
            105,
            99,
            103,
            103,
            1000,
            "test",
            "2026-01-02T12:00:00",
        )

        add_price(
            connection,
            instrument_id,
            "2026-01-05",
            103,
            108,
            102,
            107,
            107,
            1200,
            "test",
            "2026-01-05T12:00:00",
        )

        add_price(
            connection,
            instrument_id,
            "2026-01-06",
            107,
            110,
            106,
            109,
            109,
            1300,
            "test",
            "2026-01-06T12:00:00",
        )

        result = get_prices(
            connection,
            "AAPL",
            "2026-01-05",
            "2026-01-06",
        )

    assert list(result.index.strftime("%Y-%m-%d")) == [
        "2026-01-05",
        "2026-01-06",
    ]

    assert list(result["close"]) == [107, 109]
    assert list(result["volume"]) == [1200, 1300]






def test_get_prices_returns_empty_dataframe_for_unknown_ticker(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.prices import get_prices

    with get_connection(db_path) as connection:
        result = get_prices(connection, "AAPL")

    assert result.empty





def test_get_instrument_id_returns_id_for_existing_ticker(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.instruments import (
        add_instrument,
        get_instrument_id,
    )

    with get_connection(db_path) as connection:
        instrument_id = add_instrument(
            connection,
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
            "Technology",
        )

        result = get_instrument_id(connection, "AAPL")

    assert result == instrument_id





def test_get_instrument_id_returns_none_for_unknown_ticker(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from riskengine.db.instruments import get_instrument_id

    with get_connection(db_path) as connection:
        result = get_instrument_id(connection, "AAPL")

    assert result is None




from contextlib import closing

import pandas as pd

from riskengine.db.connection import get_connection
from riskengine.db.instruments import add_instrument
from riskengine.db.prices import add_prices_from_dataframe, get_prices
from riskengine.db.schema.loader import create_schema



def test_ticker_lookup_ignores_case_and_spaces(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    from contextlib import closing
    from riskengine.db.instruments import (
        add_instrument,
        get_instrument_id,
    )

    with closing(get_connection(db_path)) as connection:
        with connection:
            instrument_id = add_instrument(
                connection,
                " aapl ",
                "Apple Inc.",
                "EQUITY",
                "USD",
                "Technology",
            )

        assert get_instrument_id(connection, "AAPL") == instrument_id
        assert get_instrument_id(connection, "aapl") == instrument_id




def make_prices(second_day_adjusted_close):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [100.0, 103.0],
            "high": [105.0, 108.0],
            "low": [99.0, 102.0],
            "close": [103.0, 107.0],
            "adjusted_close": [103.0, second_day_adjusted_close],
            "volume": [1000, 1200],
            "source": ["test", "test"],
            "fetched_at": ["2026-01-05T12:00:00+00:00"] * 2,
        }
    )


def test_add_prices_from_dataframe_is_repeatable_and_updates(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with closing(get_connection(db_path)) as connection:
        with connection:
            instrument_id = add_instrument(
                connection,
                "AAPL",
                "Apple Inc.",
                "EQUITY",
                "USD",
                "Technology",
            )

            add_prices_from_dataframe(
                connection,
                instrument_id,
                make_prices(107.0),
            )

        with connection:
            add_prices_from_dataframe(
                connection,
                instrument_id,
                make_prices(106.5),
            )

        result = get_prices(connection, "AAPL")

    assert list(result.index.strftime("%Y-%m-%d")) == [
        "2026-01-02",
        "2026-01-05",
    ]

    assert list(result["adjusted_close"]) == [
        103.0,
        106.5,
    ]



def test_get_or_create_instrument_returns_existing_instrument_id(
    tmp_path,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with closing(get_connection(db_path)) as connection:
        with connection:
            first_id = get_or_create_instrument(
                connection,
                "AAPL",
                "Apple Inc.",
                "EQUITY",
                "USD",
                "Technology",
            )

            second_id = get_or_create_instrument(
                connection,
                "AAPL",
                "Apple Inc.",
                "EQUITY",
                "USD",
                "Technology",
            )

        result = connection.execute(
            "SELECT COUNT(*) FROM instruments WHERE ticker = ?",
            ("AAPL",),
        ).fetchone()

    assert first_id == second_id
    assert result[0] == 1




def test_create_schema_creates_portfolios_table(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)
    result = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'portfolios'"
    ).fetchone()

    assert result == ("portfolios",)
    connection.close()


def test_create_schema_creates_transactions_table(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)
    result = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'transactions'"
    ).fetchone()

    assert result == ("transactions",)
    connection.close()




def test_transaction_requires_existing_portfolio_and_instrument(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO transactions (
                portfolio_id,
                instrument_id,
                date,
                transaction_type,
                quantity,
                price,
                currency,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                999,
                999,
                "2026-01-02",
                "BUY",
                10,
                100,
                "USD",
                "test",
            ),
        )

    connection.close()




def test_transaction_rejects_non_positive_quantity_and_price(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = connection.execute(
        """
        INSERT INTO portfolios (name, currency)
        VALUES (?, ?)
        """,
        ("Test Portfolio", "USD"),
    ).lastrowid

    instrument_id = connection.execute(
        """
        INSERT INTO instruments (
            ticker,
            name,
            asset_type,
            currency
        )
        VALUES (?, ?, ?, ?)
        """,
        ("AAPL", "Apple Inc.", "EQUITY", "USD"),
    ).lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO transactions (
                portfolio_id,
                instrument_id,
                date,
                transaction_type,
                quantity,
                price,
                currency,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                portfolio_id,
                instrument_id,
                "2026-01-02",
                "BUY",
                0,
                100,
                "USD",
                "test",
            ),
        )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO transactions (
                portfolio_id,
                instrument_id,
                date,
                transaction_type,
                quantity,
                price,
                currency,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                portfolio_id,
                instrument_id,
                "2026-01-02",
                "BUY",
                10,
                0,
                "USD",
                "test",
            ),
        )

    connection.close()



def test_add_portfolio(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    result = connection.execute(
        """
        SELECT name, currency
        FROM portfolios
        WHERE id = ?
        """,
        (portfolio_id,),
    ).fetchone()

    assert result == ("Test Portfolio", "USD")
    connection.close()


def test_get_portfolio_id(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    assert get_portfolio_id(
        connection,
        "Test Portfolio",
    ) == portfolio_id

    assert get_portfolio_id(
        connection,
        "Unknown Portfolio",
    ) is None

    connection.close()





def test_add_transaction(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = connection.execute(
        """
        INSERT INTO instruments (
            ticker,
            name,
            asset_type,
            currency
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
        ),
    ).lastrowid

    transaction_id = add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    result = connection.execute(
        """
        SELECT
            portfolio_id,
            instrument_id,
            date,
            transaction_type,
            quantity,
            price,
            currency,
            source
        FROM transactions
        WHERE id = ?
        """,
        (transaction_id,),
    ).fetchone()

    assert result == (
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    connection.close()




def test_transaction_rejects_invalid_transaction_type(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = connection.execute(
        """
        INSERT INTO instruments (
            ticker,
            name,
            asset_type,
            currency
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
        ),
    ).lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO transactions (
                portfolio_id,
                instrument_id,
                date,
                transaction_type,
                quantity,
                price,
                currency,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                portfolio_id,
                instrument_id,
                "2026-01-02",
                "BANANA",
                10,
                100,
                "USD",
                "test",
            ),
        )

    connection.close()




def test_add_transaction_rejects_invalid_transaction_type(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = connection.execute(
        """
        INSERT INTO instruments (
            ticker,
            name,
            asset_type,
            currency
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
        ),
    ).lastrowid

    with pytest.raises(
        ValueError,
        match="Transaction type must be BUY or SELL",
    ):
        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2026-01-02",
            "BANANA",
            10,
            100,
            "USD",
            "test",
        )

    connection.close()




def test_get_transactions_returns_portfolio_transactions(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = connection.execute(
        """
        INSERT INTO instruments (
            ticker,
            name,
            asset_type,
            currency
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            "AAPL",
            "Apple Inc.",
            "EQUITY",
            "USD",
        ),
    ).lastrowid

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-05",
        "SELL",
        2,
        110,
        "USD",
        "test",
    )

    transactions = get_transactions(
        connection,
        portfolio_id,
    )

    assert list(transactions.columns) == [
        "date",
        "ticker",
        "transaction_type",
        "quantity",
        "price",
        "currency",
        "source",
    ]

    assert len(transactions) == 2
    assert transactions.iloc[0]["ticker"] == "AAPL"
    assert transactions.iloc[0]["transaction_type"] == "BUY"
    assert transactions.iloc[0]["quantity"] == 10
    assert transactions.iloc[1]["transaction_type"] == "SELL"
    assert transactions.iloc[1]["quantity"] == 2

    connection.close()





def test_portfolio_lookup_ignores_spaces(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "  Test Portfolio  ",
        "USD",
    )

    assert get_portfolio_id(
        connection,
        "Test Portfolio",
    ) == portfolio_id

    assert get_portfolio_id(
        connection,
        "  Test Portfolio  ",
    ) == portfolio_id

    connection.close()




def test_add_portfolio_normalises_currency(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        " usd ",
    )

    result = connection.execute(
        """
        SELECT currency
        FROM portfolios
        WHERE id = ?
        """,
        (portfolio_id,),
    ).fetchone()

    assert result == ("USD",)

    connection.close()




def test_get_or_create_portfolio_returns_existing_portfolio_id(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    first_id = get_or_create_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    second_id = get_or_create_portfolio(
        connection,
        "Test Portfolio",
        "EUR",
    )

    assert second_id == first_id

    count = connection.execute(
        """
        SELECT COUNT(*)
        FROM portfolios
        """
    ).fetchone()[0]

    assert count == 1

    connection.close()





def test_get_or_create_portfolio_ignores_name_case(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    first_id = get_or_create_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    second_id = get_or_create_portfolio(
        connection,
        "test portfolio",
        "USD",
    )

    assert second_id == first_id

    count = connection.execute(
        """
        SELECT COUNT(*)
        FROM portfolios
        """
    ).fetchone()[0]

    assert count == 1

    connection.close()




def test_add_transaction_normalises_type_and_currency(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        " buy ",
        10,
        100,
        " usd ",
        "test",
    )

    transaction = connection.execute(
        """
        SELECT transaction_type, currency
        FROM transactions
        """
    ).fetchone()

    assert transaction == ("BUY", "USD")

    connection.close()



def test_get_positions_calculates_net_quantity(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-10",
        "SELL",
        3,
        110,
        "USD",
        "test",
    )

    positions = get_positions(
        connection,
        portfolio_id,
    )

    assert len(positions) == 1
    assert positions.iloc[0]["ticker"] == "AAPL"
    assert positions.iloc[0]["quantity"] == 7

    connection.close()



def test_validate_transactions_rejects_temporary_negative_position(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-03",
        "SELL",
        15,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-04",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    try:
        validate_transactions(
            connection,
            portfolio_id,
        )
        assert False, "Expected ValueError for negative position"
    except ValueError as error:
        assert "AAPL" in str(error)

    connection.close()




def test_get_cost_basis_uses_weighted_average_cost(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-03",
        "BUY",
        10,
        120,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-04",
        "SELL",
        5,
        130,
        "USD",
        "test",
    )

    cost_basis = get_cost_basis(
        connection,
        portfolio_id,
    )

    assert len(cost_basis) == 1
    assert cost_basis.iloc[0]["ticker"] == "AAPL"
    assert cost_basis.iloc[0]["quantity"] == 15
    assert cost_basis.iloc[0]["total_cost"] == 1650
    assert cost_basis.iloc[0]["average_cost"] == 110

    connection.close()




def test_get_latest_prices_respects_as_of_date(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        99,
        101,
        98,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        109,
        111,
        108,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-10",
        119,
        121,
        118,
        120,
        120,
        1000,
        "test",
        "2026-01-10T00:00:00+00:00",
    )

    prices = get_latest_prices(
        connection,
        ["AAPL"],
        "2026-01-07",
    )

    assert len(prices) == 1
    assert prices.iloc[0]["ticker"] == "AAPL"
    assert prices.iloc[0]["date"] == pd.Timestamp("2026-01-05")
    assert prices.iloc[0]["adjusted_close"] == 110

    connection.close()




def test_get_portfolio_value_uses_price_as_of_date(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        109,
        111,
        108,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-10",
        119,
        121,
        118,
        120,
        120,
        1000,
        "test",
        "2026-01-10T00:00:00+00:00",
    )

    portfolio_value = get_portfolio_value(
        connection,
        portfolio_id,
        "2026-01-07",
    )

    assert len(portfolio_value) == 1
    assert portfolio_value.iloc[0]["ticker"] == "AAPL"
    assert portfolio_value.iloc[0]["quantity"] == 10
    assert portfolio_value.iloc[0]["price_date"] == pd.Timestamp(
        "2026-01-05"
    )
    assert portfolio_value.iloc[0]["adjusted_close"] == 110
    assert portfolio_value.iloc[0]["market_value"] == 1100

    connection.close()




def test_get_portfolio_value_rejects_missing_price(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    try:
        get_portfolio_value(
            connection,
            portfolio_id,
            "2026-01-01",
        )
        assert False, "Expected ValueError for missing price"
    except ValueError as error:
        assert "AAPL" in str(error)

    connection.close()





def test_get_total_portfolio_value_sums_holdings(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        aapl_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        msft_id,
        "2026-01-02",
        "BUY",
        5,
        200,
        "USD",
        "test",
    )

    add_price(
        connection,
        aapl_id,
        "2026-01-05",
        109,
        111,
        108,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    add_price(
        connection,
        msft_id,
        "2026-01-05",
        209,
        211,
        208,
        210,
        210,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    total_value = get_total_portfolio_value(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert total_value == 2150

    connection.close()





def test_get_portfolio_weights_sum_to_one(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        aapl_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        msft_id,
        "2026-01-02",
        "BUY",
        5,
        200,
        "USD",
        "test",
    )

    add_price(
        connection,
        aapl_id,
        "2026-01-05",
        109,
        111,
        108,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    add_price(
        connection,
        msft_id,
        "2026-01-05",
        209,
        211,
        208,
        210,
        210,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    weights = get_portfolio_weights(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert len(weights) == 2
    assert weights["weight"].sum() == 1
    assert weights.loc[
        weights["ticker"] == "AAPL",
        "weight",
    ].iloc[0] == 1100 / 2150
    assert weights.loc[
        weights["ticker"] == "MSFT",
        "weight",
    ].iloc[0] == 1050 / 2150

    connection.close()





def test_get_daily_returns_uses_adjusted_close(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        99,
        101,
        98,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        109,
        111,
        108,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-06",
        114,
        116,
        113,
        115,
        115,
        1000,
        "test",
        "2026-01-06T00:00:00+00:00",
    )

    returns = get_daily_returns(
        connection,
        "AAPL",
    )

    assert len(returns) == 2
    assert returns.iloc[0] == pytest.approx(0.10)
    assert returns.iloc[1] == pytest.approx((115 / 110) - 1)
    connection.close()




def test_get_cost_basis_rejects_negative_position(db_connection):
    from riskengine.db.transactions import get_cost_basis

    connection = db_connection()

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-03",
        "SELL",
        15,
        110,
        "USD",
        "test",
    )

    with pytest.raises(ValueError, match="Negative position"):
        get_cost_basis(
            connection,
            portfolio_id,
        )




def test_get_cost_basis_rejects_negative_position(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-03",
        "SELL",
        15,
        110,
        "USD",
        "test",
    )

    with pytest.raises(ValueError, match="Negative position"):
        get_cost_basis(
            connection,
            portfolio_id,
        )

    connection.close()




def test_validate_transactions_allows_selling_entire_position(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-03",
        "SELL",
        10,
        110,
        "USD",
        "test",
    )

    assert validate_transactions(
        connection,
        portfolio_id,
    ) is True

    connection.close()



def test_get_portfolio_value_uses_latest_available_price(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100,
        105,
        95,
        103,
        103,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        104,
        110,
        102,
        108,
        108,
        1200,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_value(
        connection,
        portfolio_id,
        "2026-01-06",
    )

    assert len(result) == 1
    assert result.iloc[0]["ticker"] == "AAPL"
    assert result.iloc[0]["quantity"] == 10
    assert result.iloc[0]["price_date"].strftime("%Y-%m-%d") == "2026-01-05"
    assert result.iloc[0]["adjusted_close"] == 108
    assert result.iloc[0]["market_value"] == 1080

    connection.close()




def test_get_portfolio_value_rejects_missing_price(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    with pytest.raises(ValueError, match="No price available"):
        get_portfolio_value(
            connection,
            portfolio_id,
            "2026-01-06",
        )

    connection.close()




def test_get_portfolio_return_calculates_simple_return(tmp_path):
    db_path = tmp_path / "test.db"

    create_schema(db_path)

    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100,
        100,
        100,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        110,
        110,
        110,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
    )

    assert result == pytest.approx(0.10)

    connection.close()





def test_get_cash_flows_returns_portfolio_cash_flows(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "WITHDRAWAL",
        200,
        "USD",
        "test",
    )

    result = get_cash_flows(
        connection,
        portfolio_id,
    )

    assert len(result) == 2
    assert result.iloc[0]["flow_type"] == "DEPOSIT"
    assert result.iloc[0]["amount"] == 1000
    assert result.iloc[0]["currency"] == "USD"
    assert result.iloc[1]["flow_type"] == "WITHDRAWAL"
    assert result.iloc[1]["amount"] == 200

    connection.close()




@pytest.mark.parametrize(
    "amount",
    [0, -100, float("nan"), float("inf"), float("-inf")],
)
def test_add_cash_flow_rejects_invalid_amounts(tmp_path, amount):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    with pytest.raises(
        ValueError,
        match="Cash flow amount must be a positive finite number",
    ):
        add_cash_flow(
            connection,
            portfolio_id,
            "2026-01-02",
            "DEPOSIT",
            amount,
            "USD",
            "test",
        )

    connection.close()





@pytest.mark.parametrize(
    "flow_type",
    ["TRANSFER", "BUY", "SELL", ""],
)
def test_add_cash_flow_rejects_invalid_types(tmp_path, flow_type):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    with pytest.raises(
        ValueError,
        match="Flow type must be DEPOSIT or WITHDRAWAL",
    ):
        add_cash_flow(
            connection,
            portfolio_id,
            "2026-01-02",
            flow_type,
            1000,
            "USD",
            "test",
        )

    connection.close()





def test_get_net_cash_flows_respects_as_of_date(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "WITHDRAWAL",
        200,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-10",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    assert get_net_cash_flows(
        connection,
        portfolio_id,
        "2026-01-04",
    ) == 1000

    assert get_net_cash_flows(
        connection,
        portfolio_id,
        "2026-01-06",
    ) == 800

    assert get_net_cash_flows(
        connection,
        portfolio_id,
        "2026-01-10",
    ) == 1300

    connection.close()





def test_get_cash_balance_includes_trades_and_cash_flows(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-03",
        "BUY",
        5,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-05",
        "SELL",
        2,
        120,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-06",
        "WITHDRAWAL",
        100,
        "USD",
        "test",
    )

    assert get_cash_balance(
        connection,
        portfolio_id,
        "2026-01-02",
    ) == 1000

    assert get_cash_balance(
        connection,
        portfolio_id,
        "2026-01-04",
    ) == 500

    assert get_cash_balance(
        connection,
        portfolio_id,
        "2026-01-05",
    ) == 740

    assert get_cash_balance(
        connection,
        portfolio_id,
        "2026-01-06",
    ) == 640

    connection.close()





def test_get_cash_balance_returns_zero_without_activity(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    assert get_cash_balance(
        connection,
        portfolio_id,
        "2026-01-01",
    ) == 0

    connection.close()





def test_get_total_portfolio_equity_includes_cash(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        2000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        110,
        110,
        110,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_total_portfolio_equity(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert result == pytest.approx(2100)

    connection.close()





def test_get_portfolio_value_excludes_future_transactions(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-10",
        "BUY",
        5,
        110,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        105,
        105,
        105,
        105,
        105,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_value(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert len(result) == 1
    assert result.iloc[0]["quantity"] == 10
    assert result.iloc[0]["market_value"] == pytest.approx(1050)

    connection.close()




def test_get_portfolio_value_excludes_future_sales(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-10",
        "SELL",
        6,
        110,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        105,
        105,
        105,
        105,
        105,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_value(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert len(result) == 1
    assert result.iloc[0]["quantity"] == 10
    assert result.iloc[0]["market_value"] == pytest.approx(1050)

    connection.close()



def test_total_portfolio_equity_excludes_future_deposits(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-10",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    result = get_total_portfolio_equity(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert result == pytest.approx(1000)

    connection.close()





def test_total_portfolio_equity_excludes_future_withdrawals(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-10",
        "WITHDRAWAL",
        300,
        "USD",
        "test",
    )

    result = get_total_portfolio_equity(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert result == pytest.approx(1000)

    connection.close()





def test_portfolio_equity_increase_from_deposit_is_not_profit(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    starting_equity = get_total_portfolio_equity(
        connection,
        portfolio_id,
        "2026-01-02",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    ending_equity = get_total_portfolio_equity(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    net_external_flow = (
        get_net_cash_flows(connection, portfolio_id, "2026-01-05")
        - get_net_cash_flows(connection, portfolio_id, "2026-01-02")
    )

    investment_profit = (
        ending_equity - starting_equity - net_external_flow
    )

    assert starting_equity == pytest.approx(1000)
    assert ending_equity == pytest.approx(1500)
    assert investment_profit == pytest.approx(0)

    connection.close()




def test_cash_flow_adjusted_return_excludes_deposits(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    result = get_cash_flow_adjusted_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
    )

    assert result == pytest.approx(0.0)

    connection.close()




def test_cash_flow_adjusted_return_excludes_withdrawals(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "WITHDRAWAL",
        300,
        "USD",
        "test",
    )

    result = get_cash_flow_adjusted_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
    )

    assert result == pytest.approx(0.0)

    connection.close()



def test_cash_flow_adjusted_return_rejects_zero_starting_equity(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    with pytest.raises(
        ValueError,
        match="Starting portfolio equity must be positive",
    ):
        get_cash_flow_adjusted_return(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-05",
        )

    connection.close()





def test_get_cash_flow_dates_respects_period(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    for date, flow_type, amount in [
        ("2026-01-02", "DEPOSIT", 1000),
        ("2026-01-05", "DEPOSIT", 500),
        ("2026-01-05", "WITHDRAWAL", 100),
        ("2026-01-10", "WITHDRAWAL", 200),
        ("2026-01-15", "DEPOSIT", 300),
    ]:
        add_cash_flow(
            connection,
            portfolio_id,
            date,
            flow_type,
            amount,
            "USD",
            "test",
        )

    result = get_cash_flow_dates(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-10",
    )

    assert result == [
        "2026-01-05",
        "2026-01-10",
    ]

    connection.close()




def test_get_cash_flow_on_date_returns_net_flow(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "WITHDRAWAL",
        200,
        "USD",
        "test",
    )

    assert get_cash_flow_on_date(
        connection,
        portfolio_id,
        "2026-01-05",
    ) == 300

    assert get_cash_flow_on_date(
        connection,
        portfolio_id,
        "2026-01-03",
    ) == 0

    connection.close()




def test_daily_portfolio_return_adjusts_for_deposit(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100,
        100,
        100,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        103,
        103,
        103,
        103,
        103,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_subperiod_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
    )

    assert result == pytest.approx(0.03)

    connection.close()




def test_daily_portfolio_return_adjusts_for_withdrawal(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "WITHDRAWAL",
        300,
        "USD",
        "test",
    )

    result = get_portfolio_subperiod_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
    )

    assert result == pytest.approx(0.0)

    connection.close()




def test_daily_portfolio_return_without_cash_flows(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100,
        100,
        100,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-05",
        110,
        110,
        110,
        110,
        110,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_subperiod_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
    )

    assert result == pytest.approx(0.10)

    connection.close()





@pytest.mark.parametrize(
    "previous_date, current_date",
    [
        ("2026-01-05", "2026-01-05"),
        ("2026-01-05", "2026-01-02"),
    ],
)
def test_daily_portfolio_return_rejects_invalid_dates(
    tmp_path,
    previous_date,
    current_date,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    with pytest.raises(
        ValueError,
        match="Current date must be after previous date",
    ):
        get_portfolio_subperiod_return(
            connection,
            portfolio_id,
            previous_date,
            current_date,
        )

    connection.close()





def test_daily_portfolio_return_rejects_intermediate_cash_flows(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    with pytest.raises(
        ValueError,
        match="Intermediate cash flows require separate return periods",
    ):
        get_portfolio_subperiod_return(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-06",
        )

    connection.close()





def test_daily_portfolio_return_rejects_zero_starting_capital(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    with pytest.raises(
        ValueError,
        match="Starting capital must be positive",
    ):
        get_portfolio_subperiod_return(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-05",
        )

    connection.close()




def test_time_weighted_return_compounds_without_cash_flows(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    for date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
        ("2026-01-06", 121),
    ]:
        add_price(
            connection,
            instrument_id,
            date,
            price,
            price,
            price,
            price,
            price,
            1000,
            "test",
            f"{date}T00:00:00+00:00",
        )

    result = get_time_weighted_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-06",
    )

    assert result == pytest.approx(0.21)

    connection.close() 





def test_time_weighted_return_with_intermediate_deposit(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    for date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
        ("2026-01-06", 121),
    ]:
        add_price(
            connection,
            instrument_id,
            date,
            price,
            price,
            price,
            price,
            price,
            1000,
            "test",
            f"{date}T00:00:00+00:00",
        )

    result = get_time_weighted_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-06",
    )

    assert result == pytest.approx(
    (1100 / 1000) * (1710 / 1600) - 1
   ) 

    connection.close()





def test_twr_deposit_day_market_gain_is_not_distorted(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-05",
        "DEPOSIT",
        500,
        "USD",
        "test",
    )

    for date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
    ]:
        add_price(
            connection,
            instrument_id,
            date,
            price,
            price,
            price,
            price,
            price,
            1000,
            "test",
            f"{date}T00:00:00+00:00",
        )

    result = get_time_weighted_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
    )

    assert result == pytest.approx(0.10)

    connection.close()




def test_get_portfolio_price_dates(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    for date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 105),
        ("2026-01-06", 110),
        ("2026-01-07", 115),
    ]:
        add_price(
            connection,
            instrument_id,
            date,
            price,
            price,
            price,
            price,
            price,
            1000,
            "test",
            f"{date}T00:00:00+00:00",
        )

    result = get_portfolio_price_dates(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-06",
    )

    assert result == [
        "2026-01-05",
        "2026-01-06",
    ]

    connection.close()




def test_get_portfolio_price_dates_without_prices(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    result = get_portfolio_price_dates(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-10",
    )

    assert result == []

    connection.close()





def test_portfolio_price_dates_with_partial_coverage(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    for instrument_id in [aapl_id, msft_id]:
        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2026-01-02",
            "BUY",
            5,
            100,
            "USD",
            "test",
        )

    for instrument_id, date, price in [
        (aapl_id, "2026-01-02", 100),
        (msft_id, "2026-01-02", 100),
        (aapl_id, "2026-01-05", 105),
        (aapl_id, "2026-01-06", 110),
        (msft_id, "2026-01-06", 108),
    ]:
        add_price(
            connection,
            instrument_id,
            date,
            price,
            price,
            price,
            price,
            price,
            1000,
            "test",
            f"{date}T00:00:00+00:00",
        )

    result = get_portfolio_price_dates(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-06",
    )

    assert result == [
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
    ]

    connection.close()





def test_portfolio_price_coverage_detects_missing_holding(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    for instrument_id in [aapl_id, msft_id]:
        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2026-01-02",
            "BUY",
            5,
            100,
            "USD",
            "test",
        )

    add_price(
        connection,
        aapl_id,
        "2026-01-05",
        105,
        105,
        105,
        105,
        105,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert result == {
        "valuation_date": "2026-01-05",
        "total_holdings": 2,
        "priced_holdings": 1,
        "missing_instrument_ids": [msft_id],
        "is_complete": False,
    }

    connection.close()





def test_portfolio_price_coverage_complete(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    for instrument_id in [aapl_id, msft_id]:
        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2026-01-02",
            "BUY",
            5,
            100,
            "USD",
            "test",
        )

    for instrument_id, price in [
        (aapl_id, 105),
        (msft_id, 108),
    ]:
        add_price(
            connection,
            instrument_id,
            "2026-01-05",
            price,
            price,
            price,
            price,
            price,
            1000,
            "test",
            "2026-01-05T00:00:00+00:00",
        )

    result = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert result == {
        "valuation_date": "2026-01-05",
        "total_holdings": 2,
        "priced_holdings": 2,
        "missing_instrument_ids": [],
        "is_complete": True,
    }

    connection.close()




def test_subperiod_return_rejects_incomplete_price_coverage(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    for instrument_id in [aapl_id, msft_id]:
        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2026-01-02",
            "BUY",
            5,
            100,
            "USD",
            "test",
        )

    for instrument_id in [aapl_id, msft_id]:
        add_price(
            connection,
            instrument_id,
            "2026-01-02",
            100,
            100,
            100,
            100,
            100,
            1000,
            "test",
            "2026-01-02T00:00:00+00:00",
        )

    add_price(
        connection,
        aapl_id,
        "2026-01-05",
        105,
        105,
        105,
        105,
        105,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    with pytest.raises(
        ValueError,
        match="Incomplete price coverage on 2026-01-05",
    ):
        get_portfolio_subperiod_return(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-05",
        )

    connection.close()




def test_price_coverage_excludes_fully_sold_holdings(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    for instrument_id in [aapl_id, msft_id]:
        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2026-01-02",
            "BUY",
            5,
            100,
            "USD",
            "test",
        )

    add_transaction(
        connection,
        portfolio_id,
        msft_id,
        "2026-01-05",
        "SELL",
        5,
        110,
        "USD",
        "test",
    )

    add_price(
        connection,
        aapl_id,
        "2026-01-05",
        105,
        105,
        105,
        105,
        105,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    result = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert result == {
        "valuation_date": "2026-01-05",
        "total_holdings": 1,
        "priced_holdings": 1,
        "missing_instrument_ids": [],
        "is_complete": True,
    }

    connection.close()





def test_price_coverage_on_non_trading_day(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100,
        100,
        100,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    result = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-03",
    )

    assert result == {
        "valuation_date": "2026-01-03",
        "total_holdings": 1,
        "priced_holdings": 0,
        "missing_instrument_ids": [instrument_id],
        "is_complete": False,
    }

    connection.close()





def test_get_price_with_max_age_weekend(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100,
        100,
        100,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    accepted = get_price_with_max_age(
        connection,
        instrument_id,
        "2026-01-03",
        max_age_days=1,
    )

    assert accepted == {
    "instrument_id": instrument_id,
    "valuation_date": "2026-01-03",
    "price_date": "2026-01-02",
    "close": 100.0,
    "adjusted_close": 100.0,
    "age_days": 1,
    "is_stale": True,
    }

    rejected = get_price_with_max_age(
        connection,
        instrument_id,
        "2026-01-03",
        max_age_days=0,
    )

    assert rejected is None

    connection.close()




def test_get_price_with_max_age_rejects_old_price(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100,
        100,
        100,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    rejected = get_price_with_max_age(
        connection,
        instrument_id,
        "2026-01-06",
        max_age_days=3,
    )

    assert rejected is None

    accepted = get_price_with_max_age(
        connection,
        instrument_id,
        "2026-01-06",
        max_age_days=4,
    )

    assert accepted == {
    "instrument_id": instrument_id,
    "valuation_date": "2026-01-06",
    "price_date": "2026-01-02",
    "close": 100.0,
    "adjusted_close": 100.0,
    "age_days": 4,
    "is_stale": True,
    }

    connection.close()






def test_portfolio_price_coverage_allows_recent_stale_prices(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    aapl_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    msft_id = add_instrument(
        connection,
        "MSFT",
        "Microsoft Corporation",
        "STOCK",
        "USD",
    )

    for instrument_id in [aapl_id, msft_id]:
        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2026-01-02",
            "BUY",
            5,
            100,
            "USD",
            "test",
        )

    # AAPL has a fresh price on January 5.
    add_price(
        connection,
        aapl_id,
        "2026-01-05",
        105,
        105,
        105,
        105,
        105,
        1000,
        "test",
        "2026-01-05T00:00:00+00:00",
    )

    # MSFT's latest price is from January 2.
    add_price(
        connection,
        msft_id,
        "2026-01-02",
        100,
        100,
        100,
        100,
        100,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    strict = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-05",
        max_age_days=0,
    )

    assert strict["is_complete"] is False
    assert strict["missing_instrument_ids"] == [msft_id]

    permitted = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-05",
        max_age_days=3,
    )

    assert permitted == {
        "valuation_date": "2026-01-05",
        "total_holdings": 2,
        "priced_holdings": 2,
        "missing_instrument_ids": [],
        "is_complete": True,
    }

    connection.close()





def test_price_coverage_rejects_negative_max_age(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    with pytest.raises(
        ValueError,
        match="max_age_days must not be negative",
    ):
        get_portfolio_price_coverage(
            connection,
            portfolio_id,
            "2026-01-05",
            max_age_days=-1,
        )

    connection.close()


@pytest.mark.parametrize(
    "invalid_age",
    [1.5, "3", None, True],
)
def test_price_coverage_rejects_invalid_max_age(
    tmp_path,
    invalid_age,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    with pytest.raises(
        ValueError,
        match="max_age_days must be an integer",
    ):
        get_portfolio_price_coverage(
            connection,
            portfolio_id,
            "2026-01-05",
            max_age_days=invalid_age,
        )

    connection.close()


def test_price_coverage_empty_portfolio(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    result = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-05",
    )

    assert result == {
        "valuation_date": "2026-01-05",
        "total_holdings": 0,
        "priced_holdings": 0,
        "missing_instrument_ids": [],
        "is_complete": True,
    }

    connection.close()





@pytest.mark.parametrize(
    "max_age_days, should_succeed",
    [
        (0, False),
        (2, False),
        (3, True),
        (5, True),
    ],
)
def test_portfolio_valuation_price_age_limits(
    tmp_path,
    max_age_days,
    should_succeed,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )

    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        110, 110, 110, 110, 110,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    if should_succeed:
        result = get_portfolio_value(
            connection,
            portfolio_id,
            "2026-01-05",
            max_age_days=max_age_days,
        )

        assert len(result) == 1
        assert result.iloc[0]["ticker"] == "AAPL"
        assert result.iloc[0]["price_date"] == pd.Timestamp("2026-01-02")
        assert result.iloc[0]["market_value"] == pytest.approx(1100)

    else:
        with pytest.raises(
            ValueError,
            match="Price exceeds maximum permitted age",
        ):
            get_portfolio_value(
                connection,
                portfolio_id,
                "2026-01-05",
                max_age_days=max_age_days,
            )

    connection.close()


@pytest.mark.parametrize(
    "invalid_age",
    [-1, 1.5, "3", True],
)
def test_portfolio_valuation_rejects_invalid_age(
    tmp_path,
    invalid_age,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )

    with pytest.raises(ValueError):
        get_portfolio_value(
            connection,
            portfolio_id,
            "2026-01-05",
            max_age_days=invalid_age,
        )

    connection.close()


def test_portfolio_valuation_default_allows_old_prices(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )

    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        110, 110, 110, 110, 110,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    result = get_portfolio_value(
        connection,
        portfolio_id,
        "2026-02-01",
    )

    assert result.iloc[0]["price_date"] == pd.Timestamp("2026-01-02")
    assert result.iloc[0]["market_value"] == pytest.approx(1100)

    connection.close()


def test_portfolio_valuation_matches_price_coverage(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )

    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        110, 110, 110, 110, 110,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    coverage = get_portfolio_price_coverage(
        connection,
        portfolio_id,
        "2026-01-05",
        max_age_days=3,
    )

    valuation = get_portfolio_value(
        connection,
        portfolio_id,
        "2026-01-05",
        max_age_days=3,
    )

    assert coverage["is_complete"] is True
    assert valuation.iloc[0]["price_date"] == pd.Timestamp("2026-01-02")
    assert valuation.iloc[0]["market_value"] == pytest.approx(1100)

    connection.close()




def test_price_with_max_age_returns_raw_and_adjusted_close(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100, 105, 95, 100, 97,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    result = get_price_with_max_age(
        connection,
        instrument_id,
        "2026-01-05",
        max_age_days=3,
    )

    assert result is not None
    assert result["close"] == pytest.approx(100)
    assert result["adjusted_close"] == pytest.approx(97)
    assert result["price_date"] == "2026-01-02"
    assert result["age_days"] == 3
    assert result["is_stale"] is True

    connection.close()


def test_portfolio_valuation_uses_adjusted_close(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection,
        "Test Portfolio",
        "USD",
    )

    instrument_id = add_instrument(
        connection,
        "AAPL",
        "Apple Inc.",
        "STOCK",
        "USD",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    add_price(
        connection,
        instrument_id,
        "2026-01-02",
        100, 105, 95, 100, 97,
        1000,
        "test",
        "2026-01-02T00:00:00+00:00",
    )

    valuation = get_portfolio_value(
        connection,
        portfolio_id,
        "2026-01-05",
        max_age_days=3,
    )

    assert valuation.iloc[0]["adjusted_close"] == pytest.approx(97)
    assert valuation.iloc[0]["market_value"] == pytest.approx(970)
    assert valuation.iloc[0]["price_date"] == pd.Timestamp("2026-01-02")

    connection.close()





@pytest.mark.parametrize(
    "function_name",
    [
        "holdings",
        "equity",
    ],
)
def test_portfolio_totals_enforce_price_age(
    tmp_path,
    function_name,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY", 10, 100, "USD", "test"
    )

    add_price(
        connection, instrument_id, "2026-01-02",
        100, 100, 100, 110, 110,
        1000, "test", "2026-01-02T00:00:00+00:00"
    )

    function = (
        get_total_portfolio_value
        if function_name == "holdings"
        else get_total_portfolio_equity
    )

    with pytest.raises(ValueError, match="maximum permitted age"):
        function(
            connection, portfolio_id,
            "2026-01-05", max_age_days=2
        )

    result = function(
        connection, portfolio_id,
        "2026-01-05", max_age_days=3
    )

    if function_name == "holdings":
        assert result == pytest.approx(1100)
    else:
        # BUY costs 1000; no external deposit was recorded.
        assert result == pytest.approx(100)

    connection.close()


@pytest.mark.parametrize(
    "max_age_days,should_succeed",
    [
        (0, False),
        (1, True),
        (3, True),
    ],
)
def test_subperiod_return_price_age_policy(
    tmp_path,
    max_age_days,
    should_succeed,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        1000, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY", 10, 100, "USD", "test"
    )

    for price_date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
    ]:
        add_price(
            connection, instrument_id, price_date,
            price, price, price, price, price,
            1000, "test", f"{price_date}T00:00:00+00:00"
        )

    if should_succeed:
        result = get_portfolio_subperiod_return(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-06",
            max_age_days=max_age_days,
        )
        assert result == pytest.approx(0.10)
    else:
        with pytest.raises(ValueError, match="Incomplete price coverage"):
            get_portfolio_subperiod_return(
                connection,
                portfolio_id,
                "2026-01-02",
                "2026-01-06",
                max_age_days=max_age_days,
            )

    connection.close()


def test_twr_propagates_price_age_policy(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        1000, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY", 10, 100, "USD", "test"
    )

    for price_date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
    ]:
        add_price(
            connection, instrument_id, price_date,
            price, price, price, price, price,
            1000, "test", f"{price_date}T00:00:00+00:00"
        )

    with pytest.raises(ValueError, match="Incomplete price coverage"):
        get_time_weighted_return(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-06",
        )

    result = get_time_weighted_return(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-06",
        max_age_days=1,
    )

    assert result == pytest.approx(0.10)

    connection.close()




@pytest.mark.parametrize(
    "function_name",
    ["weights", "return", "cash_flow_adjusted_return"],
)
def test_remaining_portfolio_functions_price_age_policy(
    tmp_path,
    function_name,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection,
        portfolio_id,
        "2026-01-02",
        "DEPOSIT",
        1000,
        "USD",
        "test",
    )

    add_transaction(
        connection,
        portfolio_id,
        instrument_id,
        "2026-01-02",
        "BUY",
        10,
        100,
        "USD",
        "test",
    )

    for price_date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
    ]:
        add_price(
            connection,
            instrument_id,
            price_date,
            price, price, price, price, price,
            1000,
            "test",
            f"{price_date}T00:00:00+00:00",
        )

    if function_name == "weights":
        def calculate(max_age_days=None):
            return get_portfolio_weights(
                connection,
                portfolio_id,
                "2026-01-06",
                max_age_days=max_age_days,
            )

        def check_result(result):
            assert len(result) == 1
            assert result.iloc[0]["weight"] == pytest.approx(1.0)
            assert result.iloc[0]["market_value"] == pytest.approx(1100)

    elif function_name == "return":
        def calculate(max_age_days=None):
            return get_portfolio_return(
                connection,
                portfolio_id,
                "2026-01-02",
                "2026-01-06",
                max_age_days=max_age_days,
            )

        def check_result(result):
            assert result == pytest.approx(0.10)

    else:
        def calculate(max_age_days=None):
            return get_cash_flow_adjusted_return(
                connection,
                portfolio_id,
                "2026-01-02",
                "2026-01-06",
                max_age_days=max_age_days,
            )

        def check_result(result):
            assert result == pytest.approx(0.10)

    # January 6 is one calendar day after the latest price.
    # An exact-date requirement must reject it.
    with pytest.raises(ValueError, match="maximum permitted age"):
        calculate(max_age_days=0)

    # One-day-old prices are permitted.
    check_result(calculate(max_age_days=1))

    # The existing unrestricted default must still work.
    check_result(calculate())

    connection.close()





def test_equity_curve_tracks_historical_positions(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        2000, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY",
        10, 100, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-05", "BUY",
        5, 110, "USD", "test"
    )

    for price_date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
        ("2026-01-06", 120),
    ]:
        add_price(
            connection, instrument_id, price_date,
            price, price, price, price, price,
            1000, "test", f"{price_date}T00:00:00+00:00"
        )

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-06",
    )

    assert len(curve) == 3

    assert curve["active_holdings"].tolist() == [1, 1, 1]

    assert curve["holdings_value"].tolist() == pytest.approx(
        [1000, 1650, 1800]
    )

    assert curve["cash_balance"].tolist() == pytest.approx(
        [1000, 450, 450]
    )

    assert curve["total_equity"].tolist() == pytest.approx(
        [2000, 2100, 2250]
    )

    connection.close()


def test_equity_curve_includes_cash_only_portfolio(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Cash Portfolio", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        1000, "USD", "test"
    )

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-06",
    )

    assert len(curve) == 2
    assert curve["holdings_value"].tolist() == pytest.approx(
        [0, 0]
    )
    assert curve["cash_balance"].tolist() == pytest.approx(
        [1000, 1000]
    )
    assert curve["total_equity"].tolist() == pytest.approx(
        [1000, 1000]
    )
    assert curve["active_holdings"].tolist() == [0, 0]

    connection.close()


def test_equity_curve_rejects_missing_price_coverage(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY",
        10, 100, "USD", "test"
    )

    add_price(
        connection, instrument_id, "2026-01-02",
        100, 100, 100, 100, 100,
        1000, "test", "2026-01-02T00:00:00+00:00"
    )

    with pytest.raises(
        ValueError,
        match="Incomplete price coverage",
    ):
        get_portfolio_equity_curve(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-03",
        )

    connection.close()


def test_equity_curve_allows_recent_prices(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Test Portfolio", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        1000, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY",
        10, 100, "USD", "test"
    )

    add_price(
        connection, instrument_id, "2026-01-02",
        100, 100, 100, 100, 100,
        1000, "test", "2026-01-02T00:00:00+00:00"
    )

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-03",
        max_age_days=1,
    )

    assert len(curve) == 2
    assert curve["total_equity"].tolist() == pytest.approx(
        [1000, 1000]
    )

    connection.close()




def test_equity_curve_includes_cash_flow_dates(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Cash Flow Dates", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        1000, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY",
        10, 100, "USD", "test"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-03", "DEPOSIT",
        500, "USD", "test"
    )

    for price_date, price in [
        ("2026-01-02", 100),
        ("2026-01-05", 110),
    ]:
        add_price(
            connection, instrument_id, price_date,
            price, price, price, price, price,
            1000, "test", f"{price_date}T00:00:00+00:00"
        )

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-05",
        max_age_days=1,
    )

    assert curve["date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-01-02",
        "2026-01-03",
        "2026-01-05",
    ]

    assert curve["total_equity"].tolist() == pytest.approx(
        [1000, 1500, 1600]
    )

    connection.close()




def test_equity_curve_includes_transaction_dates(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Transaction Dates", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        2000, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY",
        10, 100, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-03", "BUY",
        5, 100, "USD", "test"
    )

    add_price(
        connection, instrument_id, "2026-01-02",
        100, 100, 100, 100, 100,
        1000, "test", "2026-01-02T00:00:00+00:00"
    )

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        "2026-01-02",
        "2026-01-03",
        max_age_days=1,
    )

    assert curve["date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-01-02",
        "2026-01-03",
    ]

    assert curve["holdings_value"].tolist() == pytest.approx(
        [1000, 1500]
    )

    assert curve["cash_balance"].tolist() == pytest.approx(
        [1000, 500]
    )

    assert curve["total_equity"].tolist() == pytest.approx(
        [2000, 2000]
    )

    connection.close()






def test_equity_curve_cash_flow_date_requires_price_coverage(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    portfolio_id = add_portfolio(
        connection, "Strict Coverage", "USD"
    )
    instrument_id = add_instrument(
        connection, "AAPL", "Apple Inc.", "STOCK", "USD"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-02", "DEPOSIT",
        1000, "USD", "test"
    )

    add_transaction(
        connection, portfolio_id, instrument_id,
        "2026-01-02", "BUY",
        10, 100, "USD", "test"
    )

    add_cash_flow(
        connection, portfolio_id,
        "2026-01-03", "DEPOSIT",
        500, "USD", "test"
    )

    add_price(
        connection, instrument_id, "2026-01-02",
        100, 100, 100, 100, 100,
        1000, "test", "2026-01-02T00:00:00+00:00"
    )

    with pytest.raises(
        ValueError,
        match="Incomplete price coverage on 2026-01-03",
    ):
        get_portfolio_equity_curve(
            connection,
            portfolio_id,
            "2026-01-02",
            "2026-01-03",
            max_age_days=0,
        )

    connection.close()