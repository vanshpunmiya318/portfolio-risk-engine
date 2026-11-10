import pandas as pd

from riskengine.risk.performance import calculate_performance_metrics


def calculate_live_risk_metrics(frame, portfolio_returns=None):
    prices = frame[["portfolio", "benchmark"]].astype(float)

    if portfolio_returns is None:
        portfolio_returns = pd.Series(
            prices["portfolio"].to_numpy(),
            index=pd.DatetimeIndex(frame["date"]),
            dtype=float,
        ).pct_change().dropna()
    else:
        portfolio_returns = pd.Series(
            portfolio_returns, dtype=float
        ).dropna()

    benchmark_returns = (
        pd.Series(
            prices["benchmark"].to_numpy(),
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

    if len(portfolio_returns) < 2:
      raise ValueError("Insufficient portfolio data for risk calculations")

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

