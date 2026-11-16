"""Legacy submission policy and atomicity, using temporary databases only."""
import ast
import hashlib
import os
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from riskengine.db import streamlit_transactions as service
from riskengine.db.schema.loader import create_schema
from riskengine.db.transactions import add_cash_flow, add_transaction


@pytest.fixture
def user_db(tmp_path, monkeypatch):
    path = tmp_path / 'user-owned.db'
    create_schema(path)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO portfolios VALUES(1,'User','USD')")
        c.execute("INSERT INTO instruments(id,ticker,name,asset_type,currency) VALUES(1,'SPY','SPY','ETF','USD')")
        for day in ('2024-01-03', '2024-01-05', '2024-01-10'):
            c.execute("INSERT INTO prices(instrument_id,date,open,high,low,close,adjusted_close,source,fetched_at) VALUES(1,?,100,100,100,100,100,'test','test')", (day,))
        add_cash_flow(c, 1, '2024-01-02', 'DEPOSIT', 1000, 'USD', 'test')
    monkeypatch.setenv('RISKENGINE_DB', str(path))
    monkeypatch.setenv('RISKENGINE_ALLOW_TRANSACTION_WRITES', '1')
    return path


def submit(path, **kwargs):
    args = dict(database=path, demo_mode=False, portfolio_id=1, instrument_id=1,
                ticker='SPY', trade_date='2024-01-05', transaction_type='BUY', quantity=1, price=100)
    args.update(kwargs)
    return service.record_streamlit_transaction(**args)


@pytest.mark.parametrize('mode', ['no_opt_in', 'wrong_opt_in', 'no_override', 'different_override', 'demo'])
def test_blocked_submissions_never_open_database_or_change_contents(user_db, monkeypatch, mode):
    if mode == 'no_opt_in':
        monkeypatch.delenv('RISKENGINE_ALLOW_TRANSACTION_WRITES')
    elif mode == 'wrong_opt_in':
        monkeypatch.setenv('RISKENGINE_ALLOW_TRANSACTION_WRITES', 'true')
    elif mode == 'no_override':
        monkeypatch.delenv('RISKENGINE_DB')
    elif mode == 'different_override':
        monkeypatch.setenv('RISKENGINE_DB', str(user_db.parent / 'different.db'))
    before = user_db.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Blocked submission opened a connection')
    monkeypatch.setattr(service, 'get_connection', forbidden)
    with pytest.raises(PermissionError):
        submit(user_db, demo_mode=mode == 'demo')
    assert user_db.read_bytes() == before


@pytest.mark.parametrize('name', ['historical_portfolio.db', 'demo_portfolio.db'])
@pytest.mark.parametrize('alias', ['direct', 'dotdot', 'hardlink', 'symlink'])
def test_opt_in_cannot_write_bundled_files_via_aliases(tmp_path, user_db, monkeypatch, name, alias):
    # Simulated bundled files are temporary fixtures, never the actual datasets.
    data = tmp_path / 'data'
    data.mkdir()
    protected = data / name
    protected.write_bytes(user_db.read_bytes())
    monkeypatch.setattr(service, 'ROOT', tmp_path)
    target = protected
    if alias == 'dotdot':
        target = data / '..' / 'data' / name
    elif alias == 'hardlink':
        target = tmp_path / 'alias.db'
        os.link(protected, target)
    elif alias == 'symlink':
        target = tmp_path / 'alias.db'
        try:
            target.symlink_to(protected)
        except OSError:
            pytest.skip('OS does not permit symlink creation')
    monkeypatch.setenv('RISKENGINE_DB', str(target))
    before = protected.read_bytes()
    with pytest.raises(PermissionError):
        submit(target)
    assert protected.read_bytes() == before


def test_actual_bundled_paths_are_blocked_without_opening(monkeypatch):
    for name in ('historical_portfolio.db', 'demo_portfolio.db'):
        path = service.ROOT / 'data' / name
        before = hashlib.sha256(path.read_bytes()).digest()
        monkeypatch.setenv('RISKENGINE_DB', str(path))
        monkeypatch.setenv('RISKENGINE_ALLOW_TRANSACTION_WRITES', '1')
        assert not service.transaction_writes_allowed(path, False)
        assert hashlib.sha256(path.read_bytes()).digest() == before


def test_permitted_write_retains_original_recording_contract(user_db):
    assert service.transaction_writes_allowed(user_db, False)
    submit(user_db)
    with sqlite3.connect(user_db) as c:
        assert c.execute('SELECT date,transaction_type,quantity,price,currency,source FROM transactions').fetchall() == [('2024-01-05', 'BUY', 1, 100, 'USD', 'streamlit_dashboard')]


@pytest.mark.parametrize('failure', ['later_cash_deficit', 'later_share_deficit'])
def test_permitted_backdated_write_rolls_back_on_full_ledger_validation(user_db, failure):
    with sqlite3.connect(user_db) as c:
        if failure == 'later_cash_deficit':
            add_transaction(c, 1, 1, '2024-01-10', 'BUY', 8, 100, 'USD', 'test')
            kind, quantity, reason = 'BUY', 3, 'Negative cash balance'
        else:
            add_transaction(c, 1, 1, '2024-01-03', 'BUY', 5, 100, 'USD', 'test')
            add_transaction(c, 1, 1, '2024-01-10', 'SELL', 4, 100, 'USD', 'test')
            kind, quantity, reason = 'SELL', 2, 'Negative position'
    before = user_db.read_bytes()
    with pytest.raises(ValueError, match=reason):
        submit(user_db, transaction_type=kind, quantity=quantity)
    with sqlite3.connect(user_db) as c:
        assert not c.execute("SELECT id FROM transactions WHERE source='streamlit_dashboard'").fetchall()
    assert user_db.read_bytes() == before


def test_submission_handler_blocks_even_if_disabled_form_is_bypassed(user_db, monkeypatch):
    monkeypatch.delenv('RISKENGINE_ALLOW_TRANSACTION_WRITES')
    tree = ast.parse((service.ROOT / 'app.py').read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == 'submitted')
    errors = []
    def unexpected(*args):
        pytest.fail('Blocked handler reported success or reran')
    namespace = dict(submitted=True, DATABASE=user_db, DEMO_MODE=False, portfolio_id=1,
        instruments=pd.DataFrame([{'id': 1, 'ticker': 'SPY'}]), ticker='SPY',
        trade_date=date(2024, 1, 5), transaction_type='BUY', quantity=1, price=100,
        record_streamlit_transaction=service.record_streamlit_transaction,
        st=SimpleNamespace(error=errors.append, success=unexpected, rerun=unexpected))
    before = user_db.read_bytes()
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), namespace)
    assert errors == ['Transaction not recorded: Transaction writes are disabled for this database.']
    assert user_db.read_bytes() == before
