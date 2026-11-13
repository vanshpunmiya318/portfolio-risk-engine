import pandas as pd

from riskengine.risk.metrics import (
    annualized_volatility,
    maximum_drawdown,
    historical_var,
    historical_cvar,
    portfolio_beta,
)
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns
from riskengine.risk.performance import analysis_period_metadata


def get_portfolio_risk_summary(
    connection,
    portfolio_id: int,
    dates,
    benchmark_returns: pd.Series | None = None,
    confidence: float = 0.95,
    trading_days: int = 252,
    max_age_days: int = 0,
    *,
    requested_start_date: str | None = None,
) -> dict:
    """
    Calculate risk metrics from cash-flow-aware portfolio returns.

    Benchmark returns must be indexed by date.
    Beta is calculated only when benchmark returns are supplied.
    start_date is the actual first valuation/baseline, not the first return
    ending date. Valuation boundaries alone do not certify daily sessions.
    """

    dates = pd.DatetimeIndex(pd.to_datetime(dates)).sort_values()
    returns = get_portfolio_daily_returns(
        connection=connection,
        portfolio_id=portfolio_id,
        dates=dates,
        max_age_days=max_age_days,
    )
    if returns.empty:
        raise ValueError("No return observations available; analysis period is undefined")

    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")

    if trading_days <= 0:
        raise ValueError("trading_days must be positive")

    summary = {
        "portfolio_id": portfolio_id,
        "observations": len(returns),
        "start_date": dates[0].strftime("%Y-%m-%d"),
        "end_date": returns.index.max().strftime("%Y-%m-%d"),
        **analysis_period_metadata(
            requested_start_date=requested_start_date, baseline_date=dates[0],
            first_return_end_date=returns.index.min(), effective_end_date=returns.index.max(),
        ),
        "annualized_volatility": annualized_volatility(
            returns, trading_days=trading_days
        ),
        "maximum_drawdown": maximum_drawdown(returns),
        "historical_var": historical_var(
            returns, confidence=confidence
        ),
        "historical_cvar": historical_cvar(
            returns, confidence=confidence
        ),
        "beta": None,
    }

    if benchmark_returns is not None:
        summary["beta"] = portfolio_beta(
            returns,
            benchmark_returns,
        )

    return summary