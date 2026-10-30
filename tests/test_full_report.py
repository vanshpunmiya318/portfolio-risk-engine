import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db

from riskengine.risk.full_report import generate_full_risk_report


@pytest.fixture
def full_report(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    return generate_full_risk_report(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )


def test_full_report_structure(full_report):
    assert "risk_findings" in full_report
    assert "exposure" in full_report
    assert "markdown" in full_report


def test_full_report_security_exposures(full_report):
    exposure = full_report["exposure"]

    assert exposure["total_invested_value"] == pytest.approx(
        2852
    )

    assert exposure["largest_security"]["ticker"] == "NVDA"

    assert exposure["largest_security"]["weight"] == pytest.approx(
        1140 / 2852
    )


def test_full_report_contains_security_table(full_report):
    markdown = full_report["markdown"]

    assert "## Security Exposure" in markdown
    assert "| AAPL |" in markdown
    assert "| MSFT |" in markdown
    assert "| NVDA |" in markdown


def test_full_report_contains_sector_table(full_report):
    markdown = full_report["markdown"]

    assert "## Sector Exposure" in markdown
    assert "| Sector | Holdings | Market Value | Weight |" in markdown


def test_full_report_identifies_largest_security(full_report):
    markdown = full_report["markdown"]

    assert "**Largest security:** NVDA" in markdown
    assert "39.97%" in markdown


def test_full_report_reconciles_exposure(full_report):
    exposure = full_report["exposure"]

    security_total = sum(
        security["market_value"]
        for security in exposure["security_exposures"]
    )

    sector_total = sum(
        sector["market_value"]
        for sector in exposure["sector_exposures"]
    )

    assert security_total == pytest.approx(
        exposure["total_invested_value"]
    )

    assert sector_total == pytest.approx(
        exposure["total_invested_value"]
    )