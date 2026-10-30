import pandas as pd

from riskengine.risk.metrics import (
    annualized_volatility,
    maximum_drawdown,
    historical_var,
    historical_cvar,
    portfolio_beta,
)
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns


def get_portfolio_risk_summary(
    connection,
    portfolio_id: int,
    dates,
    benchmark_returns: pd.Series | None = None,
    confidence: float = 0.95,
    trading_days: int = 252,
    max_age_days: int = 0,
) -> dict:
    """
    Calculate risk metrics from cash-flow-aware portfolio returns.

    Benchmark returns must be indexed by date.
    Beta is calculated only when benchmark returns are supplied.
    """

    returns = get_portfolio_daily_returns(
        connection=connection,
        portfolio_id=portfolio_id,
        dates=dates,
        max_age_days=max_age_days,
    )

    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")

    if trading_days <= 0:
        raise ValueError("trading_days must be positive")

    summary = {
        "portfolio_id": portfolio_id,
        "observations": len(returns),
        "start_date": returns.index.min().strftime("%Y-%m-%d"),
        "end_date": returns.index.max().strftime("%Y-%m-%d"),
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