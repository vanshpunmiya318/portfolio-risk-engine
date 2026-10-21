
import json
import urllib.parse
import urllib.request

import pandas as pd
import truststore

from datetime import datetime, timezone

from riskengine.db.connection import get_connection
from riskengine.db.instruments import get_or_create_instrument
from riskengine.db.prices import add_prices_from_dataframe


def fetch_tiingo_prices(ticker, start_date, end_date, token):
    if not token or not token.strip():
        raise ValueError("Tiingo API token is required")

    if end_date < start_date:
        raise ValueError("End date must not precede start date")

    params = urllib.parse.urlencode(
        {
            "startDate": start_date,
            "endDate": end_date,
        }
    )

    ticker_encoded = urllib.parse.quote(ticker, safe="")
    url = (
        f"https://api.tiingo.com/tiingo/daily/"
        f"{ticker_encoded}/prices?{params}"
    )

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {token.strip()}",
            "Accept": "application/json",
        },
    )

    truststore.inject_into_ssl()

    with urllib.request.urlopen(request, timeout=20) as response:
        records = json.load(response)

    if not isinstance(records, list):
        raise ValueError("Unexpected Tiingo response format")

    columns = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
        "volume",
        "split_factor",
        "div_cash",
    ]

    rows = []

    for record in records:
        rows.append(
            {
                "date": pd.Timestamp(record["date"]).tz_localize(
                    None
                ),
                "open": record["open"],
                "high": record["high"],
                "low": record["low"],
                "close": record["close"],
                "adjusted_close": record["adjClose"],
                "volume": record["volume"],
                "split_factor": record["splitFactor"],
                "div_cash": record["divCash"],
            }
        )

    return pd.DataFrame(rows, columns=columns)





def validate_tiingo_prices(data):
    required_columns = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
        "volume",
        "split_factor",
        "div_cash",
    ]

    missing = [
        column for column in required_columns
        if column not in data.columns
    ]

    if missing:
        raise ValueError(
            f"Missing Tiingo price columns: {missing}"
        )

    if data.empty:
        return data

    if data[required_columns].isna().any().any():
        raise ValueError("Tiingo price data contains missing values")

    
    for value in data["date"]:
        if not isinstance(value, pd.Timestamp):
            raise ValueError(
                "Tiingo dates must be valid pandas Timestamps"
            )

        if pd.isna(value):
            raise ValueError(
                "Tiingo price data contains invalid dates"
            )

        if value.tzinfo is not None:
            raise ValueError(
                "Tiingo dates must be timezone-naive"
            )

        if value != value.normalize():
            raise ValueError(
                "Tiingo dates must not contain a time component"
            )

    if data["date"].duplicated().any():
        raise ValueError("Tiingo price data contains duplicate dates")

    if not data["date"].is_monotonic_increasing:
        raise ValueError("Tiingo price dates must be chronological")

    price_columns = [
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
    ]

    for column in price_columns:
        if not pd.api.types.is_numeric_dtype(data[column]):
            raise ValueError(f"Tiingo {column} must be numeric")

        if not data[column].map(
            lambda value: bool(pd.notna(value)) and
            float("-inf") < value < float("inf")
        ).all():
            raise ValueError(f"Tiingo {column} contains non-finite values")

        if (data[column] <= 0).any():
            raise ValueError(f"Tiingo {column} must be positive")

    if (data["high"] < data["low"]).any():
        raise ValueError("Tiingo high price is below low price")

    if (
        (data["open"] < data["low"])
        | (data["open"] > data["high"])
        | (data["close"] < data["low"])
        | (data["close"] > data["high"])
    ).any():
        raise ValueError("Tiingo OHLC prices are inconsistent")

    for column in ["volume", "split_factor", "div_cash"]:
        if not pd.api.types.is_numeric_dtype(data[column]):
            raise ValueError(f"Tiingo {column} must be numeric")

        if not data[column].map(
            lambda value: float("-inf") < value < float("inf")
        ).all():
            raise ValueError(f"Tiingo {column} contains non-finite values")

    if (data["volume"] < 0).any():
        raise ValueError("Tiingo volume cannot be negative")

    if (data["split_factor"] <= 0).any():
        raise ValueError("Tiingo split factor must be positive")

    if (data["div_cash"] < 0).any():
        raise ValueError("Tiingo cash dividend cannot be negative")

    return data




def extract_split_actions(data):
    required_columns = ["date", "split_factor"]

    missing = [
        column for column in required_columns
        if column not in data.columns
    ]

    if missing:
        raise ValueError(
            f"Missing split data columns: {missing}"
        )

    if data.empty:
        return pd.DataFrame(
            columns=["ex_date", "ratio"]
        )

    splits = data.loc[
        data["split_factor"] != 1.0,
        ["date", "split_factor"],
    ].copy()

    splits = splits.rename(
        columns={
            "date": "ex_date",
            "split_factor": "ratio",
        }
    )

    splits["ex_date"] = splits["ex_date"].dt.strftime(
        "%Y-%m-%d"
    )

    return splits.reset_index(drop=True)




def ingest_tiingo_prices(
    db_path,
    ticker,
    start_date,
    end_date,
    name,
    asset_type,
    currency,
    token,
    sector=None,
):
    data = fetch_tiingo_prices(
        ticker, start_date, end_date, token
    )

    validate_tiingo_prices(data)

    if data.empty:
        return 0

    requested_start = pd.Timestamp(start_date)
    requested_end = pd.Timestamp(end_date)

    if (
        data["date"].min() < requested_start
        or data["date"].max() > requested_end
    ):
        raise ValueError(
            "Tiingo returned prices outside the requested date range"
        )

    fetched_at = datetime.now(timezone.utc).isoformat()

    price_data = data[
        [
            "date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
        ]
    ].copy()

    price_data["source"] = "tiingo"
    price_data["fetched_at"] = fetched_at

    splits = extract_split_actions(data)

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
                price_data,
            )


                        # Remove obsolete Tiingo splits within the refreshed date range.
            # A split absent from the new response must not survive.
            connection.execute(
                """
                DELETE FROM corporate_actions
                WHERE instrument_id = ?
                  AND action_type = 'SPLIT'
                  AND source = 'tiingo'
                  AND ex_date BETWEEN ? AND ?
                """,
                (instrument_id, start_date, end_date),
            )


            for row in splits.itertuples(index=False):
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
                    VALUES (?, ?, 'SPLIT', ?, ?, ?)
                    ON CONFLICT (
                        instrument_id, ex_date, action_type
                    )
                    DO UPDATE SET
                        ratio = excluded.ratio,
                        source = excluded.source,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        instrument_id,
                        row.ex_date,
                        row.ratio,
                        "tiingo",
                        fetched_at,
                    ),
                )
                        # Consolidate overlapping or adjacent Tiingo coverage intervals.
            # Preserve coverage outside the current ingestion range.
            coverage_start = start_date
            coverage_end = end_date

            existing_coverage = connection.execute(
                """
                SELECT id, start_date, end_date
                FROM corporate_action_coverage
                WHERE instrument_id = ?
                  AND source = 'tiingo'
                """,
                (instrument_id,),
            ).fetchall()

            from datetime import date, timedelta

            merged_start = date.fromisoformat(coverage_start)
            merged_end = date.fromisoformat(coverage_end)
            ids_to_delete = set()

            changed = True
            while changed:
                changed = False

                for coverage_id, old_start, old_end in existing_coverage:
                    if coverage_id in ids_to_delete:
                        continue

                    old_start = date.fromisoformat(old_start)
                    old_end = date.fromisoformat(old_end)

                    if (
                        old_start <= merged_end + timedelta(days=1)
                        and old_end >= merged_start - timedelta(days=1)
                    ):
                        ids_to_delete.add(coverage_id)
                        merged_start = min(merged_start, old_start)
                        merged_end = max(merged_end, old_end)
                        changed = True

            for coverage_id in ids_to_delete:
                connection.execute(
                    """
                    DELETE FROM corporate_action_coverage
                    WHERE id = ?
                    """,
                    (coverage_id,),
                )

            coverage_start = merged_start.isoformat()
            coverage_end = merged_end.isoformat()
            

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
                ON CONFLICT (
                    instrument_id,
                    start_date,
                    end_date,
                    source
                )
                DO UPDATE SET
                    fetched_at = excluded.fetched_at
                """,
                (
                    instrument_id,
                    coverage_start,
                    coverage_end,
                    "tiingo",
                    fetched_at,
                ),
            )

    return len(price_data)