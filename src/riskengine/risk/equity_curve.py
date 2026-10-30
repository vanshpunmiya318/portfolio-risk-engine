
import pandas as pd


def build_equity_curves(
    portfolio_returns: pd.Series,
    benchmark_returns: pd.Series,
    initial_capital: float = 100_000.0,
) -> pd.DataFrame:
    """Build aligned portfolio and benchmark equity curves."""

    aligned = pd.concat(
        [
            portfolio_returns.rename("portfolio_return"),
            benchmark_returns.rename("benchmark_return"),
        ],
        axis=1,
        join="inner",
    ).dropna()

    if aligned.empty:
        raise ValueError("No overlapping return dates")

    if initial_capital <= 0:
        raise ValueError("Initial capital must be positive")

    curves = pd.DataFrame(index=aligned.index)

    curves["portfolio"] = (
        initial_capital
        * (1 + aligned["portfolio_return"]).cumprod()
    )

    curves["benchmark"] = (
        initial_capital
        * (1 + aligned["benchmark_return"]).cumprod()
    )

    return curves
