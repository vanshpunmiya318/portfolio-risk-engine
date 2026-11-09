"""Regression tests for historical portfolio transactions.

Uses a private in-memory database; never modifies the real portfolio.
"""
import sqlite3

import pytest

from riskengine.db.transactions import (
    add_cash_flow,
    add_transaction,
    get_cash_balance,
    get_cost_basis,
    get_positions,
    validate_cash_balances,
    validate_transactions,
)


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.executescript('''
        CREATE TABLE instruments (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL);
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL,
            instrument_id INTEGER NOT NULL, date TEXT NOT NULL,
            transaction_type TEXT NOT NULL, quantity REAL NOT NULL,
            price REAL NOT NULL, currency TEXT NOT NULL, source TEXT NOT NULL
        );
        CREATE TABLE cash_flows (
            id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL,
            date TEXT NOT NULL, flow_type TEXT NOT NULL,
            amount REAL NOT NULL, currency TEXT NOT NULL, source TEXT NOT NULL
        );
        CREATE TABLE corporate_actions (
            id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL,
            action_type TEXT NOT NULL, ex_date TEXT NOT NULL, ratio REAL NOT NULL
        );
        INSERT INTO instruments (id, ticker) VALUES (1, 'SPY'), (2, 'XLE');
    ''')
    yield connection
    connection.close()


def trade(db, date, kind, qty, price=100, instrument=1):
    return add_transaction(db, 1, instrument, date, kind, qty, price, 'USD', 'test')


def fund(db, date='2024-01-02', amount=1000):
    add_cash_flow(db, 1, date, 'DEPOSIT', amount, 'USD', 'test')


def test_funded_purchase_reduces_cash_and_adds_shares(db):
    fund(db)
    trade(db, '2024-01-03', 'BUY', 4)
    assert validate_cash_balances(db, 1) is True
    assert validate_transactions(db, 1) is True
    assert get_cash_balance(db, 1, '2024-01-03') == pytest.approx(600)
    assert get_positions(db, 1, '2024-01-03').set_index('ticker').loc['SPY', 'quantity'] == pytest.approx(4)


def test_insufficient_cash_detected(db):
    fund(db)
    trade(db, '2024-01-03', 'BUY', 11)
    with pytest.raises(ValueError, match='Negative cash balance'):
        validate_cash_balances(db, 1)


def test_oversell_detected(db):
    fund(db)
    trade(db, '2024-01-03', 'BUY', 3)
    trade(db, '2024-01-04', 'SELL', 4)
    with pytest.raises(ValueError, match='Negative position'):
        validate_transactions(db, 1)


def test_backdated_buy_detects_later_cash_deficit(db):
    fund(db)
    trade(db, '2024-01-10', 'BUY', 8)
    assert validate_cash_balances(db, 1) is True
    trade(db, '2024-01-05', 'BUY', 3)
    with pytest.raises(ValueError, match='2024-01-10'):
        validate_cash_balances(db, 1)


def test_backdated_sell_detects_later_share_deficit(db):
    fund(db)
    trade(db, '2024-01-03', 'BUY', 5)
    trade(db, '2024-01-10', 'SELL', 4)
    assert validate_transactions(db, 1) is True
    trade(db, '2024-01-05', 'SELL', 2)
    with pytest.raises(ValueError, match='2024-01-10'):
        validate_transactions(db, 1)


def test_split_adjusts_holdings_and_average_cost(db):
    fund(db, amount=2000)
    trade(db, '2025-01-10', 'BUY', 5, 100, instrument=2)
    db.execute("INSERT INTO corporate_actions (instrument_id, action_type, ex_date, ratio) VALUES (2, 'SPLIT', '2025-12-05', 2)")
    before = get_positions(db, 1, '2025-12-04').set_index('ticker').loc['XLE', 'quantity']
    after = get_positions(db, 1, '2025-12-05').set_index('ticker').loc['XLE', 'quantity']
    assert before == pytest.approx(5)
    assert after == pytest.approx(10)
    assert validate_transactions(db, 1) is True
    cost = get_cost_basis(db, 1).set_index('ticker').loc['XLE']
    assert cost['total_cost'] == pytest.approx(500)
    assert cost['average_cost'] == pytest.approx(50)


def test_invalid_trade_does_not_insert(db):
    with pytest.raises(ValueError, match='positive and finite'):
        trade(db, '2024-01-03', 'BUY', 0)
    assert db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
