import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from riskengine.api import main
from riskengine.api.analysis_models import RiskResponse
from riskengine.risk import performance_service
from riskengine.risk.analysis_adapters import build_risk_response


def http_get(portfolio_id=1, **query):
    async def run():
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        async with main.app.router.lifespan_context(main.app):
            await main.app({"type": "http", "http_version": "1.1", "method": "GET",
                            "path": f"/api/portfolios/{portfolio_id}/risk", "root_path": "",
                            "query_string": urlencode(query).encode(), "headers": [], "scheme": "http",
                            "server": ("test", 80), "client": ("test", 1)}, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"),
                          parse_constant=lambda value: pytest.fail(f"Non-finite JSON: {value}"))
        if status == 200:
            RiskResponse.model_validate(body)
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


@pytest.mark.parametrize("query", [{}, {"confidence": .625}, {"confidence": .99},
                                  {"start_date": "2026-01-06", "end_date": "2026-01-08"},
                                  {"start_date": "2026-01-05", "end_date": "2026-01-06"}])
def test_exact_adapter_parity_and_periods(api, monkeypatch, query):
    original = performance_service.prepare_analysis
    prepared = []
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        prepared.append(result)
        return result
    monkeypatch.setattr(performance_service, "prepare_analysis", capture)
    status, body = api(**query)
    assert status == 200
    assert body == build_risk_response(prepared[0]).model_dump(mode="json")
    assert body["confidence"] == query.get("confidence", .95)
    if query.get("end_date") == "2026-01-06":
        assert len(body["portfolio_intervals"]) == 1
        for name in ("historical_var", "historical_cvar", "maximum_drawdown"):
            assert body["portfolio_metrics"][name]["value"] is not None
        for name in ("annualized_volatility", "sharpe_ratio", "beta"):
            assert body["portfolio_metrics"][name]["value"] is None
            assert body["portfolio_metrics"][name]["unavailable_reasons"]
        assert len(body["drawdown"]["portfolio"]["points"]) == 2


@pytest.mark.parametrize("query", [{"confidence": v} for v in [0, 1, -1, 2, "nan", "inf", "-inf", "bad"]] + [
    {"start_date": "2026-02-30"}, {"end_date": "20260105"}, {"benchmark": " "}, {"benchmark": ""},
    {"start_date": "2026-01-09", "end_date": "2026-01-05"}])
def test_invalid_parameters(api, query):
    status, body = api(**query)
    assert status == 422 and body["detail"]


@pytest.mark.parametrize("pid", [0, -1, "bad", 9223372036854775808])
def test_invalid_ids(api, pid):
    assert api(pid)[0] == 422


def test_unknown_portfolio_and_database(api, monkeypatch, tmp_path):
    assert api(999)[0] == 404
    monkeypatch.setenv("RISKENGINE_DB", str(tmp_path / "absent.db"))
    assert api()[0] == 404


@pytest.mark.parametrize("benchmark", ["ABSENT", "SPY"])
def test_unavailable_benchmark_does_not_suppress_portfolio(api, golden_spy_db, benchmark):
    c, _ = golden_spy_db
    if benchmark == "SPY":
        c.execute("DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='SPY')")
    status, body = api(benchmark=benchmark)
    assert status == 200
    assert body["portfolio_metrics"]["historical_var"]["value"] is not None
    assert body["portfolio_metrics"]["beta"]["value"] is None
    assert body["benchmark_metrics"]["historical_var"]["value"] is None
    assert body["drawdown"]["portfolio"]["points"]
    assert body["drawdown"]["benchmark"]["unavailable_reasons"]


def test_empty_portfolio(api, golden_spy_db):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Empty risk','USD')").lastrowid
    status, body = api(pid)
    assert status == 200 and body["status"] == "unavailable"
    assert body["valuations"] == body["portfolio_intervals"] == []
    assert body["period"]["effective_baseline_date"] is None
    assert all(m["value"] is None for m in body["portfolio_metrics"].values())


def test_missing_prices_are_200_integrity_is_409(api, golden_spy_db):
    c, _ = golden_spy_db
    c.execute("DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    status, body = api()
    assert status == 200 and body["warnings"]
    assert body["portfolio_metrics"]["historical_var"]["value"] is None
    c.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    status, body = api()
    assert status == 409
    assert any(i["category"] == "integrity" and i["code"] == "invalid_positions" for i in body["detail"])


def test_future_invalid_trade_does_not_change_earlier_risk(api, golden_spy_db):
    c, pid = golden_spy_db
    before = api(end_date="2026-01-09")
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    assert api(end_date="2026-01-09") == before


@pytest.mark.parametrize("failure", [None, "unexpected", "unknown", "integrity", "input"])
def test_readonly_snapshot_and_cleanup(api, golden_spy_db, monkeypatch, failure):
    c, _ = golden_spy_db
    if failure == "integrity":
        c.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    opened = []
    original_open = main.get_connection
    original_prepare = performance_service.prepare_analysis
    def open_connection(path, *, read_only):
        assert read_only
        result = original_open(path, read_only=read_only)
        opened.append(result)
        return result
    def prepare(connection, *args, **kwargs):
        assert connection.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE portfolios SET name=name")
        if failure == "unexpected":
            raise RuntimeError("risk preparation failed")
        return original_prepare(connection, *args, **kwargs)
    monkeypatch.setattr(main, "get_connection", open_connection)
    monkeypatch.setattr(performance_service, "prepare_analysis", prepare)
    if failure == "unexpected":
        with pytest.raises(RuntimeError, match="risk preparation failed"):
            api()
    else:
        status, _ = api(999 if failure == "unknown" else 1,
                        **({"start_date": "0001-01-01"} if failure == "input" else {}))
        assert status == {None: 200, "unknown": 404, "integrity": 409, "input": 422}[failure]
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = opened[0].in_transaction


def test_historical_exact_adapter_parity(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    if not path.exists():
        pytest.skip("Historical database unavailable")
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    original = performance_service.prepare_analysis
    captured = []
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result
    monkeypatch.setattr(performance_service, "prepare_analysis", prepare)
    status, body = http_get()
    assert status == 200
    assert len(body["valuations"]) == 716 and len(body["portfolio_intervals"]) == 715
    assert len(body["drawdown"]["portfolio"]["points"]) == 716
    assert body["period"]["effective_baseline_date"] == "2024-01-02"
    assert body == build_risk_response(captured[0]).model_dump(mode="json")
