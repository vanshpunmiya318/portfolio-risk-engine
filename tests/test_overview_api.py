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


@pytest.mark.parametrize(('currency', 'deposit'), [('EUR', 4321), ('USD', 1000)])
def test_single_currency_cash_flows_keep_exact_totals(overview_db, currency, deposit):
    with sqlite3.connect(overview_db) as c:
        c.execute("INSERT INTO portfolios VALUES(204, 'Cash portfolio', ?)", (currency,))
        c.execute(
            "INSERT INTO cash_flows VALUES(204, 204, '2026-01-02', 'DEPOSIT', ?, ?, 'test')",
            (deposit, currency),
        )
    assert http_get('/api/portfolios/204/summary') == (200, {
        'portfolio_id': 204, 'name': 'Cash portfolio', 'currency': currency,
        'as_of_date': '2026-01-05', 'total_equity': deposit,
        'net_contributions': deposit, 'profit_loss': 0,
    })


def test_mixed_currency_deposits_fail_before_calculating_totals(overview_db, monkeypatch):
    with sqlite3.connect(overview_db) as c:
        c.executescript("""
        INSERT INTO portfolios VALUES(204, 'Mixed cash portfolio', 'EUR');
        INSERT INTO cash_flows VALUES
        (204, 204, '2026-01-02', 'DEPOSIT', 4321, 'EUR', 'test'),
        (205, 204, '2026-01-03', 'DEPOSIT', 100, 'USD', 'test');
        """)

    def unexpected_calculation(*args):
        pytest.fail('Currency conflicts must be detected before calculating totals')

    monkeypatch.setattr(main, 'get_total_portfolio_equity', unexpected_calculation)
    monkeypatch.setattr(main, 'get_net_cash_flows', unexpected_calculation)
    assert http_get('/api/portfolios/204/summary') == (409, {
        'detail': 'Overview totals are unavailable because a trade, instrument, or cash flow '
                  'uses a currency other than EUR. No currency conversion was performed.',
    })


@pytest.mark.parametrize(('instrument_id', 'trade_currency'), [(1, 'GBP'), (2, 'USD'), (999, 'GBP')])
def test_trade_or_instrument_currency_conflict_has_no_totals(overview_db, instrument_id, trade_currency):
    with sqlite3.connect(overview_db) as c:
        c.execute(
            "INSERT INTO transactions VALUES(3, 1, ?, '2026-01-05', 'BUY', 1, 10, ?, 'test')",
            (instrument_id, trade_currency),
        )
    status, body = http_get('/api/portfolios/1/summary')
    assert status == 409
    assert set(body) == {'detail'}
    assert 'uses a currency other than USD' in body['detail']
    assert http_get('/api/portfolio/summary') == (status, body)


def test_currency_conflicts_after_latest_price_do_not_affect_summary(overview_db):
    baseline = http_get('/api/portfolios/1/summary')
    assert baseline[0] == 200
    with sqlite3.connect(overview_db) as c:
        c.executescript("""
        INSERT INTO cash_flows VALUES(4, 1, '2026-01-06', 'DEPOSIT', 100, 'EUR', 'test');
        INSERT INTO transactions VALUES(3, 1, 2, '2026-01-06', 'BUY', 1, 10, 'GBP', 'test');
        """)
    assert http_get('/api/portfolios/1/summary') == baseline
    assert http_get('/api/portfolio/summary') == baseline


@pytest.fixture
def unpriced_portfolio(overview_db):
    with sqlite3.connect(overview_db) as c:
        c.executescript("""
        DELETE FROM transactions;
        DELETE FROM cash_flows;
        DELETE FROM portfolios;
        INSERT INTO portfolios VALUES(203, 'Unpriced portfolio', 'USD');
        INSERT INTO instruments(id,ticker,name,asset_type,currency,primary_source)
        VALUES(3, 'UNPRICED', 'Unpriced stock', 'STOCK', 'USD', 'test');
        INSERT INTO cash_flows VALUES(1, 203, '2026-01-02', 'DEPOSIT', 1000, 'USD', 'test');
        INSERT INTO transactions VALUES(1, 203, 3, '2026-01-02', 'BUY', 2, 100, 'USD', 'test');
        """)
    return overview_db


@pytest.mark.parametrize('path', ['/api/portfolios/203/summary', '/api/portfolio/summary'])
def test_unpriced_holding_returns_conflict_without_totals(unpriced_portfolio, path):
    assert http_get(path) == (409, {
        'detail': 'Overview totals are unavailable because a required historical security price '
                  'is missing on or before 2026-01-05.',
    })


def test_summary_can_use_earlier_historical_price_at_existing_cutoff(unpriced_portfolio):
    with sqlite3.connect(unpriced_portfolio) as c:
        c.execute("""
        INSERT INTO prices(instrument_id,date,open,high,low,close,adjusted_close,source,fetched_at)
        VALUES(3, '2026-01-04', 120, 120, 120, 120, 120, 'test', '2026-01-04T00:00:00Z')
        """)
    assert http_get('/api/portfolios/203/summary') == (200, {
        'portfolio_id': 203, 'name': 'Unpriced portfolio', 'currency': 'USD',
        'as_of_date': '2026-01-05', 'total_equity': 1040, 'net_contributions': 1000,
        'profit_loss': 40,
    })


def test_currency_conflict_takes_precedence_over_missing_price(unpriced_portfolio):
    with sqlite3.connect(unpriced_portfolio) as c:
        c.execute("INSERT INTO cash_flows VALUES(2, 203, '2026-01-03', 'DEPOSIT', 100, 'EUR', 'test')")
    assert http_get('/api/portfolios/203/summary') == (409, {
        'detail': 'Overview totals are unavailable because a trade, instrument, or cash flow '
                  'uses a currency other than USD. No currency conversion was performed.',
    })


def test_unexpected_valuation_error_is_not_classified_as_missing_price(overview_db, monkeypatch):
    def fail(*args):
        raise ValueError('Unexpected valuation failure')

    monkeypatch.setattr(main, 'get_total_portfolio_equity', fail)
    with pytest.raises(ValueError, match='Unexpected valuation failure'):
        http_get('/api/portfolios/1/summary')


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
