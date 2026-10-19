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
            i.ticker,
            SUM(
                CASE
                    WHEN t.transaction_type = 'BUY' THEN t.quantity
                    WHEN t.transaction_type = 'SELL' THEN -t.quantity
                END
            ) AS quantity
        FROM transactions AS t
        JOIN instruments AS i
            ON t.instrument_id = i.id
        WHERE t.portfolio_id = ?
    """

    parameters = [portfolio_id]

    if as_of_date is not None:
        query += " AND t.date <= ?"
        parameters.append(as_of_date)

    query += """
        GROUP BY i.id, i.ticker
        ORDER BY i.ticker
    """

    return pd.read_sql_query(
        query,
        connection,
        params=parameters,
    )



def validate_transactions(connection, portfolio_id):
    transactions = pd.read_sql_query(
        """
        SELECT
            t.date,
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

    positions = {}

    for row in transactions.itertuples(index=False):
        current_quantity = positions.get(row.ticker, 0)

        if row.transaction_type == "BUY":
            current_quantity += row.quantity
        else:
            current_quantity -= row.quantity

        positions[row.ticker] = current_quantity

        if current_quantity < 0:
            raise ValueError(
                f"Negative position detected for {row.ticker}"
            )

    return True



def get_cost_basis(connection, portfolio_id):
    validate_transactions(
        connection,
        portfolio_id,
        )
    transactions = pd.read_sql_query(
        """
        SELECT
            t.date,
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

    positions = {}
    costs = {}

    for row in transactions.itertuples(index=False):
        current_quantity = positions.get(row.ticker, 0)
        current_cost = costs.get(row.ticker, 0)

        if row.transaction_type == "BUY":
            current_quantity += row.quantity
            current_cost += row.quantity * row.price

        else:
            average_cost = current_cost / current_quantity
            current_quantity -= row.quantity
            current_cost -= row.quantity * average_cost

        positions[row.ticker] = current_quantity
        costs[row.ticker] = current_cost

    result = []

    for ticker in sorted(positions):
        quantity = positions[ticker]
        total_cost = costs[ticker]

        average_cost = (
            total_cost / quantity
            if quantity > 0
            else 0
        )

        result.append(
            {
                "ticker": ticker,
                "quantity": quantity,
                "total_cost": total_cost,
                "average_cost": average_cost,
            }
        )

    return pd.DataFrame(result)




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