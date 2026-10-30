
from pathlib import Path

import pandas as pd

from riskengine.db.connection import get_connection
from riskengine.risk.benchmark import get_benchmark_price_returns
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns
from riskengine.risk.performance import compare_portfolio_to_benchmark


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "historical_portfolio.db"
REPORT_PATH = ROOT / "reports" / "historical_performance_comparison.md"

PORTFOLIO_ID = 1
START_DATE = "2024-01-02"
END_DATE = "2026-10-23"
INITIAL_CAPITAL = 100_000.0


def format_percentage(value):
    return f"{value * 100:.2f}%"


def format_money(value):
    return f"${value:,.2f}"


def format_sharpe(value):
    return "N/A" if value is None else f"{value:.2f}"


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    with get_connection(DB_PATH) as connection:
        benchmark_returns = get_benchmark_price_returns(
            connection,
            "SPY",
            START_DATE,
            END_DATE,
        )

        valuation_dates = pd.DatetimeIndex(
            [pd.Timestamp(START_DATE)]
        ).append(benchmark_returns.index)

        portfolio_returns = get_portfolio_daily_returns(
            connection,
            PORTFOLIO_ID,
            valuation_dates,
        )

        comparison = compare_portfolio_to_benchmark(
            portfolio_returns,
            benchmark_returns,
            initial_capital=INITIAL_CAPITAL,
            risk_free_rate=0.0,
        )

    portfolio = comparison["portfolio"]
    benchmark = comparison["benchmark"]

    rows = [
        ("Ending value", format_money),
        ("Cumulative return", format_percentage),
        ("Annualized return", format_percentage),
        ("Annualized volatility", format_percentage),
        ("Maximum drawdown", format_percentage),
        ("Sharpe ratio", format_sharpe),
    ]

    metric_keys = [
        "ending_value",
        "cumulative_return",
        "annualized_return",
        "annualized_volatility",
        "max_drawdown",
        "sharpe_ratio",
    ]

    lines = [
        "# Historical Portfolio vs SPY",
        "",
        f"**Period:** {comparison['start_date']} to {comparison['end_date']}",
        f"**Initial capital:** {format_money(INITIAL_CAPITAL)}",
        f"**Aligned daily returns:** {comparison['aligned_observations']}",
        "",
        "| Metric | Diversified Portfolio | SPY Benchmark |",
        "|---|---:|---:|",
    ]

    for (label, formatter), key in zip(rows, metric_keys):
        lines.append(
            f"| {label} | "
            f"{formatter(portfolio[key])} | "
            f"{formatter(benchmark[key])} |"
        )

    lines.extend(
        [
            "",
            "## Relative Performance",
            "",
            f"- Excess cumulative return: "
            f"{format_percentage(comparison['excess_cumulative_return'])}",
            f"- Excess annualized return: "
            f"{format_percentage(comparison['excess_annualized_return'])}",
            "",
            "## Methodology",
            "",
            "- Buy-and-hold diversified ETF portfolio.",
            "- Identical $100,000 starting capital.",
            "- Portfolio returns account for external cash flows.",
            "- Benchmark returns use raw closing prices.",
            "- Performance uses aligned daily return observations.",
            "- Annualization assumes 252 trading days per year.",
            "- Sharpe ratio assumes a 0% annual risk-free rate.",
            "- Dividends and distributions are not included in the "
            "benchmark's raw-price return series.",
            "",
        ]
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    print("\n".join(lines))
    print(f"\nReport saved to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
