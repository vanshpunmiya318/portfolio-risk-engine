import math

import pandas as pd

from riskengine.db.portfolios import get_portfolio_value


def get_portfolio_exposure(
    connection,
    portfolio_id: int,
    as_of_date: str,
    max_age_days: int | None = 0,
) -> dict:
    """
    Calculate security and sector exposures for invested holdings.

    Cash is excluded. Weights are normalized to the total market
    value of active invested holdings.

    Instruments without a sector are grouped under 'Unclassified'.
    """

    holdings = get_portfolio_value(
        connection,
        portfolio_id,
        as_of_date,
        max_age_days=max_age_days,
    )

    if holdings.empty:
        return {
            "portfolio_id": portfolio_id,
            "as_of_date": as_of_date,
            "weight_basis": "invested_holdings_excluding_cash",
            "total_invested_value": 0.0,
            "security_exposures": [],
            "sector_exposures": [],
            "largest_security": None,
            "largest_sector": None,
        }

    holdings = holdings.copy()

    if holdings["ticker"].duplicated().any():
        raise ValueError("Duplicate ticker in portfolio valuation")

    if not holdings["market_value"].map(math.isfinite).all():
        raise ValueError("Non-finite holding market value")

    if (holdings["market_value"] <= 0).any():
        raise ValueError("Holding market values must be positive")

    total_value = float(holdings["market_value"].sum())

    if not math.isfinite(total_value) or total_value <= 0:
        raise ValueError("Total invested market value must be positive")

    instrument_rows = connection.execute(
        """
        SELECT ticker, sector
        FROM instruments
        """
    ).fetchall()

    sectors_by_ticker = {
        row[0]: row[1]
        for row in instrument_rows
    }

    holdings["sector"] = holdings["ticker"].map(
        lambda ticker: (
            str(sectors_by_ticker.get(ticker)).strip()
            if sectors_by_ticker.get(ticker) is not None
            and str(sectors_by_ticker.get(ticker)).strip()
            else "Unclassified"
        )
    )

    holdings["weight"] = holdings["market_value"] / total_value

    holdings = holdings.sort_values(
        ["market_value", "ticker"],
        ascending=[False, True],
    )

    security_exposures = [
        {
            "ticker": str(row.ticker),
            "sector": str(row.sector),
            "quantity": float(row.quantity),
            "market_value": float(row.market_value),
            "weight": float(row.weight),
        }
        for row in holdings.itertuples(index=False)
    ]

    sector_summary = (
        holdings.groupby("sector", as_index=False)
        .agg(
            market_value=("market_value", "sum"),
            holdings_count=("ticker", "count"),
        )
    )

    sector_summary["weight"] = (
        sector_summary["market_value"] / total_value
    )

    sector_summary = sector_summary.sort_values(
        ["market_value", "sector"],
        ascending=[False, True],
    )

    sector_exposures = [
        {
            "sector": str(row.sector),
            "market_value": float(row.market_value),
            "weight": float(row.weight),
            "holdings_count": int(row.holdings_count),
        }
        for row in sector_summary.itertuples(index=False)
    ]

    security_total = sum(
        item["market_value"]
        for item in security_exposures
    )

    sector_total = sum(
        item["market_value"]
        for item in sector_exposures
    )

    if not math.isclose(
        security_total,
        total_value,
        rel_tol=1e-10,
        abs_tol=1e-8,
    ):
        raise ValueError("Security exposure reconciliation failed")

    if not math.isclose(
        sector_total,
        total_value,
        rel_tol=1e-10,
        abs_tol=1e-8,
    ):
        raise ValueError("Sector exposure reconciliation failed")

    return {
        "portfolio_id": portfolio_id,
        "as_of_date": as_of_date,
        "weight_basis": "invested_holdings_excluding_cash",
        "total_invested_value": total_value,
        "security_exposures": security_exposures,
        "sector_exposures": sector_exposures,
        "largest_security": security_exposures[0],
        "largest_sector": sector_exposures[0],
    }