import pandas as pd

from riskengine.db.instruments import normalise_ticker


def add_price(
    connection,
    instrument_id,
    date,
    open_price,
    high,
    low,
    close,
    adjusted_close,
    volume,
    source,
    fetched_at,
):
    cursor = connection.execute(
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
        ON CONFLICT (instrument_id, date)
        DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            adjusted_close = excluded.adjusted_close,
            volume = excluded.volume,
            source = excluded.source,
            fetched_at = excluded.fetched_at
        """,
        (
            instrument_id,
            date,
            open_price,
            high,
            low,
            close,
            adjusted_close,
            volume,
            source,
            fetched_at,
        ),
    )

    return connection.execute(
        """
        SELECT id
        FROM prices
        WHERE instrument_id = ? AND date = ?
        """,
        (instrument_id, date),
    ).fetchone()[0]


def get_prices(
    connection,
    ticker,
    start_date=None,
    end_date=None,
):
    query = """
        SELECT
            p.date,
            p.open,
            p.high,
            p.low,
            p.close,
            p.adjusted_close,
            p.volume,
            p.source,
            p.fetched_at
        FROM prices AS p
        JOIN instruments AS i
            ON p.instrument_id = i.id
        WHERE i.ticker = ?
    """

    parameters = [normalise_ticker(ticker)]

    if start_date is not None:
        query += " AND p.date >= ?"
        parameters.append(start_date)

    if end_date is not None:
        query += " AND p.date <= ?"
        parameters.append(end_date)

    query += " ORDER BY p.date"

    return pd.read_sql_query(
        query,
        connection,
        params=parameters,
        parse_dates=["date"],
    ).set_index("date")


def add_prices_from_dataframe(connection, instrument_id, data):
    for row in data.itertuples(index=False):
        add_price(
            connection=connection,
            instrument_id=instrument_id,
            date=row.date.strftime("%Y-%m-%d"),
            open_price=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            adjusted_close=row.adjusted_close,
            volume=int(row.volume),
            source=row.source,
            fetched_at=row.fetched_at,
        )




def get_latest_prices(
    connection,
    tickers,
    as_of_date,
):
    if not tickers:
        return pd.DataFrame(
            columns=["ticker", "date", "adjusted_close"]
        )

    placeholders = ", ".join("?" for _ in tickers)

    query = f"""
        SELECT
            i.ticker,
            p.date,
            p.adjusted_close
        FROM prices AS p
        JOIN instruments AS i
            ON p.instrument_id = i.id
        WHERE i.ticker IN ({placeholders})
          AND p.date <= ?
          AND p.date = (
              SELECT MAX(p2.date)
              FROM prices AS p2
              WHERE p2.instrument_id = p.instrument_id
                AND p2.date <= ?
          )
        ORDER BY i.ticker
    """

    parameters = [
        normalise_ticker(ticker)
        for ticker in tickers
    ]

    parameters.extend(
        [
            as_of_date,
            as_of_date,
        ]
    )

    return pd.read_sql_query(
        query,
        connection,
        params=parameters,
        parse_dates=["date"],
    )



def get_daily_returns(
    connection,
    ticker,
    start_date=None,
    end_date=None,
):
    prices = get_prices(
        connection,
        ticker,
        start_date,
        end_date,
    )

    if prices.empty:
        return pd.Series(
            dtype=float,
            name="return",
        )

    returns = prices["adjusted_close"].pct_change()

    returns.name = "return"

    return returns.dropna()




def get_portfolio_price_dates(
    connection,
    portfolio_id,
    start_date,
    end_date,
):
    """
    Return sorted dates with price observations for instruments
    traded in the portfolio, within the requested date range.

    These are candidate valuation dates, not a guarantee that
    every portfolio holding has a valid price on each date.
    """
    if end_date < start_date:
        raise ValueError(
            "End date must not be before start date"
        )

    rows = connection.execute(
        """
        SELECT DISTINCT p.date
        FROM prices AS p
        JOIN transactions AS t
            ON t.instrument_id = p.instrument_id
        WHERE t.portfolio_id = ?
          AND p.date >= ?
          AND p.date <= ?
        ORDER BY p.date
        """,
        (portfolio_id, start_date, end_date),
    ).fetchall()

    return [row[0] for row in rows]




def get_portfolio_price_coverage(
    connection,
    portfolio_id,
    valuation_date,
    max_age_days=0,
):
    """
    Check exact-date price coverage for active portfolio holdings.

    Active holdings are determined from transactions up to the
    valuation date. Previously available prices do not count
    as exact-date coverage.
    """
    if not isinstance(max_age_days, int) or isinstance(max_age_days, bool):
        raise ValueError(
            "max_age_days must be an integer"
        )

    if max_age_days < 0:
        raise ValueError(
            "max_age_days must not be negative"
        )
    
    rows = connection.execute(
        """
        SELECT
            instrument_id,
            SUM(
                CASE
                    WHEN transaction_type = 'BUY' THEN quantity
                    WHEN transaction_type = 'SELL' THEN -quantity
                    ELSE 0
                END
            ) AS net_quantity
        FROM transactions
        WHERE portfolio_id = ?
          AND date <= ?
        GROUP BY instrument_id
        HAVING net_quantity > 0
        ORDER BY instrument_id
        """,
        (portfolio_id, valuation_date),
    ).fetchall()

    active_instruments = [
        row[0] for row in rows
    ]

    missing_instruments = []

    for instrument_id in active_instruments:
        price_data = get_price_with_max_age(
            connection,
            instrument_id,
            valuation_date,
            max_age_days=max_age_days,
        )

        if price_data is None:
            missing_instruments.append(instrument_id)

    return {
        "valuation_date": valuation_date,
        "total_holdings": len(active_instruments),
        "priced_holdings": (
            len(active_instruments) - len(missing_instruments)
        ),
        "missing_instrument_ids": missing_instruments,
        "is_complete": len(missing_instruments) == 0,
    }



from datetime import date


def get_price_with_max_age(
    connection,
    instrument_id,
    valuation_date,
    max_age_days=0,
):
    """
    Retrieve the latest available closing price on or before
    the valuation date, subject to a maximum calendar-day age.

    max_age_days=0 requires an exact-date observation.

    Returns a dictionary containing the price, observation date,
    and age, or None if no acceptable observation exists.
    """
    if not isinstance(max_age_days, int) or isinstance(max_age_days, bool):
        raise ValueError("max_age_days must be an integer")

    if max_age_days < 0:
        raise ValueError("max_age_days must not be negative")

    valuation = date.fromisoformat(valuation_date)

    row = connection.execute(
        """
        SELECT date, close, adjusted_close
        FROM prices
        WHERE instrument_id = ?
          AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (instrument_id, valuation_date),
    ).fetchone()

    if row is None:
        return None

    observation_date = date.fromisoformat(row[0])
    age_days = (valuation - observation_date).days

    if age_days > max_age_days:
        return None

    return {
    "instrument_id": instrument_id,
    "valuation_date": valuation_date,
    "price_date": row[0],
    "close": float(row[1]),
    "adjusted_close": float(row[2]),
    "age_days": age_days,
    "is_stale": age_days > 0,
    }