import pandas as pd
import pytest

from test_golden_portfolio import golden_db

from riskengine.db.instruments import add_instrument
from riskengine.db.prices import add_price
from riskengine.risk.benchmark import get_benchmark_price_returns
from riskengine.risk.portfolio_report import get_portfolio_risk_report


@pytest.fixture
def golden_spy_db(golden_db):
    connection, instruments, portfolio_id = golden_db

    spy_id = add_instrument(
        connection,
        ticker="SPY",
        name="SPDR S&P 500 ETF",
        asset_type="etf",
        currency="USD",
        sector="Broad Market",
    )

    connection.execute(
        "UPDATE instruments SET primary_source = 'test' WHERE id = ?",
        (spy_id,),
    )

    dates = pd.date_range("2026-01-05", "2026-01-09")
    closes = [500.0, 505.0, 500.0, 510.0, 515.0]

    for date, close in zip(dates, closes):
        add_price(
            connection,
            spy_id,
            date.strftime("%Y-%m-%d"),
            close,
            close,
            close,
            close,
            close,
            1000000,
            "test",
            "2026-01-10T00:00:00",
        )

    connection.commit()

    return connection, portfolio_id


def test_benchmark_price_returns(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    returns = get_benchmark_price_returns(
        connection,
        "SPY",
        "2026-01-05",
        "2026-01-09",
    )

    expected = [
        505 / 500 - 1,
        500 / 505 - 1,
        510 / 500 - 1,
        515 / 510 - 1,
    ]

    assert returns.to_numpy() == pytest.approx(expected)
    assert len(returns) == 4


def test_benchmark_missing_prices(golden_db):
    connection, instruments, portfolio_id = golden_db

    with pytest.raises(ValueError, match="No prices"):
        get_benchmark_price_returns(
            connection,
            "MISSING",
            "2026-01-05",
            "2026-01-09",
        )


def test_portfolio_risk_report_with_spy(golden_spy_db):
    connection, portfolio_id = golden_spy_db

    result = get_portfolio_risk_report(
        connection,
        portfolio_id,
        "2026-01-05",
        "2026-01-09",
    )

    assert result["benchmark"] == "SPY"
    assert result["benchmark_return_basis"] == "raw_close_price_return"
    assert result["observations"] == 4
    assert isinstance(result["beta"], float)


def test_portfolio_risk_report_missing_benchmark(golden_db):
    connection, instruments, portfolio_id = golden_db

    with pytest.raises(ValueError, match="No prices"):
        get_portfolio_risk_report(
            connection,
            portfolio_id,
            "2026-01-05",
            "2026-01-09",
        )




def test_risk_report_rejects_missing_portfolio_price_date(
    golden_spy_db,
):
    connection, portfolio_id = golden_spy_db

    # Remove every portfolio holding's price on Jan 7.
    # SPY retains its Jan 7 observation.
    connection.execute(
        """
        DELETE FROM prices
        WHERE date = '2026-01-07'
          AND instrument_id IN (
              SELECT DISTINCT instrument_id
              FROM transactions
              WHERE portfolio_id = ?
          )
        """,
        (portfolio_id,),
    )
    connection.commit()

    with pytest.raises(
        ValueError,
        match="Missing portfolio price dates",
    ):
        get_portfolio_risk_report(
            connection,
            portfolio_id,
            "2026-01-05",
            "2026-01-09",
        )


def test_risk_report_rejects_missing_benchmark_start(
    golden_spy_db,
):
    connection, portfolio_id = golden_spy_db

    with pytest.raises(
        ValueError,
        match="Benchmark start date",
    ):
        get_portfolio_risk_report(
            connection,
            portfolio_id,
            "2026-01-04",
            "2026-01-09",
        )


def test_risk_report_rejects_missing_benchmark_end(
    golden_spy_db,
):
    connection, portfolio_id = golden_spy_db

    with pytest.raises(
        ValueError,
        match="Benchmark end date",
    ):
        get_portfolio_risk_report(
            connection,
            portfolio_id,
            "2026-01-05",
            "2026-01-10",
        )



def test_risk_report_rejects_missing_individual_holding_price(
    golden_spy_db,
):
    connection, portfolio_id = golden_spy_db

    connection.execute(
        """
        DELETE FROM prices
        WHERE date = '2026-01-07'
          AND instrument_id = (
              SELECT id
              FROM instruments
              WHERE ticker = 'AAPL'
          )
        """
    )
    connection.commit()

    with pytest.raises(
        ValueError,
        match="Incomplete portfolio price coverage",
    ):
        get_portfolio_risk_report(
            connection,
            portfolio_id,
            "2026-01-05",
            "2026-01-09",
        )