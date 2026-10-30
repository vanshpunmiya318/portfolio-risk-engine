
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "reports" / "equity_curves.csv"
OUTPUT_PATH = ROOT / "reports" / "drawdowns.png"


def main():
    data = pd.read_csv(INPUT_PATH, parse_dates=["date"])

    fig, ax = plt.subplots(figsize=(12, 6))

    for column, label in [
        ("portfolio", "Diversified Portfolio"),
        ("benchmark", "SPY Benchmark"),
    ]:
        values = data[column]
        running_peak = values.cummax().clip(lower=100_000)
        drawdown = (values / running_peak - 1) * 100

        ax.plot(
            data["date"],
            drawdown,
            label=label,
            linewidth=2,
        )

    ax.set_title("Historical Drawdowns - Portfolio vs SPY")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (%)")
    ax.legend()
    ax.grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(OUTPUT_PATH, dpi=180)
    plt.close(fig)

    print(f"Saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
