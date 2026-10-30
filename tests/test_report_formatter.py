import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db

from riskengine.risk.findings import generate_risk_findings
from riskengine.risk.report_formatter import format_risk_report


@pytest.fixture
def formatted_report(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    findings = generate_risk_findings(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )

    return format_risk_report(findings)


def test_report_has_main_sections(formatted_report):
    assert "# Portfolio Risk Intelligence Report" in formatted_report
    assert "## Executive Summary" in formatted_report
    assert "## Risk Metrics" in formatted_report
    assert "## Concentration" in formatted_report
    assert "## Risk Findings" in formatted_report
    assert "## Methodology and Limitations" in formatted_report


def test_report_has_risk_metrics(formatted_report):
    assert "Annualized volatility" in formatted_report
    assert "Maximum drawdown" in formatted_report
    assert "Historical VaR" in formatted_report
    assert "Historical CVaR" in formatted_report
    assert "Beta vs SPY" in formatted_report


def test_report_has_concentration(formatted_report):
    assert "Invested holdings: 3" in formatted_report
    assert "HHI:" in formatted_report
    assert "Effective equally weighted holdings:" in formatted_report
    assert "Largest holding weight:" in formatted_report
    assert "Concentration weights exclude cash" in formatted_report


def test_report_has_five_findings(formatted_report):
    assert formatted_report.count("**Further investigation:**") == 5


def test_report_discloses_limitations(formatted_report):
    assert "external deposits and withdrawals" in formatted_report
    assert "raw closing prices" in formatted_report
    assert "excluding cash" in formatted_report
    assert "historical" in formatted_report.lower()