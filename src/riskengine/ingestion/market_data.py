from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from riskengine.db.connection import get_connection
from riskengine.db.instruments import get_or_create_instrument
from riskengine.db.prices import add_prices_from_dataframe


def validate_prices(data):
    required_columns = [
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

    missing_columns = [
        column for column in required_columns
        if column not in data.columns
    ]

    if missing_columns:
        raise ValueError(
            f"Missing required price columns: {missing_columns}"
        )

    if data[required_columns].isna().any().any():
        raise ValueError("Price data contains missing values")

    if (data["high"] <= 0).any():
        raise ValueError(
            "Price data contains non-positive high prices"
        )

    if data["date"].duplicated().any():
        raise ValueError("Price data contains duplicate dates")

    if (data["high"] < data["low"]).any():
        raise ValueError("Price data contains high values below low values")

    if (data["close"] < data["low"]).any():
        raise ValueError("Price data contains close values below low values")

    if (data["open"] < data["low"]).any():
        raise ValueError("Price data contains open values below low values")

    if (data["open"] > data["high"]).any():
        raise ValueError("Price data contains open values above high values")

    if (data["close"] > data["high"]).any():
        raise ValueError("Price data contains close values above high values")

    if (data["close"] <= 0).any():
        raise ValueError(
            "Price data contains non-positive close prices"
        )

    if (data["open"] <= 0).any():
        raise ValueError(
            "Price data contains non-positive open prices"
        )

    if (data["adjusted_close"] <= 0).any():
        raise ValueError(
            "Price data contains non-positive adjusted close values"
        )

    if (data["volume"] < 0).any():
        raise ValueError("Price data contains negative volume")

    return data


def fetch_prices(
    ticker,
    start_date,
    end_date,
):
    data = yf.download(
        ticker,
        start=start_date,
        end=end_date,
        auto_adjust=False,
        progress=False,
    )

    if data.empty:
        return pd.DataFrame(
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

    data = data.xs(ticker, level="Ticker", axis=1)

    data = data.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "adjusted_close",
            "Volume": "volume",
        }
    )

    data = data.reset_index()
    data = data.rename(columns={"Date": "date"})

    data["source"] = "yahoo_finance"
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()

    data = data[
        [
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
    ]

    return validate_prices(data)


def ingest_prices(
    db_path,
    ticker,
    start_date,
    end_date,
    name,
    asset_type,
    currency,
    sector=None,
):
    data = fetch_prices(
        ticker,
        start_date,
        end_date,
    )

    if data.empty:
        return 0

    validate_prices(data)

    with get_connection(db_path) as connection:
        with connection:
            instrument_id = get_or_create_instrument(
                connection,
                ticker,
                name,
                asset_type,
                currency,
                sector,
            )

            add_prices_from_dataframe(
                connection,
                instrument_id,
                data,
            )