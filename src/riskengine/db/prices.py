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
    primary = connection.execute(
        """
        SELECT primary_source
        FROM instruments
        WHERE id = ?
        """,
        (instrument_id,),
    ).fetchone()

    if primary is None:
        raise ValueError(
            f"Unknown instrument_id: {instrument_id}"
        )

    if source != primary[0]:
        raise ValueError(
            f"Price source mismatch for instrument {instrument_id}: "
            f"primary={primary[0]}, received={source}"
        )

    existing_source = connection.execute(
        """
        SELECT source
        FROM prices
        WHERE instrument_id = ? AND date = ?
        """,
        (instrument_id, date),
    ).fetchone()

    if existing_source is not None and existing_source[0] != source:
        raise ValueError(
            f"Existing price source conflict for instrument "
            f"{instrument_id} on {date}: "
            f"stored={existing_source[0]}, received={source}"
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
    # Validate every row before writing any of them.
    primary = connection.execute(
        """
        SELECT primary_source
        FROM instruments
        WHERE id = ?
        """,
        (instrument_id,),
    ).fetchone()

    if primary is None:
        raise ValueError(
            f"Unknown instrument_id: {instrument_id}"
        )

    if not data.empty:
        invalid_sources = set(data["source"]) - {primary[0]}

        if invalid_sources:
            raise ValueError(
                f"Price source mismatch for instrument {instrument_id}: "
                f"primary={primary[0]}, "
                f"received={sorted(invalid_sources)}"
            )

    # Preflight source conflicts so no row is written before
    # an existing-source conflict is discovered.
    for row in data.itertuples(index=False):
        date_value = row.date.strftime("%Y-%m-%d")

        existing = connection.execute(
            """
            SELECT source
            FROM prices
            WHERE instrument_id = ? AND date = ?
            """,
            (instrument_id, date_value),
        ).fetchone()

        if existing is not None and existing[0] != row.source:
            raise ValueError(
                f"Existing price source conflict for instrument "
                f"{instrument_id} on {date_value}: "
                f"stored={existing[0]}, received={row.source}"
            )

    # Savepoint keeps this batch atomic, including when called
    # inside an existing ingestion transaction.
    connection.execute("SAVEPOINT price_batch")

    try:
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

        connection.execute("RELEASE SAVEPOINT price_batch")

    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT price_batch")
        connection.execute("RELEASE SAVEPOINT price_batch")
        raise



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
            p.close,
            p.adjusted_close,
            p.source
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





def validate_raw_price_sources(connection, instrument_id):
    """
    Ensure an instrument's stored prices are suitable for
    raw-price portfolio valuation.

    Currently, only Tiingo prices are verified as raw
    historical trading prices.
    """
    rows = connection.execute(
        """
        SELECT DISTINCT source
        FROM prices
        WHERE instrument_id = ?
        """,
        (instrument_id,),
    ).fetchall()

    sources = {row[0] for row in rows}

    if not sources:
        raise ValueError(
            f"No price history found for instrument {instrument_id}"
        )

    if sources != {"tiingo"}:
        raise ValueError(
            f"Instrument {instrument_id} has price sources "
            f"unsuitable for raw-price valuation: {sorted(sources)}"
        )

    return True




def get_missing_trading_days(
    connection,
    ticker,
    start_date,
    end_date,
    benchmark_ticker="SPY",
):
    """
    Find trading dates on which an instrument has no price.

    The benchmark's stored price dates define the trading
    calendar. No prices are downloaded by this function.

    This checks the supplied date interval; callers must
    restrict it to the period when a holding was active.
    """
    if end_date < start_date:
        raise ValueError("End date must not be before start date")

    ticker = normalise_ticker(ticker)
    benchmark_ticker = normalise_ticker(benchmark_ticker)

    benchmark_dates = get_prices(
        connection,
        benchmark_ticker,
        start_date,
        end_date,
    ).index

    if benchmark_dates.empty:
        raise ValueError(
            f"No benchmark prices found for {benchmark_ticker} "
            f"between {start_date} and {end_date}"
        )

    instrument_dates = get_prices(
        connection,
        ticker,
        start_date,
        end_date,
    ).index

    missing_dates = benchmark_dates.difference(instrument_dates)

    return [
        trading_date.strftime("%Y-%m-%d")
        for trading_date in missing_dates.sort_values()
    ]




def get_portfolio_trading_day_gaps(
    connection,
    portfolio_id,
    start_date,
    end_date,
    benchmark_ticker="SPY",
):
    """
    Detect missing prices for holdings active on each benchmark
    trading day.

    Uses split-aware historical positions. A holding is considered
    active on its purchase date and inactive after a complete sale.

    The benchmark must have stored prices in the requested interval.
    """
    from riskengine.db.transactions import (
        get_positions,
        validate_transactions,
    )

    if end_date < start_date:
        raise ValueError("End date must not be before start date")

    validate_transactions(connection, portfolio_id)

    benchmark_ticker = normalise_ticker(benchmark_ticker)

    benchmark_prices = get_prices(
        connection,
        benchmark_ticker,
        start_date,
        end_date,
    )

    if benchmark_prices.empty:
        raise ValueError(
            f"No benchmark prices found for {benchmark_ticker} "
            f"between {start_date} and {end_date}"
        )

    trading_dates = [
        value.strftime("%Y-%m-%d")
        for value in benchmark_prices.index
    ]

    gaps = []

    for trading_date in trading_dates:
        positions = get_positions(
            connection,
            portfolio_id,
            as_of_date=trading_date,
        )

        if positions.empty:
            continue

        active_tickers = positions.loc[
            positions["quantity"] > 1e-9,
            "ticker",
        ].tolist()

        for ticker in active_tickers:
            exists = connection.execute(
                """
                SELECT 1
                FROM prices AS p
                JOIN instruments AS i
                    ON i.id = p.instrument_id
                WHERE i.ticker = ?
                  AND p.date = ?
                LIMIT 1
                """,
                (ticker, trading_date),
            ).fetchone()

            if exists is None:
                gaps.append(
                    {
                        "date": trading_date,
                        "ticker": ticker,
                    }
                )

    return {
        "portfolio_id": portfolio_id,
        "start_date": start_date,
        "end_date": end_date,
        "benchmark_ticker": benchmark_ticker,
        "trading_days_checked": len(trading_dates),
        "missing_prices": gaps,
        "missing_count": len(gaps),
        "is_complete": len(gaps) == 0,
    }