
from pathlib import Path

import pandas as pd

from riskengine.db.connection import get_connection
from riskengine.risk.benchmark import get_benchmark_price_returns
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns
from riskengine.risk.equity_curve import build_equity_curves


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "historical_portfolio.db"
OUTPUT_PATH = ROOT / "reports" / "equity_curves.csv"

START_DATE = "2024-01-02"
END_DATE = "2026-10-23"
PORTFOLIO_ID = 1


def main():
    with get_connection(DB_PATH) as connection:
        benchmark_returns = get_benchmark_price_returns(
            connection, "SPY", START_DATE, END_DATE
        )

        valuation_dates = pd.DatetimeIndex(
            [pd.Timestamp(START_DATE)]
        ).append(benchmark_returns.index)

        portfolio_returns = get_portfolio_daily_returns(
            connection, PORTFOLIO_ID, valuation_dates
        )

        curves = build_equity_curves(
            portfolio_returns,
            benchmark_returns,
            initial_capital=100_000,
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    curves.to_csv(OUTPUT_PATH, index_label="date")

    print(f"Exported {len(curves)} observations")
    print(f"Portfolio ending value: ${curves['portfolio'].iloc[-1]:,.2f}")
    print(f"SPY ending value: ${curves['benchmark'].iloc[-1]:,.2f}")
    print(f"Saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
