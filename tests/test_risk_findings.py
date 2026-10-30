import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db

from riskengine.risk.findings import generate_risk_findings


def test_findings_structure(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    result = generate_risk_findings(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )

    assert result["portfolio_id"] == portfolio_id
    assert result["benchmark"] == "SPY"
    assert result["as_of_date"] == "2026-01-09"

    assert len(result["findings"]) == 5

    categories = {
        finding["category"]
        for finding in result["findings"]
    }

    assert categories == {
        "concentration",
        "market_sensitivity",
        "volatility",
        "drawdown",
        "tail_risk",
    }


def test_findings_concentration(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    result = generate_risk_findings(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )

    concentration = result["concentration"]

    # End-date holdings:
    # AAPL: 6 * 112 = 672
    # MSFT: 5 * 208 = 1040
    # NVDA: 20 * 57 = 1140
    total = 672 + 1040 + 1140

    weights = [
        672 / total,
        1040 / total,
        1140 / total,
    ]

    expected_hhi = sum(weight ** 2 for weight in weights)

    assert concentration["holdings_count"] == 3

    assert concentration["hhi"] == pytest.approx(
        expected_hhi
    )

    assert concentration["effective_holdings"] == pytest.approx(
        1 / expected_hhi
    )

    assert concentration["largest_holding_weight"] == pytest.approx(
        1140 / total
    )


def test_findings_risk_metrics(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    result = generate_risk_findings(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )

    risk = result["risk_metrics"]

    assert risk["observations"] == 4
    assert isinstance(risk["beta"], float)
    assert risk["maximum_drawdown"] == pytest.approx(0.0)

    for finding in result["findings"]:
        assert isinstance(finding["headline"], str)
        assert finding["interpretation"]
        assert finding["investigation"]


def test_findings_concentration_is_holdings_only(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    result = generate_risk_findings(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )

    # Invested holdings total $2,852.
    # Total portfolio equity is $20,292.
    # Cash must not dilute invested-holdings HHI.
    assert result["concentration"]["hhi"] > 0.33
    assert result["concentration"]["hhi"] < 0.40 