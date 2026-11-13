import math

import pandas as pd

from riskengine.risk.performance import calculate_performance_metrics


def calculate_live_risk_metrics(frame, portfolio_returns=None):
    """Risk from explicitly supplied cash-flow-adjusted investment returns.

    Callers should use get_portfolio_daily_returns(). An equity frame cannot
    establish external cash flows, so it is never a substitute for returns.
    Missing, incomplete or invalid returns produce undefined floats (NaN),
    preserving the existing keys and undefined-metric representation. Valid
    supplied returns retain the existing formulas and metric conventions.
    """
    undefined = {key: float("nan") for key in
                 ("volatility", "drawdown", "sharpe", "var", "cvar", "beta")}
    if portfolio_returns is None:
        return undefined
    try:
        portfolio_returns = pd.Series(portfolio_returns, dtype=float)
    except (TypeError, ValueError, OverflowError):
        return undefined
    if (len(portfolio_returns) < 2 or portfolio_returns.index.has_duplicates
            or portfolio_returns.isna().any()
            or not all(math.isfinite(value) for value in portfolio_returns)
            or (portfolio_returns < -1).any()):
        return undefined

    benchmark_returns = (
        pd.Series(
            frame["benchmark"].to_numpy(dtype=float),
            index=pd.to_datetime(frame["date"]),
            dtype=float,
        )
        .pct_change(fill_method=None)
        .dropna()
    )

    aligned = pd.concat(
        [
            portfolio_returns.rename("portfolio"),
            benchmark_returns.rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()

    performance = calculate_performance_metrics(
        portfolio_returns,
        initial_capital=1.0,
        risk_free_rate=0.0,
    )

    volatility = performance["annualized_volatility"]
    max_drawdown = performance["max_drawdown"]

    sharpe = performance["sharpe_ratio"]
    if sharpe is None:
        sharpe = float("nan")

    threshold = portfolio_returns.quantile(0.05)
    var_95 = -threshold
    cvar_95 = -portfolio_returns[
        portfolio_returns <= threshold
    ].mean()

    aligned_portfolio_returns = aligned["portfolio"]
    aligned_benchmark_returns = aligned["benchmark"]

    benchmark_variance = (
        aligned_benchmark_returns.var(ddof=1)
        if len(aligned) >= 2
        else float("nan")
    )

    beta = (
        aligned_portfolio_returns.cov(aligned_benchmark_returns)
        / benchmark_variance
        if benchmark_variance > 0
        else float("nan")
    )

    return {
        "volatility": float(volatility * 100),
        "drawdown": float(max_drawdown * 100),
        "sharpe": float(sharpe),
        "var": float(var_95 * 100),
        "cvar": float(cvar_95 * 100),
        "beta": float(beta),
    }

