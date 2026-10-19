def normalise_ticker(ticker):
    return ticker.strip().upper()


def add_instrument(
    connection,
    ticker,
    name,
    asset_type,
    currency,
    sector=None,
):
    cursor = connection.execute(
        """
        INSERT INTO instruments (
            ticker,
            name,
            asset_type,
            currency,
            sector
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            normalise_ticker(ticker),
            name,
            asset_type,
            currency,
            sector,
        ),
    )

    return cursor.lastrowid


def get_instrument_id(connection, ticker):
    result = connection.execute(
        """
        SELECT id
        FROM instruments
        WHERE ticker = ?
        """,
        (normalise_ticker(ticker),),
    ).fetchone()

    if result is None:
        return None

    return result[0]


def get_or_create_instrument(
    connection,
    ticker,
    name,
    asset_type,
    currency,
    sector=None,
):
    instrument_id = get_instrument_id(connection, ticker)

    if instrument_id is not None:
        return instrument_id

    return add_instrument(
        connection,
        ticker,
        name,
        asset_type,
        currency,
        sector,
    )