"""Portfolio-specific Overview selection, legacy parity and read-only access."""
import asyncio
import hashlib
import json
import sqlite3

import pytest

from riskengine.api import main
from riskengine.db.schema.loader import create_schema
from riskengine.db.portfolios import get_total_portfolio_equity
from riskengine.db.transactions import get_net_cash_flows


def http_get(path):
    async def run():
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        async with main.app.router.lifespan_context(main.app):
            await main.app({'type': 'http', 'http_version': '1.1', 'method': 'GET',
                'path': path, 'root_path': '', 'query_string': b'', 'headers': [],
                'scheme': 'http', 'server': ('test', 80), 'client': ('test', 1)}, receive, send)
        return next(m['status'] for m in messages if m['type'] == 'http.response.start'), json.loads(b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))
    return asyncio.run(run())


@pytest.fixture
def overview_db(tmp_path, monkeypatch):
    path = tmp_path / 'overview.db'
    create_schema(path)
    with sqlite3.connect(path) as c:
        c.executescript("""
        INSERT INTO portfolios VALUES(1,'First portfolio','USD'),(7,'Other portfolio','GBP'),(9,'Empty portfolio','EUR');
        INSERT INTO instruments(id,ticker,name,asset_type,currency,primary_source) VALUES
        (1,'FIRST','First instrument','STOCK','USD','test'),(2,'OTHER','Other instrument','STOCK','GBP','test');
        INSERT INTO cash_flows VALUES(1,1,'2026-01-02','DEPOSIT',1000,'USD','test'),
        (2,7,'2026-01-02','DEPOSIT',2000,'GBP','test'),(3,7,'2026-01-03','WITHDRAWAL',100,'GBP','test');
        INSERT INTO transactions VALUES(1,1,1,'2026-01-02','BUY',2,100,'USD','test'),
        (2,7,2,'2026-01-02','BUY',3.5,40,'GBP','test');
        INSERT INTO prices(instrument_id,date,open,high,low,close,adjusted_close,source,fetched_at) VALUES
        (1,'2026-01-05',110,110,110,110,110,'test','2026-01-05T00:00:00Z'),
        (2,'2026-01-05',50,50,50,50,55,'test','2026-01-05T00:00:00Z');
        """)
    monkeypatch.delenv('RISKENGINE_DEMO', raising=False)
    monkeypatch.setenv('RISKENGINE_DB', str(path))
    return path


def test_two_portfolios_keep_distinct_identity_currency_and_validated_values(overview_db):
    before = hashlib.sha256(overview_db.read_bytes()).digest()
    for pid, name, currency, equity, contributions, profit in (
        (1, 'First portfolio', 'USD', 1020, 1000, 20),
        (7, 'Other portfolio', 'GBP', 1952.5, 1900, 52.5),
        (9, 'Empty portfolio', 'EUR', 0, 0, 0),
    ):
        status, body = http_get(f'/api/portfolios/{pid}/summary')
        assert status == 200
        assert body == dict(portfolio_id=pid, name=name, currency=currency, as_of_date='2026-01-05',
                            total_equity=equity, net_contributions=contributions, profit_loss=profit)
        with sqlite3.connect(overview_db) as c:
            assert body['total_equity'] == get_total_portfolio_equity(c, pid, body['as_of_date'])
            assert body['net_contributions'] == get_net_cash_flows(c, pid, body['as_of_date'])
    assert hashlib.sha256(overview_db.read_bytes()).digest() == before


def test_legacy_selects_first_and_has_exact_specific_response_parity(overview_db):
    assert http_get('/api/portfolio/summary') == http_get('/api/portfolios/1/summary')
    # Retain the existing global latest-price cutoff, not a new date policy.
    with sqlite3.connect(overview_db) as c:
        c.execute("UPDATE prices SET date='2026-01-06' WHERE instrument_id=2")
    legacy = http_get('/api/portfolio/summary')
    assert legacy == http_get('/api/portfolios/1/summary')
    assert legacy[1]['as_of_date'] == '2026-01-06'


def test_missing_id_does_not_fall_back_and_invalid_ids_are_rejected(overview_db):
    assert http_get('/api/portfolios/999/summary') == (404, {'detail': 'Portfolio 999 not found'})
    for value in ('0', '-1', 'bad', '9223372036854775808'):
        assert http_get(f'/api/portfolios/{value}/summary')[0] == 422


@pytest.mark.parametrize('path', ['/api/portfolio/summary', '/api/portfolios/7/summary'])
def test_existing_no_price_error_is_preserved(overview_db, path):
    with sqlite3.connect(overview_db) as c:
        c.execute('DELETE FROM prices')
    assert http_get(path) == (404, {'detail': 'No market prices available'})


def test_empty_portfolios_preserve_legacy_error_but_specific_id_is_not_found(overview_db):
    with sqlite3.connect(overview_db) as c:
        c.executescript('DELETE FROM transactions; DELETE FROM cash_flows; DELETE FROM portfolios;')
    assert http_get('/api/portfolio/summary') == (404, {'detail': 'No portfolio found'})
    assert http_get('/api/portfolios/7/summary') == (404, {'detail': 'Portfolio 7 not found'})


@pytest.mark.parametrize('path', ['/api/portfolio/summary', '/api/portfolios/7/summary'])
def test_missing_database_is_not_created(tmp_path, monkeypatch, path):
    absent = tmp_path / 'absent.db'
    monkeypatch.delenv('RISKENGINE_DEMO', raising=False)
    monkeypatch.setenv('RISKENGINE_DB', str(absent))
    assert http_get(path) == (404, {'detail': 'Portfolio database not found'})
    assert not absent.exists()


@pytest.mark.parametrize('path', ['/api/portfolio/summary', '/api/portfolios/7/summary'])
def test_both_routes_share_one_explicit_readonly_connection(overview_db, monkeypatch, path):
    original = main.get_connection
    connections, statements = [], []
    def connect(db, *, read_only):
        assert read_only
        c = original(db, read_only=read_only)
        connections.append(c)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            c.execute("UPDATE portfolios SET name='not allowed'")
        c.rollback()
        c.set_trace_callback(statements.append)
        return c
    monkeypatch.setattr(main, 'get_connection', connect)
    equity, flows = main.get_total_portfolio_equity, main.get_net_cash_flows
    def checked(function):
        def call(c, *args):
            assert c is connections[0] and c.in_transaction
            return function(c, *args)
        return call
    monkeypatch.setattr(main, 'get_total_portfolio_equity', checked(equity))
    monkeypatch.setattr(main, 'get_net_cash_flows', checked(flows))
    assert http_get(path)[0] == 200
    assert len(connections) == 1 and statements[0] == 'BEGIN'
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        _ = connections[0].in_transaction
