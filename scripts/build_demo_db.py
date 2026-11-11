
"""Build a reproducible, synthetic portfolio database for public demonstrations."""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema
from riskengine.db.portfolios import add_portfolio
from riskengine.db.transactions import (
    add_cash_flow,
    add_transaction,
    validate_cash_balances,
    validate_transactions,
)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "demo_portfolio.db"
REAL_DB_PATH = ROOT / "data" / "historical_portfolio.db"

START_DATE = "2024-01-02"
END_DATE = "2026-11-06"
INITIAL_CAPITAL = 100_000.0
SEED = 42

# Names and identifiers are familiar; all prices below are simulated.
ASSETS = {
    "SPY": ("Simulated US Large-Cap ETF", "Equity", 0.30, 480.0, 0.09, 0.16),
    "QQQ": ("Simulated US Growth ETF", "Equity", 0.20, 410.0, 0.13, 0.23),
    "IWM": ("Simulated US Small-Cap ETF", "Equity", 0.15, 195.0, 0.07, 0.22),
    "GLD": ("Simulated Gold ETF", "Commodity ETF", 0.15, 190.0, 0.06, 0.14),
    "TLT": ("Simulated Treasury Bond ETF", "Bond ETF", 0.10, 95.0, 0.03, 0.12),
    "XLE": ("Simulated Energy ETF", "Equity", 0.10, 85.0, 0.06, 0.24),
}


def build():
    if DB_PATH.resolve() == REAL_DB_PATH.resolve():
        raise RuntimeError("Demo database must not overwrite the real database.")

    if DB_PATH.exists():
        raise FileExistsError(
            f"{DB_PATH} already exists. Refusing to overwrite it."
        )

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(START_DATE, END_DATE)
    rng = np.random.default_rng(SEED)
    fetched_at = datetime.now(timezone.utc).isoformat()

    # Shared market factor creates correlation across simulated assets.
    market_shocks = rng.standard_normal(len(dates))
    rows = []
    price_lookup = {}

    for ticker, (name, asset_type, weight, initial_price, drift, volatility) in ASSETS.items():
        instrument_index = len(price_lookup) + 1
        independent_shocks = rng.standard_normal(len(dates))

        correlation = {
            "SPY": 0.95,
            "QQQ": 0.88,
            "IWM": 0.82,
            "GLD": 0.15,
            "TLT": -0.25,
            "XLE": 0.65,
        }[ticker]

        shocks = (
            correlation * market_shocks
            + np.sqrt(1 - correlation**2) * independent_shocks
        )

        daily_returns = (
            (drift - 0.5 * volatility**2) / 252
            + volatility / np.sqrt(252) * shocks
        )

        closes = initial_price * np.exp(np.cumsum(daily_returns))
        closes[0] = initial_price

        price_lookup[ticker] = dict(zip(dates.strftime("%Y-%m-%d"), closes))

        for day, close in zip(dates, closes):
            day_str = day.strftime("%Y-%m-%d")
            close = float(close)

            rows.append(
                (
                    instrument_index,
                    day_str,
                    close,
                    close * 1.005,
                    close * 0.995,
                    close,
                    close,
                    1_000_000,
                    "synthetic_demo",
                    fetched_at,
                )
            )

    # Create schema and insert the demo data.
    # Remove an incomplete file only if this run created it.
    try:
        create_schema(DB_PATH)

        with get_connection(DB_PATH) as connection:
            with connection:
                for instrument_id, (ticker, asset) in enumerate(ASSETS.items(), start=1):
                    name, asset_type, _, _, _, _ = asset
                    connection.execute(
                        """
                        INSERT INTO instruments
                        (id, ticker, name, asset_type, currency, sector, primary_source)
                        VALUES (?, ?, ?, ?, 'USD', ?, 'test')
                        """,
                        (instrument_id, ticker, name, asset_type, name),
                    )

                connection.executemany(
                    """
                    INSERT INTO prices
                    (instrument_id, date, open, high, low, close,
                     adjusted_close, volume, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

                portfolio_id = add_portfolio(
                    connection, "Synthetic Diversified ETF Portfolio", "USD"
                )

                add_cash_flow(
                    connection,
                    portfolio_id,
                    START_DATE,
                    "DEPOSIT",
                    INITIAL_CAPITAL,
                    "USD",
                    "synthetic_demo",
                )

                for instrument_id, (ticker, asset) in enumerate(ASSETS.items(), start=1):
                    weight = asset[2]
                    price = price_lookup[ticker][START_DATE]
                    quantity = int(INITIAL_CAPITAL * weight // price)

                    add_transaction(
                        connection,
                        portfolio_id,
                        instrument_id,
                        START_DATE,
                        "BUY",
                        quantity,
                        float(price),
                        "USD",
                        "synthetic_demo",
                    )

                # Sample activity for the Transactions and Performance pages.
                add_cash_flow(
                    connection,
                    portfolio_id,
                    "2025-01-02",
                    "DEPOSIT",
                    10_000.0,
                    "USD",
                    "synthetic_demo",
                )

                spy_price = float(price_lookup["SPY"]["2025-02-03"])
                add_transaction(
                    connection,
                    portfolio_id,
                    1,
                    "2025-02-03",
                    "BUY",
                    5,
                    spy_price,
                    "USD",
                    "synthetic_demo",
                )

                qqq_price = float(price_lookup["QQQ"]["2025-06-02"])
                add_transaction(
                    connection,
                    portfolio_id,
                    2,
                    "2025-06-02",
                    "SELL",
                    5,
                    qqq_price,
                    "USD",
                    "synthetic_demo",
                )

                validate_transactions(connection, portfolio_id)
                validate_cash_balances(connection, portfolio_id)

        print(f"Demo database created: {DB_PATH}")
        print(f"Simulated price records: {len(rows):,}")
        print("Portfolio transactions and cash balances validated.")

    except Exception:
        if DB_PATH.exists():
            DB_PATH.unlink()
        raise


if __name__ == "__main__":
    build()
