
from pathlib import Path

import pandas as pd

from riskengine.db.connection import get_connection
from riskengine.db.portfolios import get_portfolio_value
from riskengine.db.transactions import get_cost_basis


def load_portfolio_holdings(
    db_path: str | Path,
    portfolio_id: int,
    as_of_date: str | None = None,
) -> pd.DataFrame:
    """
    Load portfolio holdings with split-adjusted quantities.

    Uses the existing portfolio valuation engine for market values
    and the existing cost-basis engine for investment costs.
    """

    with get_connection(db_path) as connection:
        portfolio = connection.execute(
            "SELECT id FROM portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()

        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} not found")

        if as_of_date is None:
            as_of_date = connection.execute(
                "SELECT MAX(date) FROM prices"
            ).fetchone()[0]

        if as_of_date is None:
            return pd.DataFrame()

        valuations = get_portfolio_value(
            connection,
            portfolio_id,
            as_of_date,
        )

        if valuations.empty:
            return pd.DataFrame()

        costs = get_cost_basis(connection, portfolio_id, as_of_date)

        metadata = pd.read_sql_query(
            """
            SELECT ticker, name, asset_type, sector
            FROM instruments
            """,
            connection,
        )

    holdings = valuations.merge(
        costs[["ticker", "total_cost"]],
        on="ticker",
        how="left",
        validate="one_to_one",
    ).merge(
        metadata,
        on="ticker",
        how="left",
        validate="one_to_one",
    )

    if holdings["total_cost"].isna().any():
        raise ValueError("Missing cost basis for one or more holdings")

    holdings = holdings.rename(
        columns={
            "quantity": "shares",
            "total_cost": "net_invested",
        }
    )

    holdings["latest_price"] = (
        holdings["market_value"] / holdings["shares"]
    )

    holdings["unrealized_pl"] = (
        holdings["market_value"] - holdings["net_invested"]
    )

    total = holdings["market_value"].sum()

    holdings["weight_pct"] = (
        holdings["market_value"] / total * 100
        if total > 0
        else 0.0
    )

    return holdings[
        [
            "ticker",
            "name",
            "asset_type",
            "sector",
            "shares",
            "net_invested",
            "latest_price",
            "price_date",
            "market_value",
            "unrealized_pl",
            "weight_pct",
        ]
    ].sort_values(
        "market_value",
        ascending=False,
    ).reset_index(drop=True)
