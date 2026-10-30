
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "historical_portfolio.db"
OUTPUT_PATH = ROOT / "reports" / "allocation_drift.png"

INITIAL_WEIGHTS = {
    "SPY": 30,
    "QQQ": 20,
    "IWM": 15,
    "GLD": 15,
    "TLT": 10,
    "XLE": 10,
}


def main():
    import sqlite3

    with sqlite3.connect(DB_PATH) as connection:
        data = pd.read_sql_query(
            """
            SELECT i.ticker, SUM(t.quantity * p.close) AS market_value
            FROM transactions t
            JOIN instruments i ON i.id = t.instrument_id
            JOIN prices p ON p.instrument_id = i.id
            WHERE t.portfolio_id = 1
              AND t.transaction_type = 'BUY'
              AND p.date = '2026-10-23'
            GROUP BY i.ticker
            """,
            connection,
        )

    data["current_weight"] = (
        data["market_value"] / data["market_value"].sum() * 100
    )

    data["initial_weight"] = data["ticker"].map(INITIAL_WEIGHTS)

    x = np.arange(len(data))
    width = 0.36

    fig, ax = plt.subplots(figsize=(11, 6))

    ax.bar(
        x - width / 2,
        data["initial_weight"],
        width,
        label="Target Allocation",
    )
    ax.bar(
        x + width / 2,
        data["current_weight"],
        width,
        label="Current Allocation",
    )

    ax.set_xticks(x, data["ticker"])
    ax.set_ylabel("Weight (%)")
    ax.set_title("Portfolio Allocation Drift")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)

    fig.tight_layout()
    fig.savefig(OUTPUT_PATH, dpi=180)
    plt.close(fig)

    print(f"Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
