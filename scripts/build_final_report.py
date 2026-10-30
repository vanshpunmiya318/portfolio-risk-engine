
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"

RISK_REPORT = REPORTS / "historical_portfolio_risk_report.md"
COMPARISON_REPORT = REPORTS / "historical_performance_comparison.md"
OUTPUT_PATH = REPORTS / "portfolio_intelligence_report.md"

CHARTS = [
    ("Portfolio Growth vs SPY", "equity_curves.png"),
    ("Historical Drawdowns", "drawdowns.png"),
    ("Allocation Drift", "allocation_drift.png"),
    ("ETF Performance Attribution", "performance_attribution.png"),
]

def demote_headings(markdown):
    lines = markdown.splitlines()
    return "\n".join(
        "#" + line if line.startswith("#") else line
        for line in lines
    )

def main():
    required_files = [
        RISK_REPORT,
        COMPARISON_REPORT,
        *[REPORTS / filename for _, filename in CHARTS],
    ]

    missing = [path.name for path in required_files if not path.is_file()]

    if missing:
        raise FileNotFoundError(
            f"Missing report files: {', '.join(missing)}"
        )

    risk_text = RISK_REPORT.read_text(encoding="utf-8")
    comparison_text = COMPARISON_REPORT.read_text(encoding="utf-8")

    risk_text = risk_text.replace(
    "## Sector Exposure",
    "## Asset Class & Investment Style Exposure"
     ).replace(
    "| Sector |",
    "| Exposure Category |"
     ).replace(
    "**Largest sector:**",
    "**Largest exposure category:**"
     )

    sections = [
        "# Portfolio Risk Intelligence Engine",
        "",
        "**Historical ETF Portfolio Analysis | January 2024 – October 2026**",
        "",
        "## Performance and Benchmark Comparison",
        "",
        demote_headings(comparison_text).strip(),
        "",
        "## Portfolio Visualizations",
        "",
    ]

    for title, filename in CHARTS:
        sections.extend([
            f"### {title}",
            "",
            f"![{title}]({filename})",
            "",
        ])

    sections.extend([
        "## Detailed Portfolio Risk Analysis",
        "",
        demote_headings(risk_text).strip(),
        "",
    ])

    OUTPUT_PATH.write_text(
        "\n".join(sections),
        encoding="utf-8",
    )

    print(f"Final report saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
