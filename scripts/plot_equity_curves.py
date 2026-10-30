
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "reports" / "equity_curves.csv"
OUTPUT_PATH = ROOT / "reports" / "equity_curves.png"


def main():
    data = pd.read_csv(INPUT_PATH, parse_dates=["date"])

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.plot(
        data["date"],
        data["portfolio"],
        label="Diversified Portfolio",
        linewidth=2,
    )

    ax.plot(
        data["date"],
        data["benchmark"],
        label="SPY Benchmark",
        linewidth=2,
    )

    ax.set_title("Portfolio vs SPY — Historical Equity Growth")
    ax.set_xlabel("Date")
    ax.set_ylabel("Portfolio Value (USD)")
    ax.legend()
    ax.grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(OUTPUT_PATH, dpi=180)
    plt.close(fig)

    print(f"Chart saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
