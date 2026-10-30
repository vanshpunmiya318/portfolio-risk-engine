from pathlib import Path

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db

from riskengine.risk.full_report import generate_full_risk_report


def test_export_golden_report(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    result = generate_full_risk_report(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )

    output_path = (
        Path(__file__).resolve().parents[1]
        / "reports"
        / "golden_portfolio_risk_report.md"
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        result["markdown"],
        encoding="utf-8",
    )

    assert output_path.exists()
    assert "## Security Exposure" in output_path.read_text(
        encoding="utf-8"
    )

    print(f"\nReport generated: {output_path}")