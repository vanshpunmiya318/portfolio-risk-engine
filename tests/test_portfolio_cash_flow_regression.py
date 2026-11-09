
"""Regression tests for external cash flows in portfolio daily returns.

Uses the existing cash_flow_golden_db fixture and never accesses the live database.
"""

import pandas as pd
import pytest

from test_golden_portfolio import golden_db, cash_flow_golden_db
from riskengine.db.transactions import add_cash_flow
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns


def _flow(connection, portfolio_id, date, flow_type, amount):
    add_cash_flow(
        connection,
        portfolio_id=portfolio_id,
        date=date,
        flow_type=flow_type,
        amount=amount,
        currency="USD",
        source="regression_test",
    )
    connection.commit()


def _returns(connection, portfolio_id):
    return get_portfolio_daily_returns(
        connection,
        portfolio_id,
        pd.date_range("2026-01-05", "2026-01-08"),
    )


def test_withdrawal_does_not_create_investment_loss(cash_flow_golden_db):
    connection, portfolio_id = cash_flow_golden_db

    # Withdrawal on Jan 7 does not cause an investment loss.
    # It reduces the capital base for Jan 8 while shares stay unchanged.
    _flow(connection, portfolio_id, "2026-01-07", "WITHDRAWAL", 1000)

    actual = _returns(connection, portfolio_id)

    assert actual.to_numpy() == pytest.approx(
        [0.05, 0.0, 315 / 14750],
        abs=1e-12,
    )


def test_deposit_and_withdrawal_on_different_dates(cash_flow_golden_db):
    connection, portfolio_id = cash_flow_golden_db

    # Existing fixture deposits $5,250 on Jan 7.
    # Withdraw $1,000 on Jan 6.
    _flow(connection, portfolio_id, "2026-01-06", "WITHDRAWAL", 1000)

    actual = _returns(connection, portfolio_id)

    expected = [0.05, 0.0, 315 / 14750]

    assert actual.to_numpy() == pytest.approx(
        expected,
        abs=1e-12,
    )

    assert (1 + actual).prod() - 1 == pytest.approx(
        (1.05 * (1 + 315 / 14750)) - 1,
        abs=1e-12,
    )


def test_multiple_same_day_cash_flows_are_netted(cash_flow_golden_db):
    connection, portfolio_id = cash_flow_golden_db

    # Existing $5,250 deposit on Jan 7.
    # Additional $2,000 deposit and $1,500 withdrawal.
    # Net additional cash = $500.
    _flow(connection, portfolio_id, "2026-01-07", "DEPOSIT", 2000)
    _flow(connection, portfolio_id, "2026-01-07", "WITHDRAWAL", 1500)

    actual = _returns(connection, portfolio_id)

    expected = [0.05, 0.0, 315 / 16250]

    assert actual.to_numpy() == pytest.approx(
        expected,
        abs=1e-12,
    )

    assert (1 + actual).prod() - 1 == pytest.approx(
        (1.05 * (1 + 315 / 16250)) - 1,
        abs=1e-12,
    )
