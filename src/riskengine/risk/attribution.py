
import pandas as pd


def calculate_performance_attribution(
    connection,
    portfolio_id: int,
    as_of_date: str,
) -> pd.DataFrame:
    """
    Calculate realized and unrealized price gains by instrument.

    Uses average-cost accounting for sales and adjusts share
    quantities and average cost for stock splits.

    Excludes dividends, fees, taxes and currency effects.
    """
    rows = connection.execute(
        """
        SELECT
            t.date,
            t.id,
            t.instrument_id,
            i.ticker,
            t.transaction_type,
            t.quantity,
            t.price
        FROM transactions AS t
        JOIN instruments AS i ON i.id = t.instrument_id
        WHERE t.portfolio_id = ?
          AND t.date <= ?
        ORDER BY t.date, t.id
        """,
        (portfolio_id, as_of_date),
    ).fetchall()

    if not rows:
        return pd.DataFrame(
            columns=[
                "ticker",
                "initial_investment",
                "ending_value",
                "realized_gain_usd",
                "unrealized_gain_usd",
                "gain_usd",
                "return_pct",
            ]
        )

    instrument_ids = sorted({row[2] for row in rows})
    placeholders = ",".join("?" for _ in instrument_ids)

    split_rows = connection.execute(
        f"""
        SELECT instrument_id, ex_date, id, ratio
        FROM corporate_actions
        WHERE instrument_id IN ({placeholders})
          AND action_type = 'SPLIT'
          AND ex_date <= ?
        ORDER BY ex_date, id
        """,
        (*instrument_ids, as_of_date),
    ).fetchall()

    events = []

    for trade_date, trade_id, instrument_id, ticker, kind, quantity, price in rows:
        events.append(
            (
                trade_date, 1, trade_id, instrument_id,
                ticker, kind, float(quantity), float(price),
            )
        )

    for instrument_id, split_date, split_id, ratio in split_rows:
        events.append(
            (
                split_date, 0, split_id, instrument_id,
                None, "SPLIT", float(ratio), 0.0,
            )
        )

    events.sort(key=lambda event: (event[0], event[1], event[2]))

    positions = {
        instrument_id: {
            "quantity": 0.0,
            "cost": 0.0,
            "realized": 0.0,
        }
        for instrument_id in instrument_ids
    }

    tickers = {row[2]: row[3] for row in rows}

    for _, _, _, instrument_id, _, kind, quantity, price in events:
        position = positions[instrument_id]

        if kind == "SPLIT":
            position["quantity"] *= quantity

        elif kind == "BUY":
            position["quantity"] += quantity
            position["cost"] += quantity * price

        elif kind == "SELL":
            held = position["quantity"]

            if quantity > held + 1e-9:
                raise ValueError(
                    f"Insufficient shares to sell {tickers[instrument_id]}"
                )

            average_cost = position["cost"] / held
            sold_cost = quantity * average_cost

            position["realized"] += quantity * price - sold_cost
            position["quantity"] -= quantity
            position["cost"] -= sold_cost

            if abs(position["quantity"]) < 1e-9:
                position["quantity"] = 0.0
                position["cost"] = 0.0

    results = []

    for instrument_id, position in positions.items():
        ticker = tickers[instrument_id]

        price_row = connection.execute(
            """
            SELECT close
            FROM prices
            WHERE instrument_id = ?
              AND date = ?
            """,
            (instrument_id, as_of_date),
        ).fetchone()

        if price_row is None:
            raise ValueError(
                f"Missing closing price for {ticker} on {as_of_date}"
            )

        quantity = position["quantity"]
        remaining_cost = position["cost"]
        ending_value = quantity * float(price_row[0])

        realized = position["realized"]
        unrealized = ending_value - remaining_cost
        total_gain = realized + unrealized

        results.append(
            {
                "ticker": ticker,
                "initial_investment": remaining_cost,
                "ending_value": ending_value,
                "realized_gain_usd": realized,
                "unrealized_gain_usd": unrealized,
                "gain_usd": total_gain,
                "return_pct": (
                    unrealized / remaining_cost * 100
                    if remaining_cost > 0
                    else 0.0
                ),
            }
        )

    return pd.DataFrame(results).sort_values(
        "gain_usd", ascending=False
    ).reset_index(drop=True)
