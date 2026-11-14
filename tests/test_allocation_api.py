import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from riskengine.api import main
from riskengine.api.allocation_models import AllocationResponse
from riskengine.risk.allocation_service import get_allocation_response
from riskengine.risk.exposure import get_portfolio_exposure
from riskengine.risk.metrics import portfolio_hhi


def http_get(portfolio_id=1, **query):
    async def run():
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        async with main.app.router.lifespan_context(main.app):
            await main.app({"type": "http", "http_version": "1.1", "method": "GET", "path": f"/api/portfolios/{portfolio_id}/allocation",
                            "root_path": "", "query_string": urlencode(query).encode(), "headers": [], "scheme": "http",
                            "server": ("test", 80), "client": ("test", 1)}, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"),
                          parse_constant=lambda value: pytest.fail(f"Nonfinite JSON: {value}"))
        if status == 200:
            AllocationResponse.model_validate(body)
        return status, body
    return asyncio.run(run())


@pytest.fixture
def api(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", c.execute("PRAGMA database_list").fetchone()[2])
    def call(portfolio_id=pid, **query):
        c.commit()
        return http_get(portfolio_id, **query)
    return call


def test_primitive_parity_cash_exclusion_and_no_targets(api, golden_spy_db):
    c, pid = golden_spy_db
    status, body = api(as_of_date="2026-01-09")
    assert status == 200 and body["current"]["valuation_complete"]
    expected = get_portfolio_exposure(c, pid, "2026-01-09")
    current = body["current"]
    assert current["invested_value"] == expected["total_invested_value"]
    actual = {r["ticker"]: r["weight"] for r in current["securities"]}
    assert actual == {r["ticker"]: r["weight"] for r in expected["security_exposures"]}
    hhi = portfolio_hhi(pd.Series([r["weight"] for r in expected["security_exposures"]]))
    assert current["concentration"] == dict(hhi=hhi, effective_holdings=1 / hhi, largest_position_weight=expected["largest_security"]["weight"])
    assert sum(actual.values()) == pytest.approx(1)
    assert current["cash_balance"] != 0
    assert body["weight_basis"] == "invested_holdings_excluding_cash"
    assert body["target_allocation"] is None and body["target_unavailable_reasons"]
    assert body["reference"]["is_investment_policy_target"] is False
    assert body["reference"]["reference_date"] == "2026-01-05"
    assert sum(r["weight_change_fraction"] for r in body["changes"]) == pytest.approx(0)


@pytest.mark.parametrize("scope,day", [("current", "2026-01-09"), ("reference", "2026-01-05")])
def test_missing_prices_do_not_shift_reference_or_normalize_partial_positions(api, golden_spy_db, scope, day):
    c, _ = golden_spy_db
    c.execute("DELETE FROM prices WHERE date=? AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (day,))
    status, body = api(as_of_date="2026-01-09")
    assert status == 200 and body["changes"] is None
    assert body["reference"]["reference_date"] == "2026-01-05"
    snap = body["current"] if scope == "current" else body["reference"]["snapshot"]
    assert not snap["valuation_complete"]
    assert snap["invested_value"] is None
    assert snap["concentration"]["hhi"] is None
    assert all(r["weight"] is None for r in snap["securities"])
    assert snap["classification_exposures"] == []
    if scope == "reference":
        assert body["current"]["concentration"]["hhi"] is not None


@pytest.mark.parametrize("table", ["transactions", "cash_flows", "instruments"])
def test_currency_conflicts_suppress_aggregate_values(api, golden_spy_db, table):
    c, _ = golden_spy_db
    c.execute(f"UPDATE {table} SET currency='EUR'")
    status, body = api()
    assert status == 200
    assert body["current"]["invested_value"] is None and body["changes"] is None
    assert body["current"]["cash_balance"] is None
    assert body["current"]["concentration"]["hhi"] is None
    assert any(r["code"] == "currency_conflict" for r in body["current"]["unavailable_reasons"])


def test_new_and_exited_holdings_have_genuine_zero_comparison_weights(api, golden_spy_db):
    c, pid = golden_spy_db
    c.execute("DELETE FROM transactions WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL') AND date>'2026-01-05'")
    held = c.execute("SELECT SUM(CASE WHEN transaction_type='BUY' THEN quantity ELSE -quantity END) FROM transactions WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')").fetchone()[0]
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-09','SELL',?,100,'USD','test')", (pid, held))
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='SPY'),'2026-01-09','BUY',1,515,'USD','test')", (pid,))
    status, body = api(as_of_date="2026-01-09")
    assert status == 200
    changes = {r["ticker"]: r for r in body["changes"]}
    assert changes["AAPL"]["current_weight"] == 0 and changes["AAPL"]["reference_weight"] > 0
    assert changes["SPY"]["reference_weight"] == 0 and changes["SPY"]["current_weight"] > 0
    # A closed foreign-currency history must not evade the service currency gate.
    c.execute("UPDATE transactions SET currency='EUR' WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    assert api(as_of_date="2026-01-09")[1]["changes"] is None


def test_empty_and_preinvestment_cutoff(api, golden_spy_db):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Empty allocation','USD')").lastrowid
    status, body = api(pid)
    assert status == 200 and body["current"]["invested_value"] == 0
    assert body["current"]["securities"] == body["current"]["classification_exposures"] == []
    assert body["current"]["concentration"]["hhi"] is None and body["changes"] is None
    status, earlier = api(as_of_date="2026-01-02")
    assert status == 200 and earlier["reference"]["reference_date"] is None
    assert earlier["current"]["invested_value"] == 0


@pytest.mark.parametrize("case", ["oversell", "corporate_actions"])
def test_integrity_conflicts_are_409(api, golden_spy_db, case):
    c, _ = golden_spy_db
    if case == "oversell":
        c.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    else:
        c.execute("UPDATE prices SET source='tiingo'")
        c.execute("DELETE FROM corporate_action_coverage")
    assert api()[0] == 409


@pytest.mark.parametrize("query", [{"as_of_date": "2026-02-30"}, {"as_of_date": "20260105"}])
def test_invalid_dates(api, query):
    assert api(**query)[0] == 422


@pytest.mark.parametrize("pid", [0, -1, "abc", 9223372036854775808])
def test_invalid_ids(api, pid):
    assert api(pid)[0] == 422


def test_unknown_portfolio(api):
    assert api(999)[0] == 404


def test_invalid_reference_integrity_does_not_hide_valid_current_allocation(api, golden_spy_db):
    c, _ = golden_spy_db
    c.execute("UPDATE prices SET source='tiingo' WHERE date='2026-01-05'")
    c.execute("DELETE FROM corporate_action_coverage")
    status, body = api(as_of_date="2026-01-09")
    assert status == 200 and body["current"]["concentration"]["hhi"] is not None
    assert body["reference"]["reference_date"] == "2026-01-05"
    assert body["reference"]["snapshot"] is None
    assert body["reference"]["unavailable_reasons"][0]["code"] == "invalid_reference"
    assert body["changes"] is None


def test_future_events_do_not_change_historical_allocation_and_classification_fallback(api, golden_spy_db):
    c, pid = golden_spy_db
    c.execute("UPDATE instruments SET sector=NULL WHERE ticker='AAPL'")
    before = api(as_of_date="2026-01-09")
    assert next(r for r in before[1]["current"]["securities"] if r["ticker"] == 'AAPL')["classification"] == "Unclassified"
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,'2026-01-12','DEPOSIT',100,'EUR','test')", (pid,))
    assert api(as_of_date="2026-01-09") == before


def test_missing_database_and_transaction_requirement(api, golden_spy_db, monkeypatch, tmp_path):
    c, pid = golden_spy_db
    c.commit()
    with pytest.raises(ValueError, match="read transaction"):
        get_allocation_response(c, pid)
    monkeypatch.setenv("RISKENGINE_DB", str(tmp_path / 'missing.db'))
    assert api()[0] == 404


@pytest.mark.parametrize("failure", [False, True])
def test_readonly_connection_cleanup(api, monkeypatch, failure):
    import riskengine.risk.allocation_service as service
    original_open, original_snapshot = main.get_connection, service._snapshot
    opened = []
    def open_connection(path, *, read_only):
        assert read_only
        result = original_open(path, read_only=read_only); opened.append(result); return result
    def snapshot(c, *args):
        assert c.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("UPDATE portfolios SET name=name")
        if failure:
            raise RuntimeError("allocation failure")
        return original_snapshot(c, *args)
    monkeypatch.setattr(main, "get_connection", open_connection)
    monkeypatch.setattr(service, "_snapshot", snapshot)
    if failure:
        with pytest.raises(RuntimeError, match="allocation failure"):
            api()
    else:
        assert api()[0] == 200
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = opened[0].in_transaction


def test_historical_reference_current_and_xle_split(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    if not path.exists():
        pytest.skip("Historical database unavailable")
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    status, body = http_get()
    assert status == 200
    reference = body["reference"]
    assert reference["reference_date"] == "2024-01-02"
    assert reference["snapshot"]["invested_value"] == 99272.65
    assert reference["snapshot"]["cash_balance"] == pytest.approx(727.35)
    assert reference["snapshot"]["invested_value"] + reference["snapshot"]["cash_balance"] == 100000
    assert reference["snapshot"]["concentration"]["hhi"] == .1947849071150656
    assert body["current"]["invested_value"] == 150870.21713256836
    assert body["current"]["concentration"]["hhi"] == .20758018377173057
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        for day, quantity in [("2025-12-04", 118), ("2025-12-05", 236)]:
            actual = get_allocation_response(c, 1, day)
            exposure = get_portfolio_exposure(c, 1, day)
            xle = next(r for r in actual.current.securities if r.ticker == "XLE")
            assert xle.quantity == quantity
            assert actual.current.invested_value == exposure["total_invested_value"]
            assert sum(r.weight for r in actual.current.securities) == pytest.approx(1)
    weights = {r["ticker"]: r["weight"] for r in reference["snapshot"]["securities"]}
    assert weights["SPY"] == 29776.949999999997 / 99272.65
    assert weights["SPY"] != .30
