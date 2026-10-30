import numpy as np
import pandas as pd


def annualized_volatility(
    returns: pd.Series,
    trading_days: int = 252,
) -> float:
    """Annualized sample standard deviation of daily returns."""
    if trading_days <= 0:
        raise ValueError("trading_days must be positive")

    clean = returns.dropna().astype(float)

    if len(clean) < 2:
        raise ValueError("At least two returns are required")

    return float(clean.std(ddof=1) * np.sqrt(trading_days))


def maximum_drawdown(returns: pd.Series) -> float:
    """Maximum peak-to-trough loss, returned as a negative decimal."""
    clean = returns.dropna().astype(float)

    if clean.empty:
        raise ValueError("At least one return is required")

    wealth = pd.concat([
        pd.Series([1.0]),
        (1.0 + clean).cumprod().reset_index(drop=True),
    ], ignore_index=True)

    drawdowns = wealth / wealth.cummax() - 1.0

    return float(drawdowns.min())




def historical_var(
    returns: pd.Series,
    confidence: float = 0.95,
) -> float:
    """Historical Value at Risk, expressed as a positive loss."""
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")

    clean = returns.dropna().astype(float)

    if clean.empty:
        raise ValueError("At least one return is required")

    if not np.isfinite(clean.to_numpy()).all():
        raise ValueError("Returns must be finite")

    threshold = float(np.quantile(clean, 1 - confidence))

    return -threshold


def historical_cvar(
    returns: pd.Series,
    confidence: float = 0.95,
) -> float:
    """Historical Expected Shortfall using the worst tail fraction."""
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")

    clean = returns.dropna().astype(float)

    if clean.empty:
        raise ValueError("At least one return is required")

    values = clean.to_numpy()

    if not np.isfinite(values).all():
        raise ValueError("Returns must be finite")

    sorted_returns = np.sort(values)

    tail_size = (1 - confidence) * len(sorted_returns)
    full_observations = int(np.floor(tail_size))
    fractional_observation = tail_size - full_observations

    if tail_size < 1:
        return -float(sorted_returns[0])

    tail_sum = sorted_returns[:full_observations].sum()

    if fractional_observation > 0:
        tail_sum += (
            fractional_observation
            * sorted_returns[full_observations]
        )

    return -float(tail_sum / tail_size)




def portfolio_hhi(weights: pd.Series) -> float:
    """
    Calculate portfolio concentration using HHI.

    Weights must be non-negative and sum to 1.
    Cash can be included as a separate allocation.
    """
    clean = weights.astype(float)

    if clean.empty:
        raise ValueError("At least one weight is required")

    if not np.isfinite(clean.to_numpy()).all():
        raise ValueError("Weights must be finite")

    if (clean < 0).any():
        raise ValueError("Weights cannot be negative")

    if not np.isclose(clean.sum(), 1.0, atol=1e-10):
        raise ValueError("Weights must sum to 1")

    return float((clean ** 2).sum())




def portfolio_beta(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
) -> float:
    """Calculate portfolio beta using date-aligned returns."""

    aligned = pd.concat(
        [
            portfolio_returns.rename("portfolio"),
            benchmark_returns.rename("benchmark"),
        ],
        axis=1,
        join="inner",
    )

    aligned = aligned.dropna().astype(float)

    if len(aligned) < 2:
        raise ValueError(
            "At least two matching return observations are required"
        )

    if not np.isfinite(aligned.to_numpy()).all():
        raise ValueError("Returns must be finite")

    benchmark_variance = aligned["benchmark"].var(ddof=1)

    if np.isclose(benchmark_variance, 0.0, atol=1e-15):
        raise ValueError("Benchmark variance must be positive")

    covariance = aligned["portfolio"].cov(
        aligned["benchmark"]
    )

    return float(covariance / benchmark_variance)