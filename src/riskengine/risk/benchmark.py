import numpy as np
import pandas as pd

from riskengine.db.prices import get_prices


def get_benchmark_price_returns(
    connection,
    ticker: str,
    start_date: str,
    end_date: str,
) -> pd.Series:
    """
    Calculate benchmark returns from raw closing prices.

    The first price observation provides the baseline.
    Returns are indexed by their ending dates.
    """

    prices = get_prices(
        connection,
        ticker,
        start_date,
        end_date,
    )

    if prices.empty:
        raise ValueError(f"No prices found for {ticker}")

    if "close" not in prices.columns:
        raise ValueError("Benchmark prices must contain a close column")

    closes = prices["close"].astype(float)

    if closes.isna().any() or not np.isfinite(closes.to_numpy()).all():
        raise ValueError("Benchmark closing prices must be finite")

    if (closes <= 0).any():
        raise ValueError("Benchmark closing prices must be positive")

    if closes.index.has_duplicates:
        raise ValueError("Benchmark price dates must be unique")

    closes = closes.sort_index()

    if len(closes) < 3:
        raise ValueError(
            "At least three benchmark prices are required"
        )

    returns = closes.pct_change().dropna()
    returns.index = pd.to_datetime(returns.index)
    returns.name = f"{ticker}_price_return"

    return returns