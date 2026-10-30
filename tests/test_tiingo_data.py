
import json
from unittest.mock import patch

import pandas as pd
import pytest

from riskengine.ingestion.tiingo_data import (
    fetch_tiingo_prices,
    validate_tiingo_prices,
    extract_split_actions,
)

from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema
from riskengine.ingestion.tiingo_data import ingest_tiingo_prices
from riskengine.db.prices import validate_raw_price_sources

def test_fetch_tiingo_prices_preserves_raw_prices_and_split():
    sample_response = [
        {
            "date": "2024-06-07T00:00:00.000Z",
            "open": 1197.70,
            "high": 1216.92,
            "low": 1180.22,
            "close": 1208.88,
            "adjClose": 120.5447288816,
            "volume": 41238600,
            "splitFactor": 1.0,
            "divCash": 0.0,
        },
        {
            "date": "2024-06-10T00:00:00.000Z",
            "open": 120.37,
            "high": 123.10,
            "low": 117.01,
            "close": 121.79,
            "adjClose": 121.4441675807,
            "volume": 314162700,
            "splitFactor": 10.0,
            "divCash": 0.0,
        },
    ]

    class MockResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    with (
        patch(
            "riskengine.ingestion.tiingo_data.urllib.request.urlopen",
            return_value=MockResponse(),
        ),
        patch(
            "riskengine.ingestion.tiingo_data.json.load",
            return_value=sample_response,
        ),
    ):
        data = fetch_tiingo_prices(
            "NVDA",
            "2024-06-07",
            "2024-06-10",
            "dummy-test-token",
        )

    
    assert len(data) == 2

    before = data.iloc[0]
    after = data.iloc[1]

    assert before["date"].strftime("%Y-%m-%d") == "2024-06-07"
    assert before["close"] == pytest.approx(1208.88)
    assert before["adjusted_close"] == pytest.approx(
        120.5447288816
    )

    assert after["date"].strftime("%Y-%m-%d") == "2024-06-10"
    assert after["close"] == pytest.approx(121.79)
    assert after["split_factor"] == pytest.approx(10.0)
    assert after["div_cash"] == pytest.approx(0.0)





@pytest.mark.parametrize(
    "column, invalid_value",
    [
        ("close", -100.0),
        ("adjusted_close", 0.0),
        ("split_factor", 0.0),
        ("split_factor", -2.0),
        ("split_factor", float("nan")),
        ("split_factor", float("inf")),
        ("volume", -100),
        ("div_cash", -0.01),
        ("high", 0.0),
    ],
)
def test_validate_tiingo_prices_rejects_invalid_data(
    column, invalid_value
):
    data = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-06-10"),
                "open": 120.37,
                "high": 123.10,
                "low": 117.01,
                "close": 121.79,
                "adjusted_close": 121.44,
                "volume": 314162700,
                "split_factor": 10.0,
                "div_cash": 0.0,
            }
        ]
    )

    data.loc[0, column] = invalid_value

    with pytest.raises(ValueError):
        validate_tiingo_prices(data)






def test_validate_tiingo_prices_accepts_valid_data():
    data = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-10"),
                "open": 100.0,
                "high": 105.0,
                "low": 98.0,
                "close": 102.0,
                "adjusted_close": 51.0,
                "volume": 100000,
                "split_factor": 2.0,
                "div_cash": 0.0,
            },
            {
                "date": pd.Timestamp("2024-01-11"),
                "open": 200.0,
                "high": 210.0,
                "low": 195.0,
                "close": 205.0,
                "adjusted_close": 205.0,
                "volume": 50000,
                "split_factor": 0.5,
                "div_cash": 0.25,
            },
        ]
    )

    result = validate_tiingo_prices(data)

    pd.testing.assert_frame_equal(result, data)

    assert result.iloc[0]["split_factor"] == pytest.approx(2.0)
    assert result.iloc[1]["split_factor"] == pytest.approx(0.5)
    assert result.iloc[0]["div_cash"] == pytest.approx(0.0)
    assert result.iloc[1]["div_cash"] == pytest.approx(0.25)





@pytest.mark.parametrize(
    "invalid_date",
    [
        "not-a-date",
        "2024-02-30",
        12345,
    ],
)
def test_validate_tiingo_prices_rejects_invalid_dates(invalid_date):
    data = pd.DataFrame(
        [
            {
                "date": invalid_date,
                "open": 100.0,
                "high": 105.0,
                "low": 98.0,
                "close": 102.0,
                "adjusted_close": 101.0,
                "volume": 100000,
                "split_factor": 1.0,
                "div_cash": 0.0,
            }
        ]
    )

    with pytest.raises(ValueError):
        validate_tiingo_prices(data)




def test_extract_split_actions():
    data = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-06-07"),
                "split_factor": 1.0,
            },
            {
                "date": pd.Timestamp("2024-06-10"),
                "split_factor": 10.0,
            },
            {
                "date": pd.Timestamp("2024-06-11"),
                "split_factor": 1.0,
            },
            {
                "date": pd.Timestamp("2024-07-01"),
                "split_factor": 0.5,
            },
        ]
    )

    splits = extract_split_actions(data)

    assert len(splits) == 2

    assert splits.iloc[0]["ex_date"] == "2024-06-10"
    assert splits.iloc[0]["ratio"] == pytest.approx(10.0)

    assert splits.iloc[1]["ex_date"] == "2024-07-01"
    assert splits.iloc[1]["ratio"] == pytest.approx(0.5)





def test_ingest_tiingo_prices_stores_raw_prices_and_splits(
    tmp_path,
):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    sample_data = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-06-07"),
                "open": 1197.70,
                "high": 1216.92,
                "low": 1180.22,
                "close": 1208.88,
                "adjusted_close": 120.5447288816,
                "volume": 41238600,
                "split_factor": 1.0,
                "div_cash": 0.0,
            },
            {
                "date": pd.Timestamp("2024-06-10"),
                "open": 120.37,
                "high": 123.10,
                "low": 117.01,
                "close": 121.79,
                "adjusted_close": 121.4441675807,
                "volume": 314162700,
                "split_factor": 10.0,
                "div_cash": 0.0,
            },
        ]
    )

    with patch(
        "riskengine.ingestion.tiingo_data.fetch_tiingo_prices",
        return_value=sample_data,
    ):
        count = ingest_tiingo_prices(
            db_path=db_path,
            ticker="NVDA",
            start_date="2024-06-07",
            end_date="2024-06-10",
            name="NVIDIA",
            asset_type="STOCK",
            currency="USD",
            token="dummy-test-token",
        )

    assert count == 2

    with get_connection(db_path) as connection:
        prices = connection.execute(
            """
            SELECT
                p.date,
                p.close,
                p.adjusted_close,
                p.source
            FROM prices AS p
            JOIN instruments AS i
                ON p.instrument_id = i.id
            WHERE i.ticker = 'NVDA'
            ORDER BY p.date
            """
        ).fetchall()

        splits = connection.execute(
            """
            SELECT
                ca.ex_date,
                ca.ratio,
                ca.source
            FROM corporate_actions AS ca
            JOIN instruments AS i
                ON ca.instrument_id = i.id
            WHERE i.ticker = 'NVDA'
            ORDER BY ca.ex_date
            """
        ).fetchall()


        with get_connection(db_path) as connection:
            coverage = connection.execute(
            """
            SELECT c.start_date, c.end_date, c.source
            FROM corporate_action_coverage AS c
            JOIN instruments AS i
                ON c.instrument_id = i.id
            WHERE i.ticker = 'NVDA'
            """
        ).fetchall()

    assert coverage == [
        ("2024-06-07", "2024-06-10", "tiingo")
    ]

    assert len(prices) == 2

    assert prices[0][0] == "2024-06-07"
    assert prices[0][1] == pytest.approx(1208.88)
    assert prices[0][2] == pytest.approx(120.5447288816)
    assert prices[0][3] == "tiingo"

    assert prices[1][0] == "2024-06-10"
    assert prices[1][1] == pytest.approx(121.79)
    assert prices[1][3] == "tiingo"

    assert len(splits) == 1
    assert splits[0][0] == "2024-06-10"
    assert splits[0][1] == pytest.approx(10.0)
    assert splits[0][2] == "tiingo"





def test_tiingo_ingestion_rolls_back_if_split_insert_fails(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    sample_data = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-06-10"),
                "open": 120.37,
                "high": 123.10,
                "low": 117.01,
                "close": 121.79,
                "adjusted_close": 121.44,
                "volume": 314162700,
                "split_factor": 10.0,
                "div_cash": 0.0,
            }
        ]
    )

    # Force the corporate-action insertion to fail.
    with get_connection(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_tiingo_split
            BEFORE INSERT ON corporate_actions
            BEGIN
                SELECT RAISE(ABORT, 'Simulated split insertion failure');
            END
            """
        )
        connection.commit()

    with patch(
        "riskengine.ingestion.tiingo_data.fetch_tiingo_prices",
        return_value=sample_data,
    ):
        with pytest.raises(
            Exception, match="Simulated split insertion failure"
        ):
            ingest_tiingo_prices(
                db_path=db_path,
                ticker="NVDA",
                start_date="2024-06-10",
                end_date="2024-06-10",
                name="NVIDIA",
                asset_type="STOCK",
                currency="USD",
                token="dummy-test-token",
            )

    with get_connection(db_path) as connection:
        price_count = connection.execute(
            "SELECT COUNT(*) FROM prices"
        ).fetchone()[0]

        split_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_actions"
        ).fetchone()[0]

    assert price_count == 0
    assert split_count == 0

    with get_connection(db_path) as connection:
        coverage_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_action_coverage"
        ).fetchone()[0]

    assert coverage_count == 0






def test_tiingo_prices_are_not_overwritten_by_yahoo(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        instrument_id = connection.execute(
            """
            INSERT INTO instruments (
                ticker, name, asset_type, currency
            ) VALUES (?, ?, ?, ?)
            """,
            ("NVDA", "NVIDIA", "STOCK", "USD"),
        ).lastrowid

        connection.execute(
            """
            INSERT INTO prices (
                instrument_id, date, open, high, low,
                close, adjusted_close, volume, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instrument_id,
                "2024-06-07",
                1197.70,
                1216.92,
                1180.22,
                1208.88,
                120.5447288816,
                41238600,
                "tiingo",
                "2024-06-08T00:00:00+00:00",
            ),
        )
        connection.commit()

    from riskengine.db.prices import add_price

    with get_connection(db_path) as connection:
     with pytest.raises(ValueError, match="Price source mismatch"):
        add_price(
            connection=connection,
            instrument_id=instrument_id,
            date="2024-06-07",
            open_price=119.77,
            high=121.69,
            low=118.02,
            close=120.89,
            adjusted_close=120.54,
            volume=412386000,
            source="yahoo_finance",
            fetched_at="2024-06-09T00:00:00+00:00",
        )
        connection.commit()

    with get_connection(db_path) as connection:
        stored = connection.execute(
            """
            SELECT close, source
            FROM prices
            WHERE instrument_id = ?
              AND date = ?
            """,
            (instrument_id, "2024-06-07"),
        ).fetchone()

    assert stored[0] == pytest.approx(1208.88)
    assert stored[1] == "tiingo"





def test_detect_mixed_price_sources(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        instrument_id = connection.execute(
            """
            INSERT INTO instruments (
                ticker, name, asset_type, currency
            )
            VALUES (?, ?, ?, ?)
            """,
            ("NVDA", "NVIDIA", "STOCK", "USD"),
        ).lastrowid

        connection.executemany(
            """
            INSERT INTO prices (
                instrument_id, date, open, high, low,
                close, adjusted_close, volume,
                source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    instrument_id, "2023-06-07",
                    38.0, 39.0, 37.0, 38.5, 38.2,
                    1000000, "yahoo_finance",
                    "2024-01-01T00:00:00+00:00",
                ),
                (
                    instrument_id, "2024-06-07",
                    1197.70, 1216.92, 1180.22,
                    1208.88, 120.5447288816,
                    41238600, "tiingo",
                    "2024-06-08T00:00:00+00:00",
                ),
            ],
        )
        connection.commit()

        sources = connection.execute(
            """
            SELECT DISTINCT source
            FROM prices
            WHERE instrument_id = ?
            """,
            (instrument_id,),
        ).fetchall()

    source_names = {row[0] for row in sources}

    assert source_names == {"yahoo_finance", "tiingo"}
    assert len(source_names) > 1




def test_validate_raw_price_sources_rejects_mixed_history(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        instrument_id = connection.execute(
            """
            INSERT INTO instruments (
                ticker, name, asset_type, currency
            )
            VALUES (?, ?, ?, ?)
            """,
            ("NVDA", "NVIDIA", "STOCK", "USD"),
        ).lastrowid

        connection.executemany(
            """
            INSERT INTO prices (
                instrument_id, date, open, high, low,
                close, adjusted_close, volume,
                source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    instrument_id, "2023-06-07",
                    38.0, 39.0, 37.0, 38.5, 38.2,
                    1000000, "yahoo_finance",
                    "2024-01-01T00:00:00+00:00",
                ),
                (
                    instrument_id, "2024-06-07",
                    1197.70, 1216.92, 1180.22,
                    1208.88, 120.5447288816,
                    41238600, "tiingo",
                    "2024-06-08T00:00:00+00:00",
                ),
            ],
        )
        connection.commit()

        with pytest.raises(
            ValueError,
            match="unsuitable for raw-price valuation",
        ):
            validate_raw_price_sources(
                connection, instrument_id
            )




def test_validate_raw_price_sources_accepts_tiingo_history(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    with get_connection(db_path) as connection:
        instrument_id = connection.execute(
            """
            INSERT INTO instruments (
                ticker, name, asset_type, currency
            )
            VALUES (?, ?, ?, ?)
            """,
            ("NVDA", "NVIDIA", "STOCK", "USD"),
        ).lastrowid

        connection.executemany(
            """
            INSERT INTO prices (
                instrument_id, date, open, high, low,
                close, adjusted_close, volume,
                source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    instrument_id, "2024-06-07",
                    1197.70, 1216.92, 1180.22,
                    1208.88, 120.5447288816,
                    41238600, "tiingo",
                    "2024-06-08T00:00:00+00:00",
                ),
                (
                    instrument_id, "2024-06-10",
                    120.37, 123.10, 117.01,
                    121.79, 121.4441675807,
                    314162700, "tiingo",
                    "2024-06-11T00:00:00+00:00",
                ),
            ],
        )
        connection.commit()

        assert validate_raw_price_sources(
            connection, instrument_id
        ) is True




def test_tiingo_ingestion_rolls_back_if_coverage_insert_fails(tmp_path):
    db_path = tmp_path / "test.db"
    create_schema(db_path)

    sample_data = pd.DataFrame([
        {
            "date": pd.Timestamp("2024-06-10"),
            "open": 120.37,
            "high": 123.10,
            "low": 117.01,
            "close": 121.79,
            "adjusted_close": 121.44,
            "volume": 314162700,
            "split_factor": 10.0,
            "div_cash": 0.0,
        }
    ])

    with get_connection(db_path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_coverage
            BEFORE INSERT ON corporate_action_coverage
            BEGIN
                SELECT RAISE(ABORT, 'Simulated coverage failure');
            END
        """)
        connection.commit()

    with patch(
        "riskengine.ingestion.tiingo_data.fetch_tiingo_prices",
        return_value=sample_data,
    ):
        with pytest.raises(
            Exception, match="Simulated coverage failure"
        ):
            ingest_tiingo_prices(
                db_path=db_path,
                ticker="NVDA",
                start_date="2024-06-10",
                end_date="2024-06-10",
                name="NVIDIA",
                asset_type="STOCK",
                currency="USD",
                token="dummy-test-token",
            )

    with get_connection(db_path) as connection:
        price_count = connection.execute(
            "SELECT COUNT(*) FROM prices"
        ).fetchone()[0]

        split_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_actions"
        ).fetchone()[0]

        coverage_count = connection.execute(
            "SELECT COUNT(*) FROM corporate_action_coverage"
        ).fetchone()[0]

    assert price_count == 0
    assert split_count == 0
    assert coverage_count == 0