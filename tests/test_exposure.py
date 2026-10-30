import pytest

from test_golden_portfolio import golden_db

from riskengine.risk.exposure import get_portfolio_exposure


def test_security_exposure_values(golden_db):
    connection, instruments, portfolio_id = golden_db

    result = get_portfolio_exposure(
        connection,
        portfolio_id,
        "2026-01-09",
    )

    securities = {
        item["ticker"]: item
        for item in result["security_exposures"]
    }

    assert set(securities) == {"AAPL", "MSFT", "NVDA"}

    assert securities["AAPL"]["market_value"] == pytest.approx(672)
    assert securities["MSFT"]["market_value"] == pytest.approx(1040)
    assert securities["NVDA"]["market_value"] == pytest.approx(1140)

    assert result["total_invested_value"] == pytest.approx(2852)


def test_security_weights_sum_to_one(golden_db):
    connection, instruments, portfolio_id = golden_db

    result = get_portfolio_exposure(
        connection,
        portfolio_id,
        "2026-01-09",
    )

    assert sum(
        item["weight"]
        for item in result["security_exposures"]
    ) == pytest.approx(1.0)

    assert result["largest_security"]["ticker"] == "NVDA"
    assert result["largest_security"]["weight"] == pytest.approx(
        1140 / 2852
    )


def test_sector_exposures_reconcile(golden_db):
    connection, instruments, portfolio_id = golden_db

    result = get_portfolio_exposure(
        connection,
        portfolio_id,
        "2026-01-09",
    )

    assert sum(
        item["market_value"]
        for item in result["sector_exposures"]
    ) == pytest.approx(2852)

    assert sum(
        item["weight"]
        for item in result["sector_exposures"]
    ) == pytest.approx(1.0)


def test_missing_sector_is_unclassified(golden_db):
    connection, instruments, portfolio_id = golden_db

    connection.execute(
        """
        UPDATE instruments
        SET sector = NULL
        WHERE ticker = 'NVDA'
        """
    )
    connection.commit()

    result = get_portfolio_exposure(
        connection,
        portfolio_id,
        "2026-01-09",
    )

    nvda = next(
        item
        for item in result["security_exposures"]
        if item["ticker"] == "NVDA"
    )

    assert nvda["sector"] == "Unclassified"


def test_nvda_split_reflected_in_quantity(golden_db):
    connection, instruments, portfolio_id = golden_db

    result = get_portfolio_exposure(
        connection,
        portfolio_id,
        "2026-01-09",
    )

    nvda = next(
        item
        for item in result["security_exposures"]
        if item["ticker"] == "NVDA"
    )

    assert nvda["quantity"] == pytest.approx(20)
    assert nvda["market_value"] == pytest.approx(1140)