"""Integration regression tests for Tiingo corporate-action coverage in portfolio valuation."""

import sqlite3

import pandas as pd
import pytest

import riskengine.db.portfolios as portfolios


@pytest.fixture
def valuation_context(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE instruments (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL);
        CREATE TABLE transactions (
            portfolio_id INTEGER NOT NULL,
            instrument_id INTEGER NOT NULL,
            date TEXT NOT NULL
        );
        CREATE TABLE corporate_action_coverage (
            instrument_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            source TEXT NOT NULL
        );
        INSERT INTO instruments VALUES (1, 'NVDA');
        INSERT INTO transactions VALUES (1, 1, '2024-06-07');
        """
    )

    monkeypatch.setattr(portfolios, "validate_transactions", lambda *_: None)
    monkeypatch.setattr(
        portfolios,
        "get_positions",
        lambda *_: pd.DataFrame({"ticker": ["NVDA"], "quantity": [10.0]}),
    )

    price_source = {"value": "tiingo"}

    def latest_prices(*_):
        return pd.DataFrame(
            {
                "ticker": ["NVDA"],
                "date": ["2024-06-10"],
                "close": [121.79],
                "adjusted_close": [121.4441675807],
                "source": [price_source["value"]],
            }
        )

    monkeypatch.setattr(portfolios, "get_latest_prices", latest_prices)
    yield connection, price_source
    connection.close()


def add_coverage(connection, start, end, source="tiingo"):
    connection.execute(
        """INSERT INTO corporate_action_coverage
           (instrument_id, start_date, end_date, source)
           VALUES (1, ?, ?, ?)""",
        (start, end, source),
    )


def test_tiingo_valuation_rejects_missing_coverage(valuation_context):
    connection, _ = valuation_context
    with pytest.raises(ValueError, match="Incomplete corporate action coverage"):
        portfolios.get_portfolio_value(connection, 1, "2024-06-10")


def test_tiingo_valuation_rejects_partial_coverage(valuation_context):
    connection, _ = valuation_context
    add_coverage(connection, "2024-06-07", "2024-06-08")
    add_coverage(connection, "2024-06-10", "2024-06-10")
    with pytest.raises(ValueError, match="Incomplete corporate action coverage"):
        portfolios.get_portfolio_value(connection, 1, "2024-06-10")


def test_tiingo_valuation_accepts_complete_coverage_and_raw_close(valuation_context):
    connection, _ = valuation_context
    add_coverage(connection, "2024-06-07", "2024-06-10")
    result = portfolios.get_portfolio_value(connection, 1, "2024-06-10")
    assert result.iloc[0]["quantity"] == pytest.approx(10)
    assert result.iloc[0]["market_value"] == pytest.approx(1217.90)


def test_tiingo_valuation_accepts_adjacent_coverage_intervals(valuation_context):
    connection, _ = valuation_context
    add_coverage(connection, "2024-06-07", "2024-06-08")
    add_coverage(connection, "2024-06-09", "2024-06-10")
    result = portfolios.get_portfolio_value(connection, 1, "2024-06-10")
    assert result.iloc[0]["market_value"] == pytest.approx(1217.90)


def test_test_source_does_not_require_tiingo_coverage(valuation_context):
    connection, price_source = valuation_context
    price_source["value"] = "test"
    result = portfolios.get_portfolio_value(connection, 1, "2024-06-10")
    assert result.iloc[0]["market_value"] == pytest.approx(1214.441675807)


def test_non_tiingo_coverage_does_not_authorize_tiingo_valuation(valuation_context):
    connection, _ = valuation_context
    add_coverage(connection, "2024-06-07", "2024-06-10", "yahoo_finance")
    with pytest.raises(ValueError, match="Incomplete corporate action coverage"):
        portfolios.get_portfolio_value(connection, 1, "2024-06-10")




def test_tiingo_valuation_accepts_coverage_across_weekend(
    valuation_context,
):
    connection, _ = valuation_context

    add_coverage(
        connection,
        "2024-06-07",
        "2024-06-10",
    )

    result = portfolios.get_portfolio_value(
        connection,
        1,
        "2024-06-10",
    )

    assert result.iloc[0]["market_value"] == pytest.approx(1217.90)




def test_coverage_is_not_created_by_valuation(valuation_context):
    connection, _ = valuation_context

    with pytest.raises(
        ValueError,
        match="Incomplete corporate action coverage",
    ):
        portfolios.get_portfolio_value(
            connection, 1, "2024-06-10"
        )

    count = connection.execute(
        "SELECT COUNT(*) FROM corporate_action_coverage"
    ).fetchone()[0]

    assert count == 0




def test_tiingo_ingestion_records_requested_coverage_without_splits(
    tmp_path, monkeypatch
):
    from riskengine.ingestion import tiingo_data
    from riskengine.db.connection import get_connection

    db_path = tmp_path / "tiingo_coverage.db"
    from pathlib import Path

    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "riskengine"
        / "db"
        / "schema"
    )

    with sqlite3.connect(db_path) as setup_connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            setup_connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-07", "2024-06-10"]),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "adjusted_close": [101.0, 102.0],
            "volume": [1000, 1100],
            "split_factor": [1.0, 1.0],
            "div_cash": [0.0, 0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    count = tiingo_data.ingest_tiingo_prices(
        db_path,
        "NVDA",
        "2024-06-07",
        "2024-06-11",
        "NVIDIA",
        "STOCK",
        "USD",
        "test-token",
    )

    assert count == 2

    with get_connection(db_path) as connection:
        coverage = connection.execute(
            """
            SELECT start_date, end_date, source
            FROM corporate_action_coverage
            """
        ).fetchall()

        splits = connection.execute(
            "SELECT COUNT(*) FROM corporate_actions"
        ).fetchone()[0]

    assert coverage == [
        ("2024-06-07", "2024-06-11", "tiingo")
    ]
    assert splits == 0





@pytest.mark.parametrize(
    "returned_dates",
    [
        ["2024-06-06", "2024-06-10"],
        ["2024-06-07", "2024-06-12"],
    ],
)
def test_tiingo_ingestion_rejects_out_of_range_prices(
    tmp_path, monkeypatch, returned_dates
):
    from riskengine.ingestion import tiingo_data

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(returned_dates),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "adjusted_close": [101.0, 102.0],
            "volume": [1000, 1100],
            "split_factor": [1.0, 1.0],
            "div_cash": [0.0, 0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    with pytest.raises(
        ValueError,
        match="outside the requested date range",
    ):
        tiingo_data.ingest_tiingo_prices(
            tmp_path / "out_of_range.db",
            "NVDA",
            "2024-06-07",
            "2024-06-11",
            "NVIDIA",
            "STOCK",
            "USD",
            "test-token",
        )




@pytest.mark.parametrize(
    "returned_dates",
    [
        [],
        ["2024-06-07", "2024-06-10"],
    ],
)
def test_tiingo_ingestion_coverage_is_idempotent(
    tmp_path, monkeypatch, returned_dates
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data
    from riskengine.db.connection import get_connection

    db_path = tmp_path / "idempotent.db"

    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "riskengine"
        / "db"
        / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    dates = pd.to_datetime(returned_dates)
    n = len(dates)

    prices = pd.DataFrame(
        {
            "date": dates,
            "open": [100.0] * n,
            "high": [102.0] * n,
            "low": [99.0] * n,
            "close": [101.0] * n,
            "adjusted_close": [101.0] * n,
            "volume": [1000] * n,
            "split_factor": [1.0] * n,
            "div_cash": [0.0] * n,
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    for _ in range(2):
        result = tiingo_data.ingest_tiingo_prices(
            db_path,
            "NVDA",
            "2024-06-07",
            "2024-06-11",
            "NVIDIA",
            "STOCK",
            "USD",
            "test-token",
        )
        assert result == n

    with get_connection(db_path) as connection:
        coverage_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_action_coverage"
        ).fetchone()[0]

        price_count = connection.execute(
            "SELECT COUNT(*) FROM prices"
        ).fetchone()[0]

        split_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_actions"
        ).fetchone()[0]

    assert coverage_count == (1 if n else 0)
    assert price_count == n
    assert split_count == 0


def test_tiingo_ingestion_rejects_duplicate_price_dates(
    tmp_path, monkeypatch
):
    from riskengine.ingestion import tiingo_data

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-06-10", "2024-06-10"]
            ),
            "open": [100.0, 100.0],
            "high": [102.0, 102.0],
            "low": [99.0, 99.0],
            "close": [101.0, 101.0],
            "adjusted_close": [101.0, 101.0],
            "volume": [1000, 1000],
            "split_factor": [1.0, 1.0],
            "div_cash": [0.0, 0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    with pytest.raises(ValueError, match="duplicate dates"):
        tiingo_data.ingest_tiingo_prices(
            tmp_path / "duplicates.db",
            "NVDA",
            "2024-06-07",
            "2024-06-11",
            "NVIDIA",
            "STOCK",
            "USD",
            "test-token",
        )




def test_tiingo_ingestion_rolls_back_when_split_insert_fails(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "rollback.db"

    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "riskengine"
        / "db"
        / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

        connection.execute(
            """
            CREATE TRIGGER reject_split
            BEFORE INSERT ON corporate_actions
            BEGIN
                SELECT RAISE(ABORT, 'simulated split failure');
            END;
            """
        )

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-07", "2024-06-10"]),
            "open": [100.0, 10.0],
            "high": [102.0, 11.0],
            "low": [99.0, 9.0],
            "close": [101.0, 10.0],
            "adjusted_close": [10.1, 10.0],
            "volume": [1000, 1100],
            "split_factor": [1.0, 10.0],
            "div_cash": [0.0, 0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="simulated split failure",
    ):
        tiingo_data.ingest_tiingo_prices(
            db_path,
            "NVDA",
            "2024-06-07",
            "2024-06-10",
            "NVIDIA",
            "STOCK",
            "USD",
            "test-token",
        )

    with sqlite3.connect(db_path) as connection:
        for table in (
            "instruments",
            "prices",
            "corporate_actions",
            "corporate_action_coverage",
        ):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

            assert count == 0, (
                f"{table} retained rows after failed ingestion"
            )





def test_tiingo_ingestion_persists_split_and_raw_prices(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "split_ingestion.db"

    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "riskengine"
        / "db"
        / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-06-07", "2024-06-10"]
            ),
            "open": [1197.70, 120.37],
            "high": [1216.92, 123.10],
            "low": [1180.22, 117.01],
            "close": [1208.88, 121.79],
            "adjusted_close": [
                120.5447288816,
                121.4441675807,
            ],
            "volume": [41200000, 314000000],
            "split_factor": [1.0, 10.0],
            "div_cash": [0.0, 0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    count = tiingo_data.ingest_tiingo_prices(
        db_path,
        "NVDA",
        "2024-06-07",
        "2024-06-10",
        "NVIDIA",
        "STOCK",
        "USD",
        "test-token",
    )

    assert count == 2

    with sqlite3.connect(db_path) as connection:
        stored_prices = connection.execute(
            """
            SELECT date, close, adjusted_close, source
            FROM prices
            ORDER BY date
            """
        ).fetchall()

        splits = connection.execute(
            """
            SELECT ex_date, action_type, ratio, source
            FROM corporate_actions
            """
        ).fetchall()

        coverage = connection.execute(
            """
            SELECT start_date, end_date, source
            FROM corporate_action_coverage
            """
        ).fetchall()

    assert len(stored_prices) == 2
    assert stored_prices[0][1] == pytest.approx(1208.88)
    assert stored_prices[0][2] == pytest.approx(120.5447288816)
    assert stored_prices[0][3] == "tiingo"

    assert stored_prices[1][1] == pytest.approx(121.79)
    assert stored_prices[1][2] == pytest.approx(121.4441675807)
    assert stored_prices[1][3] == "tiingo"

    assert splits == [
        ("2024-06-10", "SPLIT", 10.0, "tiingo")
    ]

    assert coverage == [
        ("2024-06-07", "2024-06-10", "tiingo")
    ]




def test_tiingo_reingestion_updates_existing_split_and_prices(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "reingestion.db"
    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src" / "riskengine" / "db" / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-07", "2024-06-10"]),
            "open": [1200.0, 120.0],
            "high": [1220.0, 125.0],
            "low": [1180.0, 115.0],
            "close": [1208.88, 121.79],
            "adjusted_close": [120.54, 121.44],
            "volume": [1000, 2000],
            "split_factor": [1.0, 10.0],
            "div_cash": [0.0, 0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    def ingest():
        return tiingo_data.ingest_tiingo_prices(
            db_path, "NVDA",
            "2024-06-07", "2024-06-10",
            "NVIDIA", "STOCK", "USD", "test-token",
        )

    assert ingest() == 2

    # Simulate corrected data returned by Tiingo.
    prices.loc[1, "close"] = 122.50
    prices.loc[1, "adjusted_close"] = 122.10
    prices.loc[1, "split_factor"] = 5.0

    assert ingest() == 2

    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            """
            SELECT close, adjusted_close
            FROM prices
            WHERE date = '2024-06-10'
            """
        ).fetchone()

        split = connection.execute(
            """
            SELECT ratio
            FROM corporate_actions
            WHERE ex_date = '2024-06-10'
            """
        ).fetchone()

        counts = [
            connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "prices",
                "corporate_actions",
                "corporate_action_coverage",
            )
        ]

    assert stored[0] == pytest.approx(122.50)
    assert stored[1] == pytest.approx(122.10)
    assert split[0] == pytest.approx(5.0)
    assert counts == [2, 1, 1]


def test_tiingo_reingestion_preserves_other_instruments(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "multi_instrument.db"
    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src" / "riskengine" / "db" / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-10"]),
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "adjusted_close": [105.0],
            "volume": [1000],
            "split_factor": [1.0],
            "div_cash": [0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    for ticker in ("AAPL", "NVDA"):
        tiingo_data.ingest_tiingo_prices(
            db_path, ticker,
            "2024-06-10", "2024-06-10",
            ticker, "STOCK", "USD", "test-token",
        )

    prices.loc[0, "close"] = 108.0
    prices.loc[0, "adjusted_close"] = 108.0

    tiingo_data.ingest_tiingo_prices(
        db_path, "NVDA",
        "2024-06-10", "2024-06-10",
        "NVDA", "STOCK", "USD", "test-token",
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT i.ticker, p.close
            FROM prices p
            JOIN instruments i ON i.id = p.instrument_id
            ORDER BY i.ticker
            """
        ).fetchall()

    assert rows == [
        ("AAPL", 105.0),
        ("NVDA", 108.0),
    ]




def test_tiingo_reingestion_removes_obsolete_split(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "obsolete_split.db"
    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src" / "riskengine" / "db" / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-07", "2024-06-10"]),
            "open": [1200.0, 120.0],
            "high": [1220.0, 125.0],
            "low": [1180.0, 115.0],
            "close": [1208.88, 121.79],
            "adjusted_close": [120.54, 121.44],
            "volume": [1000, 2000],
            "split_factor": [1.0, 10.0],
            "div_cash": [0.0, 0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    def ingest():
        return tiingo_data.ingest_tiingo_prices(
            db_path, "NVDA",
            "2024-06-07", "2024-06-10",
            "NVIDIA", "STOCK", "USD", "test-token",
        )

    assert ingest() == 2

    with sqlite3.connect(db_path) as connection:
        initial = connection.execute(
            "SELECT ratio FROM corporate_actions"
        ).fetchall()

    assert initial == [(10.0,)]

    # Tiingo corrects its historical record: no split occurred.
    prices.loc[1, "split_factor"] = 1.0

    assert ingest() == 2

    with sqlite3.connect(db_path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM corporate_actions"
        ).fetchone()[0]

        coverage = connection.execute(
            "SELECT COUNT(*) FROM corporate_action_coverage"
        ).fetchone()[0]

    assert remaining == 0
    assert coverage == 1


def test_tiingo_split_refresh_preserves_other_instruments(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "split_isolation.db"
    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src" / "riskengine" / "db" / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    prices = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-10"]),
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "adjusted_close": [105.0],
            "volume": [1000],
            "split_factor": [2.0],
            "div_cash": [0.0],
        }
    )

    monkeypatch.setattr(
        tiingo_data,
        "fetch_tiingo_prices",
        lambda *args: prices.copy(),
    )

    def ingest(ticker):
        return tiingo_data.ingest_tiingo_prices(
            db_path, ticker,
            "2024-06-10", "2024-06-10",
            ticker, "STOCK", "USD", "test-token",
        )

    ingest("AAPL")
    ingest("NVDA")

    # Remove NVDA's split without affecting AAPL's split.
    prices.loc[0, "split_factor"] = 1.0
    ingest("NVDA")

    with sqlite3.connect(db_path) as connection:
        splits = connection.execute(
            """
            SELECT i.ticker, ca.ratio
            FROM corporate_actions ca
            JOIN instruments i ON i.id = ca.instrument_id
            ORDER BY i.ticker
            """
        ).fetchall()

    assert splits == [("AAPL", 2.0)]




def test_tiingo_coverage_merges_overlapping_ranges(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "merge_coverage.db"
    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src" / "riskengine" / "db" / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    def ingest(start, end):
        dates = pd.to_datetime([start, end]).unique()
        n = len(dates)

        prices = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0] * n,
                "high": [110.0] * n,
                "low": [90.0] * n,
                "close": [105.0] * n,
                "adjusted_close": [105.0] * n,
                "volume": [1000] * n,
                "split_factor": [1.0] * n,
                "div_cash": [0.0] * n,
            }
        )

        monkeypatch.setattr(
            tiingo_data,
            "fetch_tiingo_prices",
            lambda *args: prices.copy(),
        )

        tiingo_data.ingest_tiingo_prices(
            db_path, "NVDA", start, end,
            "NVIDIA", "STOCK", "USD", "test-token",
        )

    ingest("2024-06-03", "2024-06-07")
    ingest("2024-06-06", "2024-06-11")

    with sqlite3.connect(db_path) as connection:
        coverage = connection.execute(
            """
            SELECT start_date, end_date
            FROM corporate_action_coverage
            """
        ).fetchall()

    assert coverage == [("2024-06-03", "2024-06-11")]


def test_tiingo_coverage_keeps_disjoint_ranges(
    tmp_path, monkeypatch
):
    from pathlib import Path
    from riskengine.ingestion import tiingo_data

    db_path = tmp_path / "disjoint_coverage.db"
    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src" / "riskengine" / "db" / "schema"
    )

    with sqlite3.connect(db_path) as connection:
        for schema_file in sorted(schema_dir.glob("*.sql")):
            connection.executescript(
                schema_file.read_text(encoding="utf-8")
            )

    def ingest(start, end):
        prices = pd.DataFrame(
            {
                "date": pd.to_datetime([start]),
                "open": [100.0],
                "high": [110.0],
                "low": [90.0],
                "close": [105.0],
                "adjusted_close": [105.0],
                "volume": [1000],
                "split_factor": [1.0],
                "div_cash": [0.0],
            }
        )

        monkeypatch.setattr(
            tiingo_data,
            "fetch_tiingo_prices",
            lambda *args: prices.copy(),
        )

        tiingo_data.ingest_tiingo_prices(
            db_path, "NVDA", start, end,
            "NVIDIA", "STOCK", "USD", "test-token",
        )

    ingest("2024-06-03", "2024-06-07")
    ingest("2024-06-17", "2024-06-21")

    with sqlite3.connect(db_path) as connection:
        coverage = connection.execute(
            """
            SELECT start_date, end_date
            FROM corporate_action_coverage
            ORDER BY start_date
            """
        ).fetchall()

    assert coverage == [
        ("2024-06-03", "2024-06-07"),
        ("2024-06-17", "2024-06-21"),
    ]