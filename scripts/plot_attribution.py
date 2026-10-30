
from pathlib import Path

import matplotlib.pyplot as plt

from riskengine.db.connection import get_connection
from riskengine.risk.attribution import calculate_performance_attribution

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "historical_portfolio.db"
OUTPUT_PATH = ROOT / "reports" / "performance_attribution.png"


def main():
    with get_connection(DB_PATH) as connection:
        data = calculate_performance_attribution(
            connection, 1, "2026-10-23"
        )

    data = data.sort_values("gain_usd")

    fig, ax = plt.subplots(figsize=(11, 6))

    colors = [
        "green" if value >= 0 else "red"
        for value in data["gain_usd"]
    ]

    ax.barh(data["ticker"], data["gain_usd"], color=colors)

    ax.set_title("ETF Contribution to Portfolio Profit")
    ax.set_xlabel("Gain / Loss (USD)")
    ax.grid(axis="x", alpha=0.25)

    fig.tight_layout()
    fig.savefig(OUTPUT_PATH, dpi=180)
    plt.close(fig)

    print(f"Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
