import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from riskengine.db.prices import (
    add_price,
    add_prices_from_dataframe,
)


@pytest.fixture
def database(tmp_path):
    db_path = tmp_path / "source_policy.db"

    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "riskengine"
        / "db"
        / "schema"
    )

    connection = sqlite3.connect(db_path)

    for schema_file in sorted(schema_dir.glob("*.sql")):
        connection.executescript(
            schema_file.read_text(encoding="utf-8")
        )

    connection.execute(
        """
        INSERT INTO instruments (
            ticker, name, asset_type, currency, primary_source
        )
        VALUES ('NVDA', 'NVIDIA', 'STOCK', 'USD', 'tiingo')
        """
    )

    instrument_id = connection.execute(
        "SELECT id FROM instruments WHERE ticker = 'NVDA'"
    ).fetchone()[0]

    yield connection, instrument_id

    connection.close()


def insert_price(connection, instrument_id, source, close=100.0):
    return add_price(
        connection=connection,
        instrument_id=instrument_id,
        date="2024-06-10",
        open_price=100.0,
        high=110.0,
        low=90.0,
        close=close,
        adjusted_close=close,
        volume=1000,
        source=source,
        fetched_at="2024-06-11T00:00:00+00:00",
    )


def test_primary_tiingo_price_is_accepted(database):
    connection, instrument_id = database

    insert_price(connection, instrument_id, "tiingo")

    row = connection.execute(
        "SELECT close, source FROM prices"
    ).fetchone()

    assert row == (100.0, "tiingo")


def test_secondary_source_cannot_overwrite_primary(database):
    connection, instrument_id = database

    insert_price(connection, instrument_id, "tiingo")

    with pytest.raises(ValueError, match="Price source mismatch"):
        insert_price(
            connection,
            instrument_id,
            "yahoo_finance",
            close=999.0,
        )

    row = connection.execute(
        "SELECT close, source FROM prices"
    ).fetchone()

    assert row == (100.0, "tiingo")


def test_bulk_ingestion_rejects_secondary_source(database):
    connection, instrument_id = database

    data = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-06-10", "2024-06-11"]
            ),
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "adjusted_close": [105.0, 106.0],
            "volume": [1000, 1100],
            "source": ["yahoo_finance", "tiingo"],
            "fetched_at": [
                "2024-06-12T00:00:00+00:00"
            ] * 2,
        }
    )

    with pytest.raises(ValueError, match="Price source mismatch"):
        add_prices_from_dataframe(
            connection,
            instrument_id,
            data,
        )

    count = connection.execute(
        "SELECT COUNT(*) FROM prices"
    ).fetchone()[0]

    assert count == 0


def test_existing_secondary_price_cannot_be_replaced(database):
    connection, instrument_id = database

    connection.execute(
        """
        INSERT INTO prices (
            instrument_id, date, open, high, low,
            close, adjusted_close, volume, source, fetched_at
        )
        VALUES (?, '2024-06-10', 100, 110, 90,
                105, 105, 1000, 'yahoo_finance',
                '2024-06-11T00:00:00+00:00')
        """,
        (instrument_id,),
    )

    with pytest.raises(
        ValueError,
        match="Existing price source conflict",
    ):
        insert_price(connection, instrument_id, "tiingo")

    row = connection.execute(
        "SELECT close, source FROM prices"
    ).fetchone()

    assert row == (105.0, "yahoo_finance")