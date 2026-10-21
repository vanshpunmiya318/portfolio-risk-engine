import pytest

from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema
from riskengine.db.instruments import add_instrument
from riskengine.db.portfolios import add_portfolio, get_portfolio_value
from riskengine.db.transactions import (
    add_transaction,
    get_positions,
    validate_transactions,
    get_cost_basis,
    get_cash_balance,
)


def test_ten_for_one_split_updates_position(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    try:
        portfolio_id = add_portfolio(
            connection, "Test Portfolio", "USD"
        )
        instrument_id = add_instrument(
            connection, "NVDA", "NVIDIA", "STOCK", "USD"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-06-07", "BUY", 10, 1200, "USD", "test",
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-06-10", "SPLIT",
                10.0, "test", "2024-06-10T00:00:00+00:00",
            ),
        )

        before = get_positions(
            connection, portfolio_id, "2024-06-07"
        )
        after = get_positions(
            connection, portfolio_id, "2024-06-10"
        )

        assert before.iloc[0]["quantity"] == pytest.approx(10)
        assert after.iloc[0]["quantity"] == pytest.approx(100)

    finally:
        connection.close()



def test_post_split_sale_is_valid(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    try:
        portfolio_id = add_portfolio(
            connection, "Test Portfolio", "USD"
        )
        instrument_id = add_instrument(
            connection, "NVDA", "NVIDIA", "STOCK", "USD"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-06-07", "BUY", 10, 1200, "USD", "test",
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-06-10", "SPLIT",
                10.0, "test", "2024-06-10T00:00:00+00:00",
            ),
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-06-11", "SELL", 100, 120, "USD", "test",
        )

        assert validate_transactions(connection, portfolio_id) is True

        positions = get_positions(
            connection, portfolio_id, "2024-06-11"
        )
        assert positions.iloc[0]["quantity"] == pytest.approx(0)

    finally:
        connection.close()






def test_stock_split_preserves_total_cost_basis(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    try:
        portfolio_id = add_portfolio(
            connection, "Test Portfolio", "USD"
        )
        instrument_id = add_instrument(
            connection, "NVDA", "NVIDIA", "STOCK", "USD"
        )

        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            "2024-06-07",
            "BUY",
            10,
            1200,
            "USD",
            "test",
        )

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
                instrument_id,
                "2024-06-10",
                "SPLIT",
                10.0,
                "test",
                "2024-06-10T00:00:00+00:00",
            ),
        )

        result = get_cost_basis(connection, portfolio_id)
        nvda = result.loc[result["ticker"] == "NVDA"].iloc[0]

        assert nvda["quantity"] == pytest.approx(100)
        assert nvda["total_cost"] == pytest.approx(12000)
        assert nvda["average_cost"] == pytest.approx(120)

    finally:
        connection.close()






def test_multiple_splits_with_intermediate_transactions(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    try:
        portfolio_id = add_portfolio(
            connection, "Test Portfolio", "USD"
        )
        instrument_id = add_instrument(
            connection, "TEST", "Test Stock", "STOCK", "USD"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-01-02", "BUY", 10, 100, "USD", "test"
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-01-10", "SPLIT",
                2.0, "test", "2024-01-10T00:00:00+00:00"
            ),
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-01-15", "BUY", 10, 60, "USD", "test"
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-01-20", "SPLIT",
                0.5, "test", "2024-01-20T00:00:00+00:00"
            ),
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-01-25", "SELL", 5, 120, "USD", "test"
        )

        assert validate_transactions(connection, portfolio_id) is True

        positions = get_positions(connection, portfolio_id)
        assert positions.iloc[0]["quantity"] == pytest.approx(10)

        cost_basis = get_cost_basis(connection, portfolio_id)
        stock = cost_basis.iloc[0]

        assert stock["quantity"] == pytest.approx(10)
        assert stock["total_cost"] == pytest.approx(1066.6666666667)
        assert stock["average_cost"] == pytest.approx(106.6666666667)

    finally:
        connection.close()




def test_positions_respect_split_effective_dates(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    try:
        portfolio_id = add_portfolio(
            connection, "Test Portfolio", "USD"
        )
        instrument_id = add_instrument(
            connection, "TEST", "Test Stock", "STOCK", "USD"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-01-02", "BUY", 10, 100, "USD", "test"
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-01-10", "SPLIT",
                2.0, "test", "2024-01-10T00:00:00+00:00"
            ),
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-01-15", "BUY", 5, 60, "USD", "test"
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-01-20", "SPLIT",
                0.5, "test", "2024-01-20T00:00:00+00:00"
            ),
        )

        expected_positions = {
            "2024-01-09": 10,
            "2024-01-10": 20,
            "2024-01-14": 20,
            "2024-01-15": 25,
            "2024-01-19": 25,
            "2024-01-20": 12.5,
            "2024-01-25": 12.5,
        }

        for date, expected_quantity in expected_positions.items():
            positions = get_positions(
                connection, portfolio_id, as_of_date=date
            )

            assert positions.iloc[0]["quantity"] == pytest.approx(
                expected_quantity
            ), f"Incorrect position on {date}"

    finally:
        connection.close()





def test_sale_on_split_date_uses_post_split_shares(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    try:
        portfolio_id = add_portfolio(
            connection, "Test Portfolio", "USD"
        )
        instrument_id = add_instrument(
            connection, "NVDA", "NVIDIA", "STOCK", "USD"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-06-07", "BUY", 10, 1200, "USD", "test"
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-06-10", "SPLIT",
                10.0, "test", "2024-06-10T00:00:00+00:00"
            ),
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-06-10", "SELL", 40, 125, "USD", "test"
        )

        assert validate_transactions(connection, portfolio_id) is True

        positions = get_positions(
            connection, portfolio_id, "2024-06-10"
        )
        assert positions.iloc[0]["quantity"] == pytest.approx(60)

        cost_basis = get_cost_basis(connection, portfolio_id)
        nvda = cost_basis.iloc[0]

        assert nvda["quantity"] == pytest.approx(60)
        assert nvda["total_cost"] == pytest.approx(7200)
        assert nvda["average_cost"] == pytest.approx(120)

    finally:
        connection.close()





def test_stock_split_does_not_change_cash_balance(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)
    connection = get_connection(db_path)

    try:
        portfolio_id = add_portfolio(
            connection, "Test Portfolio", "USD"
        )
        instrument_id = add_instrument(
            connection, "NVDA", "NVIDIA", "STOCK", "USD"
        )

        add_transaction(
            connection, portfolio_id, instrument_id,
            "2024-06-07", "BUY", 10, 1200, "USD", "test"
        )

        cash_before = get_cash_balance(
            connection, portfolio_id, "2024-06-07"
        )

        connection.execute(
            """
            INSERT INTO corporate_actions (
                instrument_id, ex_date, action_type,
                ratio, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id, "2024-06-10", "SPLIT",
                10.0, "test", "2024-06-10T00:00:00+00:00"
            ),
        )

        cash_after = get_cash_balance(
            connection, portfolio_id, "2024-06-10"
        )

        assert cash_after == pytest.approx(cash_before)

        positions = get_positions(
            connection, portfolio_id, "2024-06-10"
        )
        assert positions.iloc[0]["quantity"] == pytest.approx(100)

    finally:
        connection.close()





def test_tiingo_raw_price_valuation_after_split(tmp_path):
    import sqlite3

    from riskengine.db.portfolios import get_portfolio_value

    db_path = tmp_path / "valuation.db"

    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE portfolios (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE instruments (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                name TEXT,
                asset_type TEXT,
                currency TEXT
            );

            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                portfolio_id INTEGER NOT NULL,
                instrument_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                fees REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE corporate_actions (
                id INTEGER PRIMARY KEY,
                instrument_id INTEGER NOT NULL,
                ex_date TEXT NOT NULL,
                action_type TEXT NOT NULL,
                ratio REAL NOT NULL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

                        CREATE TABLE prices (
                id INTEGER PRIMARY KEY,
                instrument_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                adjusted_close REAL NOT NULL,
                volume INTEGER,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE corporate_action_coverage (
                id INTEGER PRIMARY KEY,
                instrument_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
        """)

        connection.execute(
            "INSERT INTO portfolios VALUES (1, 'Test')"
        )
        connection.execute(
            """
            INSERT INTO instruments
            VALUES (1, 'NVDA', 'NVIDIA', 'STOCK', 'USD')
            """
        )
        connection.execute(
            """
            INSERT INTO transactions
            (portfolio_id, instrument_id, date,
             transaction_type, quantity, price, fees)
            VALUES (1, 1, '2024-06-07', 'BUY', 1, 1208.88, 0)
            """
        )
        connection.execute(
            """
            INSERT INTO corporate_actions
            (instrument_id, ex_date, action_type,
             ratio, source, fetched_at)
            VALUES (1, '2024-06-10', 'SPLIT',
                    10, 'tiingo', '2024-06-11')
            """
        )
        connection.execute(
            """
            INSERT INTO prices
            (instrument_id, date, open, high, low,
             close, adjusted_close, volume, source, fetched_at)
            VALUES (1, '2024-06-10',
                    120.37, 123.10, 117.01,
                    121.79, 121.4441675807,
                    314162700, 'tiingo', '2024-06-11')
            """
        )

        connection.execute(
            """
            INSERT INTO corporate_action_coverage
                (instrument_id, start_date, end_date, source, fetched_at)
            VALUES (1, '2024-06-07', '2024-06-10',
                    'tiingo', '2024-06-11')
            """
        )

        result = get_portfolio_value(
            connection,
            portfolio_id=1,
            as_of_date="2024-06-10",
        )

    assert result.iloc[0]["quantity"] == pytest.approx(10)
    assert result.iloc[0]["market_value"] == pytest.approx(1217.90)





def test_corporate_action_coverage_schema(tmp_path):
    import sqlite3
    import pytest
    from pathlib import Path

    db_path = tmp_path / "coverage_test.db"

    connection = sqlite3.connect(db_path)
    connection.execute(
        "PRAGMA foreign_keys = ON"
    )

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "riskengine"
        / "db"
        / "schema"
        / "portfolios.sql"
    )

    connection.executescript(
        schema_path.read_text(encoding="utf-8")
    )

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table'"
        )
    }

    assert "corporate_action_coverage" in tables

    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(corporate_action_coverage)"
        )
    }

    assert {
        "instrument_id",
        "start_date",
        "end_date",
        "source",
        "fetched_at",
    }.issubset(columns)


    connection.execute(
        """
        CREATE TABLE instruments (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL UNIQUE
        )
        """
    )

    connection.execute(
        "INSERT INTO instruments (ticker) VALUES (?)",
        ("NVDA",),
    )

    instrument_id = connection.execute(
        "SELECT id FROM instruments WHERE ticker = ?",
        ("NVDA",),
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO corporate_action_coverage (
                instrument_id,
                start_date,
                end_date,
                source,
                fetched_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                instrument_id,
                "2024-07-01",
                "2024-06-01",
                "tiingo",
                "2026-10-20T12:00:00",
            ),
        )

    connection.close()




def test_corporate_action_coverage_detects_missing_history(tmp_path):
    import sqlite3
    from pathlib import Path

    db_path = tmp_path / "coverage_gap.db"
    connection = sqlite3.connect(db_path)

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "riskengine"
        / "db"
        / "schema"
        / "portfolios.sql"
    )

    connection.executescript(
        schema_path.read_text(encoding="utf-8")
    )

    connection.execute(
        """
        CREATE TABLE instruments (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL UNIQUE
        )
        """
    )

    connection.execute(
        "INSERT INTO instruments (ticker) VALUES ('NVDA')"
    )

    instrument_id = connection.execute(
        "SELECT id FROM instruments WHERE ticker = 'NVDA'"
    ).fetchone()[0]

    connection.execute(
        """
        INSERT INTO corporate_action_coverage (
            instrument_id, start_date, end_date,
            source, fetched_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            instrument_id,
            "2024-07-01",
            "2024-12-31",
            "tiingo",
            "2026-10-20T12:00:00",
        ),
    )

    from riskengine.db.corporate_actions import (
        validate_corporate_action_coverage,
    )

    with pytest.raises(ValueError, match="coverage"):
        validate_corporate_action_coverage(
            connection,
            instrument_id,
            "2024-01-01",
            "2024-12-31",
        )

    connection.close()





def test_corporate_action_coverage_adjacent_intervals_and_gaps(tmp_path):
    import sqlite3
    import pytest

    from riskengine.db.corporate_actions import (
        validate_corporate_action_coverage,
    )

    connection = sqlite3.connect(tmp_path / "intervals.db")

    connection.execute(
        """
        CREATE TABLE corporate_action_coverage (
            instrument_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            source TEXT,
            fetched_at TEXT
        )
        """
    )

    connection.executemany(
        """
        INSERT INTO corporate_action_coverage
        VALUES (1, ?, ?, 'tiingo', '2026-10-20')
        """,
        [
            ("2024-01-01", "2024-06-30"),
            ("2024-07-01", "2024-12-31"),
        ],
    )

    assert validate_corporate_action_coverage(
        connection, 1, "2024-01-01", "2024-12-31"
    ) is True

    connection.execute(
        """
        UPDATE corporate_action_coverage
        SET start_date = '2024-07-02'
        WHERE start_date = '2024-07-01'
        """
    )

    with pytest.raises(ValueError, match="coverage"):
        validate_corporate_action_coverage(
            connection, 1, "2024-01-01", "2024-12-31"
        )

    connection.close()




def test_corporate_action_coverage_adjacent_intervals_and_gaps(tmp_path):
    import sqlite3
    import pytest

    from riskengine.db.corporate_actions import (
        validate_corporate_action_coverage,
    )

    connection = sqlite3.connect(tmp_path / "intervals.db")

    connection.execute(
        """
        CREATE TABLE corporate_action_coverage (
            instrument_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            source TEXT,
            fetched_at TEXT
        )
        """
    )

    connection.executemany(
        """
        INSERT INTO corporate_action_coverage
        VALUES (1, ?, ?, 'tiingo', '2026-10-20')
        """,
        [
            ("2024-01-01", "2024-06-30"),
            ("2024-07-01", "2024-12-31"),
        ],
    )

    assert validate_corporate_action_coverage(
        connection, 1, "2024-01-01", "2024-12-31"
    ) is True

    connection.execute(
        """
        UPDATE corporate_action_coverage
        SET start_date = '2024-07-02'
        WHERE start_date = '2024-07-01'
        """
    )

    with pytest.raises(ValueError, match="coverage"):
        validate_corporate_action_coverage(
            connection, 1, "2024-01-01", "2024-12-31"
        )

    connection.close()




def test_corporate_action_coverage_overlapping_intervals(tmp_path):
    import sqlite3

    from riskengine.db.corporate_actions import (
        validate_corporate_action_coverage,
    )

    connection = sqlite3.connect(tmp_path / "overlap.db")

    connection.execute(
        """
        CREATE TABLE corporate_action_coverage (
            instrument_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            source TEXT,
            fetched_at TEXT
        )
        """
    )

    connection.executemany(
        """
        INSERT INTO corporate_action_coverage
        VALUES (1, ?, ?, 'tiingo', '2026-10-20')
        """,
        [
            ("2024-01-01", "2024-08-31"),
            ("2024-06-01", "2024-12-31"),
        ],
    )

    assert validate_corporate_action_coverage(
        connection, 1, "2024-01-01", "2024-12-31"
    ) is True

    connection.close()




def test_corporate_action_coverage_rejects_wrong_source(tmp_path):
    import sqlite3
    import pytest

    from riskengine.db.corporate_actions import (
        validate_corporate_action_coverage,
    )

    connection = sqlite3.connect(tmp_path / "source.db")

    connection.execute(
        """
        CREATE TABLE corporate_action_coverage (
            instrument_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            source TEXT,
            fetched_at TEXT
        )
        """
    )

    connection.execute(
        """
        INSERT INTO corporate_action_coverage
        VALUES (1, '2024-01-01', '2024-12-31',
                'yahoo_finance', '2026-10-20')
        """
    )

    with pytest.raises(ValueError, match="coverage"):
        validate_corporate_action_coverage(
            connection, 1, "2024-01-01", "2024-12-31"
        )

    connection.close()




def test_corporate_action_coverage_rejects_wrong_instrument(tmp_path):
    import sqlite3
    import pytest

    from riskengine.db.corporate_actions import (
        validate_corporate_action_coverage,
    )

    connection = sqlite3.connect(tmp_path / "instrument.db")

    connection.execute(
        """
        CREATE TABLE corporate_action_coverage (
            instrument_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            source TEXT,
            fetched_at TEXT
        )
        """
    )

    connection.execute(
        """
        INSERT INTO corporate_action_coverage
        VALUES (2, '2024-01-01', '2024-12-31',
                'tiingo', '2026-10-20')
        """
    )

    with pytest.raises(ValueError, match="coverage"):
        validate_corporate_action_coverage(
            connection, 1, "2024-01-01", "2024-12-31"
        )

    connection.close()




def test_corporate_action_coverage_rejects_missing_end(tmp_path):
    import sqlite3
    import pytest

    from riskengine.db.corporate_actions import (
        validate_corporate_action_coverage,
    )

    connection = sqlite3.connect(tmp_path / "missing_end.db")

    connection.execute(
        """
        CREATE TABLE corporate_action_coverage (
            instrument_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            source TEXT,
            fetched_at TEXT
        )
        """
    )

    connection.execute(
        """
        INSERT INTO corporate_action_coverage
        VALUES (1, '2024-01-01', '2024-06-30',
                'tiingo', '2026-10-20')
        """
    )

    with pytest.raises(ValueError, match="coverage"):
        validate_corporate_action_coverage(
            connection, 1, "2024-01-01", "2024-12-31"
        )

    connection.close()