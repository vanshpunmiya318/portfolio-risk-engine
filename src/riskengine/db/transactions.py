import math

import pandas as pd


def add_transaction(
    connection,
    portfolio_id,
    instrument_id,
    date,
    transaction_type,
    quantity,
    price,
    currency,
    source,
):
    transaction_type = transaction_type.strip().upper()
    currency = currency.strip().upper()

    if transaction_type not in {"BUY", "SELL"}:
        raise ValueError(
            "Transaction type must be BUY or SELL"
        )

    cursor = connection.execute(
        """
        INSERT INTO transactions (
            portfolio_id,
            instrument_id,
            date,
            transaction_type,
            quantity,
            price,
            currency,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            portfolio_id,
            instrument_id,
            date,
            transaction_type,
            quantity,
            price,
            currency,
            source,
        ),
    )

    return cursor.lastrowid


def get_transactions(connection, portfolio_id):
    return pd.read_sql_query(
        """
        SELECT
            t.date,
            i.ticker,
            t.transaction_type,
            t.quantity,
            t.price,
            t.currency,
            t.source
        FROM transactions AS t
        JOIN instruments AS i
            ON t.instrument_id = i.id
        WHERE t.portfolio_id = ?
        ORDER BY t.date, t.id
        """,
        connection,
        params=(portfolio_id,),
        parse_dates=["date"],
    )




def get_positions(connection, portfolio_id, as_of_date=None):
    query = """
        SELECT
            t.instrument_id,
            i.ticker,
            t.date,
            t.transaction_type,
            t.quantity,
            t.id
        FROM transactions AS t
        JOIN instruments AS i
            ON t.instrument_id = i.id
        WHERE t.portfolio_id = ?
    """

    parameters = [portfolio_id]

    if as_of_date is not None:
        query += " AND t.date <= ?"
        parameters.append(as_of_date)

    query += " ORDER BY t.date, t.id"

    transactions = connection.execute(
        query, parameters
    ).fetchall()

    positions = {}

    for (
        instrument_id,
        ticker,
        transaction_date,
        transaction_type,
        quantity,
        transaction_id,
    ) in transactions:
        signed_quantity = (
            quantity if transaction_type == "BUY" else -quantity
        )

        split_query = """
            SELECT COALESCE(
                EXP(SUM(LN(ratio))),
                1.0
            )
            FROM corporate_actions
            WHERE instrument_id = ?
              AND action_type = 'SPLIT'
              AND ex_date > ?
        """

        split_parameters = [instrument_id, transaction_date]

        if as_of_date is not None:
            split_query += " AND ex_date <= ?"
            split_parameters.append(as_of_date)

        split_multiplier = connection.execute(
            split_query, split_parameters
        ).fetchone()[0]

        positions[ticker] = (
            positions.get(ticker, 0.0)
            + signed_quantity * split_multiplier
        )

    import pandas as pd

    return pd.DataFrame(
        [
            {"ticker": ticker, "quantity": quantity}
            for ticker, quantity in sorted(positions.items())
        ],
        columns=["ticker", "quantity"],
    )




def validate_transactions(connection, portfolio_id):
    transactions = pd.read_sql_query(
        """
        SELECT
            t.date,
            t.id,
            t.instrument_id,
            i.ticker,
            t.transaction_type,
            t.quantity
        FROM transactions AS t
        JOIN instruments AS i
            ON t.instrument_id = i.id
        WHERE t.portfolio_id = ?
        ORDER BY t.date, t.id
        """,
        connection,
        params=(portfolio_id,),
    )

    if transactions.empty:
        return True

    instrument_ids = transactions["instrument_id"].unique().tolist()

    placeholders = ",".join("?" for _ in instrument_ids)

    splits = pd.read_sql_query(
        f"""
        SELECT instrument_id, ex_date, ratio
        FROM corporate_actions
        WHERE action_type = 'SPLIT'
          AND instrument_id IN ({placeholders})
        ORDER BY ex_date, id
        """,
        connection,
        params=instrument_ids,
    )

    events = []

    for row in transactions.itertuples(index=False):
        events.append(
            (
                row.date,
                1,
                row.id,
                row.instrument_id,
                row.ticker,
                row.transaction_type,
                row.quantity,
            )
        )

    for row in splits.itertuples(index=False):
        events.append(
            (
                row.ex_date,
                0,
                0,
                row.instrument_id,
                None,
                "SPLIT",
                row.ratio,
            )
        )

    events.sort(key=lambda event: (event[0], event[1], event[2]))

    positions = {}
    tickers = dict(
        zip(transactions["instrument_id"], transactions["ticker"])
    )

    for (
        event_date,
        priority,
        event_id,
        instrument_id,
        ticker,
        event_type,
        quantity,
    ) in events:
        current_quantity = positions.get(instrument_id, 0.0)

        if event_type == "SPLIT":
            current_quantity *= quantity
        elif event_type == "BUY":
            current_quantity += quantity
        else:
            current_quantity -= quantity

        positions[instrument_id] = current_quantity

        if current_quantity < -1e-9:
            raise ValueError(
                f"Negative position detected for "
                f"{tickers[instrument_id]} on {event_date}"
            )

    return True




def get_cost_basis(connection, portfolio_id):
    validate_transactions(connection, portfolio_id)

    transactions = pd.read_sql_query(
        """
        SELECT
            t.id,
            t.date,
            t.instrument_id,
            i.ticker,
            t.transaction_type,
            t.quantity,
            t.price
        FROM transactions AS t
        JOIN instruments AS i
            ON t.instrument_id = i.id
        WHERE t.portfolio_id = ?
        ORDER BY t.date, t.id
        """,
        connection,
        params=(portfolio_id,),
    )

    if transactions.empty:
        return pd.DataFrame(
            columns=["ticker", "quantity", "total_cost", "average_cost"]
        )

    instrument_ids = transactions["instrument_id"].unique().tolist()
    placeholders = ",".join("?" for _ in instrument_ids)

    splits = pd.read_sql_query(
        f"""
        SELECT id, instrument_id, ex_date, ratio
        FROM corporate_actions
        WHERE action_type = 'SPLIT'
          AND instrument_id IN ({placeholders})
        ORDER BY ex_date, id
        """,
        connection,
        params=instrument_ids,
    )

    events = []

    for row in transactions.itertuples(index=False):
        events.append(
            (
                row.date,
                1,
                row.id,
                row.instrument_id,
                row.ticker,
                row.transaction_type,
                row.quantity,
                row.price,
            )
        )

    for row in splits.itertuples(index=False):
        events.append(
            (
                row.ex_date,
                0,
                row.id,
                row.instrument_id,
                None,
                "SPLIT",
                row.ratio,
                0.0,
            )
        )

    events.sort(key=lambda event: (event[0], event[1], event[2]))

    positions = {}
    costs = {}

    tickers = dict(
        zip(transactions["instrument_id"], transactions["ticker"])
    )

    for (
        event_date,
        priority,
        event_id,
        instrument_id,
        ticker,
        event_type,
        quantity,
        price,
    ) in events:
        current_quantity = positions.get(instrument_id, 0.0)
        current_cost = costs.get(instrument_id, 0.0)

        if event_type == "SPLIT":
            current_quantity *= quantity

        elif event_type == "BUY":
            current_quantity += quantity
            current_cost += quantity * price

        elif event_type == "SELL":
            average_cost = current_cost / current_quantity
            current_quantity -= quantity
            current_cost -= quantity * average_cost

            if abs(current_quantity) < 1e-9:
                current_quantity = 0.0
                current_cost = 0.0

        positions[instrument_id] = current_quantity
        costs[instrument_id] = current_cost

    result = []

    for instrument_id in sorted(
        positions, key=lambda item: tickers[item]
    ):
        quantity = positions[instrument_id]
        total_cost = costs[instrument_id]

        average_cost = (
            total_cost / quantity if quantity > 0 else 0.0
        )

        result.append(
            {
                "ticker": tickers[instrument_id],
                "quantity": quantity,
                "total_cost": total_cost,
                "average_cost": average_cost,
            }
        )

    return pd.DataFrame(
        result,
        columns=["ticker", "quantity", "total_cost", "average_cost"],
    )





def add_cash_flow(
    connection,
    portfolio_id,
    date,
    flow_type,
    amount,
    currency,
    source,
):
    flow_type = flow_type.strip().upper()
    currency = currency.strip().upper()

    if flow_type not in {"DEPOSIT", "WITHDRAWAL"}:
        raise ValueError(
            "Flow type must be DEPOSIT or WITHDRAWAL"
        )

    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(
            "Cash flow amount must be a positive finite number"
        )

    cursor = connection.execute(
        """
        INSERT INTO cash_flows (
            portfolio_id,
            date,
            flow_type,
            amount,
            currency,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            portfolio_id,
            date,
            flow_type,
            amount,
            currency,
            source,
        ),
    )

    return cursor.lastrowid




def get_cash_flows(connection, portfolio_id):
    return pd.read_sql_query(
        """
        SELECT
            date,
            flow_type,
            amount,
            currency,
            source
        FROM cash_flows
        WHERE portfolio_id = ?
        ORDER BY date, id
        """,
        connection,
        params=(portfolio_id,),
        parse_dates=["date"],
    )




def get_net_cash_flows(
    connection,
    portfolio_id,
    as_of_date,
):
    result = connection.execute(
        """
        SELECT COALESCE(
            SUM(
                CASE
                    WHEN flow_type = 'DEPOSIT' THEN amount
                    WHEN flow_type = 'WITHDRAWAL' THEN -amount
                END
            ),
            0
        )
        FROM cash_flows
        WHERE portfolio_id = ?
          AND date <= ?
        """,
        (portfolio_id, as_of_date),
    ).fetchone()

    return result[0]




def get_cash_balance(
    connection,
    portfolio_id,
    as_of_date,
):
    net_cash_flows = get_net_cash_flows(
        connection,
        portfolio_id,
        as_of_date,
    )

    result = connection.execute(
        """
        SELECT COALESCE(
            SUM(
                CASE
                    WHEN transaction_type = 'BUY'
                        THEN -quantity * price
                    WHEN transaction_type = 'SELL'
                        THEN quantity * price
                END
            ),
            0
        )
        FROM transactions
        WHERE portfolio_id = ?
          AND date <= ?
        """,
        (portfolio_id, as_of_date),
    ).fetchone()

    return net_cash_flows + result[0]





def get_cash_flow_dates(
    connection,
    portfolio_id,
    start_date,
    end_date,
):
    result = connection.execute(
        """
        SELECT DISTINCT date
        FROM cash_flows
        WHERE portfolio_id = ?
          AND date > ?
          AND date <= ?
        ORDER BY date
        """,
        (
            portfolio_id,
            start_date,
            end_date,
        ),
    ).fetchall()

    return [row[0] for row in result]




def get_cash_flow_on_date(
    connection,
    portfolio_id,
    date,
):
    result = connection.execute(
        """
        SELECT COALESCE(
            SUM(
                CASE
                    WHEN flow_type = 'DEPOSIT' THEN amount
                    WHEN flow_type = 'WITHDRAWAL' THEN -amount
                END
            ),
            0
        )
        FROM cash_flows
        WHERE portfolio_id = ?
          AND date = ?
        """,
        (portfolio_id, date),
    ).fetchone()

    return result[0]