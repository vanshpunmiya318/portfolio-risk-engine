from contextlib import closing

import pandas as pd
import pytest

from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema
from riskengine.db.transactions import (
    add_transaction,
    get_cost_basis,
    validate_transactions,
)
from riskengine.risk.holdings import load_portfolio_holdings


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "cutoff.db"
    create_schema(path)
    with closing(get_connection(path)) as connection:
        connection.execute("INSERT INTO portfolios VALUES (1, 'Cutoff Test', 'USD')")
        connection.execute(
            "INSERT INTO instruments (id, ticker, name, asset_type, currency, primary_source) "
            "VALUES (1, 'TEST', 'Test Instrument', 'STOCK', 'USD', 'test')"
        )
        connection.commit()
        yield path, connection


def trade(connection, day, kind, quantity, price):
    add_transaction(connection, 1, 1, day, kind, quantity, price, "USD", "test")


def split(connection, day, ratio):
    connection.execute(
        "INSERT INTO corporate_actions "
        "(instrument_id, ex_date, action_type, ratio, source, fetched_at) "
        "VALUES (1, ?, 'SPLIT', ?, 'test', ?)",
        (day, ratio, day + "T00:00:00Z"),
    )


def price(connection, day, amount):
    connection.execute(
        "INSERT INTO prices "
        "(instrument_id, date, open, high, low, close, adjusted_close, source, fetched_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, 'test', ?)",
        (day, amount, amount, amount, amount, amount, day + "T00:00:00Z"),
    )


def test_historical_holdings_exclude_next_day_sale(ledger):
    path, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    trade(connection, "2026-01-06", "SELL", 4, 150)
    price(connection, "2026-01-05", 120)
    connection.commit()

    basis = get_cost_basis(connection, 1, as_of_date="2026-01-05").iloc[0]
    assert basis["quantity"] == 10
    assert basis["total_cost"] == 1000
    assert basis["average_cost"] == 100
    holdings = load_portfolio_holdings(path, 1, "2026-01-05")
    row = holdings.iloc[0]
    assert row["shares"] == 10
    assert row["market_value"] == 1200
    assert row["net_invested"] == 1000  # Preserve the legacy column name.
    assert row["unrealized_pl"] == 200
    assert row["price_date"] == pd.Timestamp("2026-01-05")
    assert row["weight_pct"] == 100
    # The default loader cutoff is the latest price date, not the last trade.
    pd.testing.assert_frame_equal(load_portfolio_holdings(path, 1), holdings)


@pytest.mark.parametrize(
    "cutoff,quantity,total_cost,average_cost",
    [("2026-01-05", 10, 1000, 100), ("2026-01-06", 20, 1000, 50),
     ("2026-01-07", 15, 750, 50)],
)
def test_split_cutoff_and_sale(ledger, cutoff, quantity, total_cost, average_cost):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    split(connection, "2026-01-06", 2)
    trade(connection, "2026-01-07", "SELL", 5, 60)
    row = get_cost_basis(connection, 1, cutoff).iloc[0]
    assert row["quantity"] == quantity
    assert row["total_cost"] == total_cost
    assert row["average_cost"] == average_cost


def test_cutoff_day_splits_precede_trades_and_trades_follow_id_order(ledger):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    split(connection, "2026-01-05", 2)
    trade(connection, "2026-01-05", "BUY", 4, 60)
    trade(connection, "2026-01-05", "SELL", 6, 70)
    trade(connection, "2026-01-06", "BUY", 1, 90)
    row = get_cost_basis(connection, 1, "2026-01-05").iloc[0]
    assert row["quantity"] == 18
    assert row["total_cost"] == pytest.approx(930)
    assert row["average_cost"] == pytest.approx(1240 / 24)


def test_validation_ignores_future_invalid_sale_but_not_cutoff_day_sale(ledger):
    path, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    trade(connection, "2026-01-06", "SELL", 11, 150)
    price(connection, "2026-01-05", 120)
    connection.commit()
    assert validate_transactions(connection, 1, "2026-01-05") is True
    assert get_cost_basis(connection, 1, "2026-01-05").iloc[0]["total_cost"] == 1000
    assert load_portfolio_holdings(path, 1, "2026-01-05").iloc[0]["unrealized_pl"] == 200
    for cutoff in ("2026-01-06", None):
        with pytest.raises(ValueError, match="Negative position"):
            validate_transactions(connection, 1, cutoff)
        with pytest.raises(ValueError, match="Negative position"):
            get_cost_basis(connection, 1, cutoff)


def test_validation_applies_split_cutoff(ledger):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    split(connection, "2026-01-06", 0.5)
    trade(connection, "2026-01-06", "SELL", 6, 150)
    assert validate_transactions(connection, 1, "2026-01-05") is True
    with pytest.raises(ValueError, match="Negative position"):
        get_cost_basis(connection, 1, "2026-01-06")


def test_no_cutoff_preserves_full_history_and_return_structure(ledger):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    split(connection, "2026-01-06", 2)
    trade(connection, "2026-01-07", "BUY", 10, 60)
    trade(connection, "2026-01-08", "SELL", 5, 70)
    implicit = get_cost_basis(connection, 1)
    pd.testing.assert_frame_equal(implicit, get_cost_basis(connection, 1, None))
    pd.testing.assert_frame_equal(implicit, get_cost_basis(connection, 1, "2026-01-08"))
    assert list(implicit.columns) == ["ticker", "quantity", "total_cost", "average_cost"]
    row = implicit.iloc[0]
    assert row["quantity"] == 25
    assert row["total_cost"] == pytest.approx(1600 * 25 / 30)
    assert row["average_cost"] == pytest.approx(1600 / 30)
    assert validate_transactions(connection, 1) is True


def test_cutoff_before_first_trade_returns_existing_empty_structure(ledger):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    result = get_cost_basis(connection, 1, "2026-01-01")
    assert result.empty
    assert list(result.columns) == ["ticker", "quantity", "total_cost", "average_cost"]
    assert validate_transactions(connection, 1, "2026-01-01") is True
