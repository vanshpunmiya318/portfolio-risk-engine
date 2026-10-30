
import numpy as np
import pandas as pd


TRADING_DAYS = 252


def calculate_performance_metrics(
    returns: pd.Series,
    initial_capital: float = 100_000.0,
    risk_free_rate: float = 0.0,
) -> dict:
    """
    Calculate investment performance from daily simple returns.

    Returns must represent consecutive trading-day observations.
    The equity curve starts at initial_capital.
    """

    if initial_capital <= 0 or not np.isfinite(initial_capital):
        raise ValueError("Initial capital must be positive and finite")

    if not np.isfinite(risk_free_rate):
        raise ValueError("Risk-free rate must be finite")

    returns = pd.Series(returns, dtype=float).sort_index()

    if len(returns) < 2:
        raise ValueError("At least two daily returns are required")

    if returns.index.has_duplicates:
        raise ValueError("Return dates must be unique")

    if not np.isfinite(returns.to_numpy()).all():
        raise ValueError("Returns must be finite")

    if (returns < -1).any():
        raise ValueError("A simple return cannot be below -100%")

    equity_curve = initial_capital * (1 + returns).cumprod()

    cumulative_return = equity_curve.iloc[-1] / initial_capital - 1

    annualized_return = (
        (1 + cumulative_return) ** (TRADING_DAYS / len(returns)) - 1
    )

    annualized_volatility = (
        returns.std(ddof=1) * np.sqrt(TRADING_DAYS)
    )

    daily_risk_free_rate = (
        (1 + risk_free_rate) ** (1 / TRADING_DAYS) - 1
    )

    excess_returns = returns - daily_risk_free_rate

    sharpe_ratio = (
        excess_returns.mean() / returns.std(ddof=1)
        * np.sqrt(TRADING_DAYS)
        if returns.std(ddof=1) > 0
        else None
    )

    equity_with_baseline = pd.concat(
        [
            pd.Series([initial_capital], index=["baseline"]),
            equity_curve.reset_index(drop=True),
        ],
        ignore_index=True,
    )

    running_peak = equity_with_baseline.cummax()
    drawdowns = equity_with_baseline / running_peak - 1
    max_drawdown = -float(drawdowns.min())

    return {
        "initial_capital": initial_capital,
        "ending_value": float(equity_curve.iloc[-1]),
        "cumulative_return": float(cumulative_return),
        "annualized_return": float(annualized_return),
        "annualized_volatility": float(annualized_volatility),
        "max_drawdown": max_drawdown,
        "sharpe_ratio": (
            float(sharpe_ratio)
            if sharpe_ratio is not None
            else None
        ),
        "observations": len(returns),
        "equity_curve": equity_curve,
    }


def compare_portfolio_to_benchmark(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    initial_capital: float = 100_000.0,
    risk_free_rate: float = 0.0,
) -> dict:
    """
    Compare portfolio and benchmark over identical return dates.
    """

    portfolio = pd.Series(
        portfolio_returns, dtype=float
    ).rename("portfolio")

    benchmark = pd.Series(
        benchmark_returns, dtype=float
    ).rename("benchmark")

    aligned = pd.concat(
        [portfolio, benchmark],
        axis=1,
        join="inner",
    ).dropna()

    if len(aligned) < 2:
        raise ValueError(
            "Insufficient overlapping portfolio and benchmark returns"
        )

    portfolio_metrics = calculate_performance_metrics(
        aligned["portfolio"],
        initial_capital,
        risk_free_rate,
    )

    benchmark_metrics = calculate_performance_metrics(
        aligned["benchmark"],
        initial_capital,
        risk_free_rate,
    )

    return {
        "portfolio": portfolio_metrics,
        "benchmark": benchmark_metrics,
        "excess_cumulative_return": (
            portfolio_metrics["cumulative_return"]
            - benchmark_metrics["cumulative_return"]
        ),
        "excess_annualized_return": (
            portfolio_metrics["annualized_return"]
            - benchmark_metrics["annualized_return"]
        ),
        "aligned_observations": len(aligned),
        "start_date": str(aligned.index.min().date()),
        "end_date": str(aligned.index.max().date()),
    }
