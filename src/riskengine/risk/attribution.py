
import pandas as pd

from riskengine.db.transactions import get_cost_basis, get_positions


def calculate_performance_attribution(
    connection,
    portfolio_id: int,
    as_of_date: str,
) -> pd.DataFrame:
    """
    Calculate unrealized price gains for each holding.

    Excludes dividends, fees, and realized gains from sales.
    """

    transactions = connection.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE portfolio_id = ?
          AND transaction_type = 'SELL'
          AND date <= ?
        """,
        (portfolio_id, as_of_date),
    ).fetchone()[0]

    if transactions:
        raise ValueError(
            "This attribution currently supports portfolios "
            "without sales."
        )

    cost_basis = get_cost_basis(connection, portfolio_id)
    positions = get_positions(connection, portfolio_id, as_of_date)

    results = []

    for holding in positions.itertuples(index=False):
        ticker = holding.ticker
        quantity = float(holding.quantity)

        cost = cost_basis.loc[
            cost_basis["ticker"] == ticker, "total_cost"
        ]

        if cost.empty:
            raise ValueError(f"Missing cost basis for {ticker}")

        purchase_cost = float(cost.iloc[0])

        price_row = connection.execute(
            """
            SELECT p.close
            FROM prices p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE i.ticker = ?
              AND p.date = ?
            """,
            (ticker, as_of_date),
        ).fetchone()

        if price_row is None:
            raise ValueError(
                f"Missing closing price for {ticker} on {as_of_date}"
            )

        market_value = quantity * float(price_row[0])
        gain = market_value - purchase_cost

        results.append(
            {
                "ticker": ticker,
                "initial_investment": purchase_cost,
                "ending_value": market_value,
                "gain_usd": gain,
                "return_pct": (
                    gain / purchase_cost * 100
                    if purchase_cost > 0
                    else 0.0
                ),
            }
        )

    return pd.DataFrame(results).sort_values(
        "gain_usd", ascending=False
    ).reset_index(drop=True)
