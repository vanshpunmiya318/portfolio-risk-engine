import pandas as pd

from riskengine.db.portfolios import get_portfolio_subperiod_return


def get_portfolio_daily_returns(
    connection,
    portfolio_id: int,
    dates,
    max_age_days: int = 0,
) -> pd.Series:
    """
    Calculate cash-flow-aware portfolio returns between valuation dates.

    Each return is calculated using the existing portfolio
    subperiod-return function, which accounts for external
    deposits and withdrawals.

    Returns are indexed by the ending valuation date.
    """

    if max_age_days < 0:
        raise ValueError("max_age_days cannot be negative")

    valuation_dates = pd.DatetimeIndex(pd.to_datetime(dates))

    if valuation_dates.hasnans:
        raise ValueError("Valuation dates cannot contain missing values")

    if valuation_dates.has_duplicates:
        raise ValueError("Valuation dates must be unique")

    valuation_dates = valuation_dates.sort_values()

    if len(valuation_dates) < 2:
        raise ValueError("At least two valuation dates are required")

    returns = []

    for previous_date, current_date in zip(
        valuation_dates[:-1],
        valuation_dates[1:],
    ):
        daily_return = get_portfolio_subperiod_return(
            connection,
            portfolio_id,
            previous_date.strftime("%Y-%m-%d"),
            current_date.strftime("%Y-%m-%d"),
            max_age_days=max_age_days,
        )

        returns.append(float(daily_return))

    return pd.Series(
        returns,
        index=valuation_dates[1:],
        name="portfolio_return",
        dtype=float,
    )