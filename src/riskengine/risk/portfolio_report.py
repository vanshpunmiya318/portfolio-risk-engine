import pandas as pd

from riskengine.db.prices import (
    get_prices,
    get_portfolio_price_dates,
    get_portfolio_trading_day_gaps,
)
from riskengine.risk.benchmark import get_benchmark_price_returns
from riskengine.risk.summary import get_portfolio_risk_summary


def get_portfolio_risk_report(
    connection,
    portfolio_id: int,
    start_date: str,
    end_date: str,
    benchmark_ticker: str = "SPY",
    confidence: float = 0.95,
    trading_days: int = 252,
    max_age_days: int = 0,
) -> dict:
    """
    Calculate portfolio risk against a price-return benchmark.

    Every observed benchmark date in the requested interval
    must have a candidate portfolio price date.

    Portfolio valuation itself is validated by the existing
    cash-flow-aware subperiod-return calculations.
    """

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)

    if end < start:
        raise ValueError("end_date cannot precede start_date")

    benchmark_prices = get_prices(
        connection,
        benchmark_ticker,
        start_date,
        end_date,
    )

    if benchmark_prices.empty:
        raise ValueError(
            f"No prices found for {benchmark_ticker}"
        )

    benchmark_dates = pd.DatetimeIndex(
        pd.to_datetime(benchmark_prices.index)
    ).sort_values()

    if benchmark_dates.has_duplicates:
        raise ValueError(
            "Benchmark price dates must be unique"
        )

    if start not in benchmark_dates:
        raise ValueError(
            "Benchmark start date has no price observation"
        )

    if end not in benchmark_dates:
        raise ValueError(
            "Benchmark end date has no price observation"
        )

    if len(benchmark_dates) < 3:
        raise ValueError(
            "At least three benchmark price dates are required"
        )

    portfolio_dates = pd.DatetimeIndex(
        pd.to_datetime(
            get_portfolio_price_dates(
                connection,
                portfolio_id,
                start_date,
                end_date,
            )
        )
    )

    missing_dates = benchmark_dates.difference(portfolio_dates)

    if len(missing_dates) > 0:
        missing = [
            date.strftime("%Y-%m-%d")
            for date in missing_dates
        ]

        raise ValueError(
            f"Missing portfolio price dates: {missing}"
        )

    coverage = get_portfolio_trading_day_gaps(
        connection=connection,
        portfolio_id=portfolio_id,
        start_date=start_date,
        end_date=end_date,
        benchmark_ticker=benchmark_ticker,
    )

    if not coverage["is_complete"]:
        missing = coverage["missing_prices"]

        raise ValueError(
            f"Incomplete portfolio price coverage: {missing}"
        )
    
    benchmark_returns = get_benchmark_price_returns(
        connection,
        benchmark_ticker,
        start_date,
        end_date,
    )

    summary = get_portfolio_risk_summary(
        connection=connection,
        portfolio_id=portfolio_id,
        dates=benchmark_dates,
        benchmark_returns=benchmark_returns,
        confidence=confidence,
        trading_days=trading_days,
        max_age_days=max_age_days,
    )

    summary["benchmark"] = benchmark_ticker
    summary["benchmark_return_basis"] = "raw_close_price_return"

    return summary