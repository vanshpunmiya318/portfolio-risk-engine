import pandas as pd
import pytest
from test_golden_portfolio import (
    golden_db,
    cash_flow_golden_db,
)
from riskengine.risk.portfolio_returns import (
    get_portfolio_daily_returns,
)


def test_daily_returns_without_intermediate_flows(golden_db):
    connection, instruments, portfolio_id = golden_db

    dates = pd.date_range("2026-01-05", "2026-01-09")

    actual = get_portfolio_daily_returns(
        connection,
        portfolio_id,
        dates,
    )

    expected = [
        20140 / 20000 - 1,
        20205 / 20140 - 1,
        20238 / 20205 - 1,
        20292 / 20238 - 1,
    ]

    assert actual.to_numpy() == pytest.approx(
        expected,
        abs=1e-12,
    )

    assert list(actual.index) == list(dates[1:])


def test_daily_returns_with_intermediate_deposit(cash_flow_golden_db):
    connection, portfolio_id = cash_flow_golden_db
    dates = pd.date_range("2026-01-05", "2026-01-08")

    actual = get_portfolio_daily_returns(
        connection,
        portfolio_id,
        dates,
    )

    # Jan 6: $10,000 -> $10,500 = +5%
    # Jan 7: $5,250 deposit, no market movement = 0%
    # Jan 8: $15,750 -> $16,065 = +2%

    expected = [0.05, 0.0, 0.02]

    assert actual.to_numpy() == pytest.approx(
        expected,
        abs=1e-12,
    )

    compounded_return = (1 + actual).prod() - 1

    assert compounded_return == pytest.approx(
        0.071,
        abs=1e-12,
    )


def test_daily_returns_sorts_dates(golden_db):
    connection, instruments, portfolio_id = golden_db

    dates = [
        "2026-01-09",
        "2026-01-05",
        "2026-01-07",
    ]

    actual = get_portfolio_daily_returns(
        connection,
        portfolio_id,
        dates,
    )

    assert list(actual.index) == [
        pd.Timestamp("2026-01-07"),
        pd.Timestamp("2026-01-09"),
    ]


def test_daily_returns_rejects_duplicate_dates(golden_db):
    connection, instruments, portfolio_id = golden_db

    with pytest.raises(ValueError, match="unique"):
        get_portfolio_daily_returns(
            connection,
            portfolio_id,
            ["2026-01-05", "2026-01-05", "2026-01-06"],
        )


def test_daily_returns_rejects_insufficient_dates(golden_db):
    connection, instruments, portfolio_id = golden_db

    with pytest.raises(ValueError, match="At least two"):
        get_portfolio_daily_returns(
            connection,
            portfolio_id,
            ["2026-01-05"],
        )


def test_daily_returns_rejects_negative_max_age(golden_db):
    connection, instruments, portfolio_id = golden_db

    with pytest.raises(ValueError, match="max_age_days"):
        get_portfolio_daily_returns(
            connection,
            portfolio_id,
            ["2026-01-05", "2026-01-06"],
            max_age_days=-1,
        )