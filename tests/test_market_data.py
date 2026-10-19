import pandas as pd
import pytest

from riskengine.ingestion.market_data import fetch_prices, validate_prices


@pytest.mark.network
def test_fetch_prices_returns_clean_dataframe():
    result = fetch_prices(
        "AAPL",
        "2026-01-02",
        "2026-01-10",
    )

    assert isinstance(result, pd.DataFrame)
    assert not result.empty

    assert list(result.columns) == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
        "volume",
        "source",
        "fetched_at",
    ]

    assert result["source"].eq("yahoo_finance").all()




@pytest.mark.network
def test_fetch_prices_returns_empty_dataframe_when_no_data():
    result = fetch_prices(
        "THIS_TICKER_DOES_NOT_EXIST_12345",
        "2026-01-02",
        "2026-01-10",
    )

    assert isinstance(result, pd.DataFrame)
    assert result.empty
    assert list(result.columns) == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
        "volume",
        "source",
        "fetched_at",
    ]




def test_validate_prices_rejects_high_below_low():
    from riskengine.ingestion.market_data import validate_prices

    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [98],
            "low": [99],
            "close": [99.5],
            "adjusted_close": [99.5],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(ValueError, match="high values below low"):
        validate_prices(data)




def test_validate_prices_rejects_close_above_high():
    from riskengine.ingestion.market_data import validate_prices

    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [105],
            "low": [99],
            "close": [106],
            "adjusted_close": [106],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(ValueError, match="close values above high"):
        validate_prices(data)





def test_validate_prices_rejects_close_below_low():
    from riskengine.ingestion.market_data import validate_prices

    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [105],
            "low": [99],
            "close": [98],
            "adjusted_close": [98],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(ValueError, match="close values below low"):
        validate_prices(data)





def test_validate_prices_rejects_missing_required_column():
    from riskengine.ingestion.market_data import validate_prices

    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [105],
            "low": [99],
            "close": [102],
            "adjusted_close": [102],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    data = data.drop(columns=["volume"])

    with pytest.raises(ValueError, match="Missing required price columns"):
        validate_prices(data)




def test_validate_prices_rejects_missing_values():
    from riskengine.ingestion.market_data import validate_prices

    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [105],
            "low": [99],
            "close": [102],
            "adjusted_close": [102],
            "volume": [None],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(ValueError, match="missing values"):
        validate_prices(data)





def test_validate_prices_rejects_negative_volume():
    from riskengine.ingestion.market_data import validate_prices

    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [105],
            "low": [99],
            "close": [102],
            "adjusted_close": [102],
            "volume": [-1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(ValueError, match="negative volume"):
        validate_prices(data)





def test_validate_prices_rejects_non_positive_adjusted_close():
    from riskengine.ingestion.market_data import validate_prices

    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [105],
            "low": [99],
            "close": [102],
            "adjusted_close": [0],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(
        ValueError,
        match="non-positive adjusted close",
    ):
        validate_prices(data)




def test_validate_prices_rejects_duplicate_dates():
    data = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-02", "2026-01-02"]
            ),
            "open": [100.0, 101.0],
            "high": [105.0, 106.0],
            "low": [99.0, 100.0],
            "close": [103.0, 104.0],
            "adjusted_close": [103.0, 104.0],
            "volume": [1000, 1100],
            "source": ["test", "test"],
            "fetched_at": [
                "2026-01-03T00:00:00+00:00",
                "2026-01-03T00:00:00+00:00",
            ],
        }
    )

    with pytest.raises(
        ValueError,
        match="duplicate dates",
    ):
        validate_prices(data)





def test_ingest_prices_stores_data(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"

    data = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-02", "2026-01-05"]
            ),
            "open": [100.0, 102.0],
            "high": [105.0, 106.0],
            "low": [99.0, 101.0],
            "close": [103.0, 104.0],
            "adjusted_close": [103.0, 104.0],
            "volume": [1000, 1100],
            "source": ["test", "test"],
            "fetched_at": [
                "2026-01-06T00:00:00+00:00",
                "2026-01-06T00:00:00+00:00",
            ],
        }
    )

    monkeypatch.setattr(
        "riskengine.ingestion.market_data.fetch_prices",
        lambda ticker, start_date, end_date: data,
    )

    from riskengine.db.connection import get_connection
    from riskengine.db.schema.loader import create_schema
    from riskengine.db.prices import get_prices
    from riskengine.ingestion.market_data import ingest_prices

    create_schema(db_path)

    ingest_prices(
        db_path=db_path,
        ticker="AAPL",
        start_date="2026-01-01",
        end_date="2026-01-06",
        name="Apple Inc.",
        asset_type="EQUITY",
        currency="USD",
        sector="Technology",
    )

    with get_connection(db_path) as connection:
        prices = get_prices(
            connection,
            "AAPL",
        )

    assert len(prices) == 2
    assert prices.iloc[0]["close"] == 103.0
    assert prices.iloc[1]["close"] == 104.0




def test_ingest_prices_rolls_back_on_database_error(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"

    data = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-02", "2026-01-05"]
            ),
            "open": [100.0, 102.0],
            "high": [105.0, 106.0],
            "low": [99.0, 101.0],
            "close": [103.0, 104.0],
            "adjusted_close": [103.0, 104.0],
            "volume": [1000, 1100],
            "source": ["test", "test"],
            "fetched_at": [
                "2026-01-06T00:00:00+00:00",
                "2026-01-06T00:00:00+00:00",
            ],
        }
    )

    monkeypatch.setattr(
        "riskengine.ingestion.market_data.fetch_prices",
        lambda ticker, start_date, end_date: data,
    )

    def fail_after_first_price(connection, instrument_id, data):
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
                instrument_id,
                "2026-01-02",
                100.0,
                105.0,
                99.0,
                103.0,
                103.0,
                1000,
                "test",
                "2026-01-06T00:00:00+00:00",
            ),
        )

        raise RuntimeError("database insert failed")

    monkeypatch.setattr(
        "riskengine.ingestion.market_data.add_prices_from_dataframe",
        fail_after_first_price,
    )

    from riskengine.db.connection import get_connection
    from riskengine.db.schema.loader import create_schema
    from riskengine.ingestion.market_data import ingest_prices

    create_schema(db_path)

    with pytest.raises(
        RuntimeError,
        match="database insert failed",
    ):
        ingest_prices(
            db_path=db_path,
            ticker="AAPL",
            start_date="2026-01-01",
            end_date="2026-01-06",
            name="Apple Inc.",
            asset_type="EQUITY",
            currency="USD",
            sector="Technology",
        )

    with get_connection(db_path) as connection:
        instrument_count = connection.execute(
            "SELECT COUNT(*) FROM instruments"
        ).fetchone()[0]

        price_count = connection.execute(
            "SELECT COUNT(*) FROM prices"
        ).fetchone()[0]

    assert instrument_count == 0
    assert price_count == 0




def test_ingest_prices_returns_zero_when_no_data(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"

    empty_data = pd.DataFrame(
        columns=[
            "date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
            "source",
            "fetched_at",
        ]
    )

    monkeypatch.setattr(
        "riskengine.ingestion.market_data.fetch_prices",
        lambda ticker, start_date, end_date: empty_data,
    )

    from riskengine.db.connection import get_connection
    from riskengine.db.schema.loader import create_schema
    from riskengine.ingestion.market_data import ingest_prices

    create_schema(db_path)

    result = ingest_prices(
        db_path=db_path,
        ticker="AAPL",
        start_date="2026-01-01",
        end_date="2026-01-06",
        name="Apple Inc.",
        asset_type="EQUITY",
        currency="USD",
        sector="Technology",
    )

    assert result == 0

    with get_connection(db_path) as connection:
        instrument_count = connection.execute(
            "SELECT COUNT(*) FROM instruments"
        ).fetchone()[0]

    assert instrument_count == 0



def test_validate_prices_rejects_non_positive_open():
    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [0],
            "high": [0],
            "low": [0],
            "close": [0],
            "adjusted_close": [1],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(
        ValueError,
        match="non-positive open prices",
    ):
        validate_prices(data)





def test_validate_prices_rejects_open_below_low():
    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [98],
            "high": [105],
            "low": [99],
            "close": [102],
            "adjusted_close": [102],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(
        ValueError,
        match="open values below low",
    ):
        validate_prices(data)


def test_validate_prices_rejects_open_above_high():
    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [106],
            "high": [105],
            "low": [99],
            "close": [102],
            "adjusted_close": [102],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(
        ValueError,
        match="open values above high",
    ):
        validate_prices(data)




def test_validate_prices_rejects_non_positive_open():
    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [0],
            "high": [105],
            "low": [0],
            "close": [100],
            "adjusted_close": [1],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(
        ValueError,
        match="non-positive open prices",
    ):
        validate_prices(data)




def test_validate_prices_rejects_non_positive_high():
    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [1],
            "high": [0],
            "low": [0],
            "close": [1],
            "adjusted_close": [1],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(
        ValueError,
        match="non-positive high prices",
    ):
        validate_prices(data)




def test_validate_prices_rejects_non_positive_close():
    data = pd.DataFrame(
        {
            "date": ["2026-01-02"],
            "open": [100],
            "high": [105],
            "low": [0],
            "close": [0],
            "adjusted_close": [1],
            "volume": [1000],
            "source": ["test"],
            "fetched_at": ["2026-01-02T12:00:00+00:00"],
        }
    )

    with pytest.raises(
        ValueError,
        match="non-positive close prices",
    ):
        validate_prices(data)