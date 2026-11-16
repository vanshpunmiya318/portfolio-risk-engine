"""HTTP ledger contract, isolation and read-only snapshot regression tests."""
import asyncio
import hashlib
import json
import sqlite3

import pytest

from riskengine.api import main
from riskengine.db.schema.loader import create_schema


def request(pid=1):
    async def run():
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        async with main.app.router.lifespan_context(main.app):
            await main.app({'type': 'http', 'http_version': '1.1', 'method': 'GET',
                'path': f'/api/portfolios/{pid}/transactions', 'root_path': '',
                'query_string': b'', 'headers': [], 'scheme': 'http',
                'server': ('test', 80), 'client': ('test', 1)}, receive, send)
        return next(m['status'] for m in messages if m['type'] == 'http.response.start'), json.loads(b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))
    return asyncio.run(run())


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / 'history.db'
    create_schema(path)
    with sqlite3.connect(path) as c:
        c.executescript("""
        INSERT INTO portfolios VALUES(1,'Recorded portfolio','GBP'),(2,'Other','USD'),(3,'Empty','EUR');
        INSERT INTO instruments(id,ticker,name,asset_type,currency,primary_source) VALUES(1,'ABC','Example','EQUITY','EUR','test');
        INSERT INTO transactions VALUES
        (1,1,1,'2026-01-02','BUY',0.125,0.0000123456789,'EUR','fixture'),
        (2,1,1,'2026-01-03','SELL',0.025,1.23456789,'GBP','fixture'),
        (3,1,1,'2026-01-03','BUY',0.00000000001,2.5,'EUR','fixture'),
        (4,2,1,'2026-01-04','BUY',10,20,'USD','other');
        INSERT INTO cash_flows VALUES
        (1,1,'2026-01-01','DEPOSIT',100.125,'GBP','fixture'),
        (2,1,'2026-01-03','WITHDRAWAL',0.01,'EUR','fixture'),
        (3,1,'2026-01-03','DEPOSIT',2,'GBP','fixture'),
        (4,2,'2026-01-04','DEPOSIT',999,'USD','other');
        """)
    monkeypatch.delenv('RISKENGINE_DEMO', raising=False)
    monkeypatch.setenv('RISKENGINE_DB', str(path))
    return path


def test_complete_recorded_history_without_prices_and_no_modifications(ledger):
    before = hashlib.sha256(ledger.read_bytes()).digest()
    status, body = request()
    assert status == 200
    assert (body['portfolio_id'], body['name'], body['currency']) == (1, 'Recorded portfolio', 'GBP')
    assert [r['id'] for r in body['trades']] == [3, 2, 1]
    assert [r['id'] for r in body['external_cash_flows']] == [3, 2, 1]
    trade = body['trades'][-1]
    assert (trade['quantity'], trade['price'], trade['currency']) == (0.125, 0.0000123456789, 'EUR')
    assert (trade['ticker'], trade['instrument_name'], trade['instrument_currency'], trade['source']) == ('ABC', 'Example', 'EUR', 'fixture')
    assert trade['issues'][0]['code'] == 'currency_mismatch'
    assert body['external_cash_flows'][-1]['amount'] == 100.125
    assert request()[1] == body  # stable row identities
    assert [r['id'] for r in request(2)[1]['trades']] == [4]
    assert hashlib.sha256(ledger.read_bytes()).digest() == before


def test_empty_missing_and_invalid_portfolio(ledger):
    assert request(3)[1]['trades'] == []
    assert request(3)[1]['external_cash_flows'] == []
    assert request(999)[0] == 404
    assert request(0)[0] == 422


def test_problematic_rows_are_retained_with_reasons(ledger):
    with sqlite3.connect(ledger) as c:
        c.execute("UPDATE transactions SET instrument_id=999,date='invalid-date',source='' WHERE id=1")
    status, body = request()
    assert status == 200
    row = next(r for r in body['trades'] if r['id'] == 1)
    assert row['ticker'] is None and row['instrument_id'] == 999 and row['date'] == 'invalid-date'
    assert {'invalid_recorded_date', 'missing_instrument', 'missing_source'} <= {i['code'] for i in row['issues']}


def test_unserializable_number_is_explicit_conflict(ledger):
    with sqlite3.connect(ledger) as c:
        c.execute('UPDATE transactions SET price=? WHERE id=1', (float('inf'),))
    status, body = request()
    assert status == 409
    assert 'row 1: price is not a finite' in body['detail']


def test_one_connection_explicit_readonly_transaction(ledger, monkeypatch):
    original = main.get_connection
    connections, statements = [], []
    def connect(path, *, read_only=False):
        assert read_only is True
        c = original(path, read_only=read_only)
        connections.append(c)
        c.set_trace_callback(statements.append)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            c.execute("UPDATE portfolios SET name='forbidden'")
        c.rollback()
        statements.clear()
        return c
    monkeypatch.setattr(main, 'get_connection', connect)
    service = main.get_transactions_history
    def inspect(c, pid):
        assert c.in_transaction
        result = service(c, pid)
        assert c.in_transaction
        return result
    monkeypatch.setattr(main, 'get_transactions_history', inspect)
    assert request()[0] == 200
    assert len(connections) == 1
    assert statements[0] == 'BEGIN'
    assert len([s for s in statements if s.startswith('SELECT')]) == 3
    assert not any(s.startswith(('INSERT', 'UPDATE', 'DELETE')) for s in statements)


def test_collections_share_snapshot_during_concurrent_write(ledger, monkeypatch):
    with sqlite3.connect(ledger) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
    original = main.get_connection
    inserted = []
    def connect(path, *, read_only=False):
        c = original(path, read_only=read_only)
        def trace(sql):
            if 'FROM cash_flows' in sql and not inserted:
                with sqlite3.connect(ledger) as writer:
                    writer.execute("INSERT INTO cash_flows VALUES(5,1,'2026-01-04','DEPOSIT',123,'GBP','concurrent')")
                inserted.append(True)
        c.set_trace_callback(trace)
        return c
    monkeypatch.setattr(main, 'get_connection', connect)
    status, body = request()
    assert status == 200 and inserted
    assert [r['id'] for r in body['external_cash_flows']] == [3, 2, 1]
    with sqlite3.connect(ledger) as c:
        assert c.execute('SELECT count(*) FROM cash_flows WHERE id=5').fetchone()[0] == 1

def test_invalid_types_and_values_are_disclosed_not_normalized(ledger):
    with sqlite3.connect(ledger) as c:
        c.execute('PRAGMA ignore_check_constraints=ON')
        c.execute("UPDATE transactions SET transaction_type='TRANSFER',quantity=0 WHERE id=1")
        c.execute("UPDATE cash_flows SET flow_type='UNKNOWN',amount=-3,date='2026-02-30' WHERE id=1")
    status, body = request()
    assert status == 200
    trade = next(r for r in body['trades'] if r['id'] == 1)
    assert trade['transaction_type'] == 'TRANSFER' and trade['quantity'] == 0
    assert {'invalid_recorded_type', 'invalid_recorded_value'} <= {i['code'] for i in trade['issues']}
    cash = next(r for r in body['external_cash_flows'] if r['id'] == 1)
    assert cash['flow_type'] == 'UNKNOWN' and cash['amount'] == -3 and cash['date'] == '2026-02-30'
    assert {'invalid_recorded_date', 'invalid_recorded_type', 'invalid_recorded_value'} <= {i['code'] for i in cash['issues']}


def test_missing_database_does_not_create_one(tmp_path, monkeypatch):
    path = tmp_path / 'absent.db'
    monkeypatch.delenv('RISKENGINE_DEMO', raising=False)
    monkeypatch.setenv('RISKENGINE_DB', str(path))
    assert request() == (404, {'detail': 'Portfolio database not found'})
    assert not path.exists()
