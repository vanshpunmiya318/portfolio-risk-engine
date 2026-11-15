"""Report composition parity, independent availability and snapshot guarantees."""
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from riskengine.api import main
from riskengine.api.report_models import ReportResponse
from riskengine.risk import performance_service, report_service
from riskengine.risk.analysis_adapters import build_performance_response, build_risk_response
from riskengine.risk.allocation_service import get_allocation_response
from riskengine.risk.holdings_service import get_holdings_summary


def http_get(portfolio_id=1, **query):
    async def run():
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        async with main.app.router.lifespan_context(main.app):
            await main.app({"type": "http", "http_version": "1.1", "method": "GET",
                "path": f"/api/portfolios/{portfolio_id}/report", "root_path": "",
                "query_string": urlencode(query).encode(), "headers": [], "scheme": "http",
                "server": ("test", 80), "client": ("test", 1)}, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"),
                          parse_constant=lambda v: pytest.fail(f"Nonfinite JSON: {v}"))
        if status == 200:
            ReportResponse.model_validate(body)
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


@pytest.mark.parametrize("query", [{}, {"confidence": .625}, {"start_date": "2026-01-06", "end_date": "2026-01-08"}])
def test_prepare_once_exact_adapter_and_section_parity(api, monkeypatch, golden_spy_db, query):
    original = performance_service.prepare_analysis
    captured = []
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        captured.append(result)
        return result
    monkeypatch.setattr(performance_service, "prepare_analysis", prepare)
    status, body = api(**query)
    assert status == 200 and len(captured) == 1
    assert body["performance"] == build_performance_response(captured[0]).model_dump(mode="json")
    assert body["risk"] == build_risk_response(captured[0]).model_dump(mode="json")
    c, pid = golden_spy_db
    c.execute("BEGIN")
    cutoff = body["endpoint"]["valuation_date"]
    assert body["allocation"] == get_allocation_response(c, pid, cutoff).model_dump(mode="json")
    from riskengine.api.holdings_models import HoldingsResponse
    assert body["holdings"] == HoldingsResponse.model_validate(get_holdings_summary(c, pid, cutoff)).model_dump(mode="json")
    assert body["period"] == body["performance"]["period"] == body["risk"]["period"]
    assert body["endpoint"]["invested_value"]["value"] == body["holdings"]["totals"]["market_value"]
    assert body["allocation"]["target_allocation"] is None
    assert not body["allocation"]["reference"]["is_investment_policy_target"]
    assert "attribution" not in body
    c.rollback()


def test_missing_benchmark_preserves_portfolio(api):
    status, b = api(benchmark="ABSENT")
    assert status == 200 and b["status"] == "partial"
    assert b["performance"]["portfolio_metrics"]["cumulative_return"]["value"] is not None
    assert b["risk"]["portfolio_metrics"]["beta"]["value"] is None
    assert b["risk"]["portfolio_metrics"]["beta"]["unavailable_reasons"]
    assert b["endpoint"]["total_equity"]["value"] is not None
    assert b["performance"]["benchmark_metrics"]["cumulative_return"]["value"] is None
    assert b["performance"]["benchmark_metrics"]["cumulative_return"]["unavailable_reasons"]


@pytest.mark.parametrize("zero_cash", [False, True])
def test_healthy_report_is_fully_available_including_zero_cash(api, golden_spy_db, zero_cash):
    c, _ = golden_spy_db
    if zero_cash:
        c.execute("UPDATE cash_flows SET amount=amount-17440")
    status, b = api()
    assert status == 200 and b["status"] == "ok"
    assert b["performance"]["status"] == b["risk"]["status"] == "ok"
    assert b["endpoint"]["cash_balance"] == {
        "value": 0.0 if zero_cash else 17440.0, "unavailable_reasons": []}
    assert b["endpoint"]["total_equity"]["value"] is not None
    assert b["endpoint"]["total_equity"]["unavailable_reasons"] == []
    # Inapplicable self-beta and unsupported policy targets do not degrade status.
    assert b["risk"]["benchmark_metrics"]["beta"]["value"] is None
    assert {r["code"] for r in b["risk"]["benchmark_metrics"]["beta"]["unavailable_reasons"]} == {"not_applicable"}
    assert b["allocation"]["target_allocation"] is None
    assert b["allocation"]["target_unavailable_reasons"]


def test_no_financial_results_is_unavailable_despite_metadata(api, golden_spy_db):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('No results','USD')").lastrowid
    status, b = api(pid)
    assert status == 200 and b["status"] == "unavailable"
    assert b["name"] == "No results" and b["provenance"]["report_methodology"]
    assert b["warnings"] and b["unavailable_reasons"]
    assert b["holdings"] is b["allocation"] is None
    for name in ("total_equity", "invested_value", "cash_balance", "net_contributions", "remaining_average_cost_basis"):
        assert b["endpoint"][name]["value"] is None
        assert b["endpoint"][name]["unavailable_reasons"]
    for section in ("performance", "risk"):
        for side in ("portfolio_metrics", "benchmark_metrics"):
            assert all(m["value"] is None and m["unavailable_reasons"]
                       for m in b[section][side].values())


def test_zero_amounts_alone_count_as_financial_results(api, golden_spy_db):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Zero balances','USD')").lastrowid
    status, b = api(pid, start_date="2026-01-05", end_date="2026-01-09", benchmark="ABSENT")
    assert status == 200 and b["status"] == "partial"
    for name in ("total_equity", "invested_value", "cash_balance", "net_contributions", "remaining_average_cost_basis"):
        assert b["endpoint"][name] == {"value": 0.0, "unavailable_reasons": []}
    assert b["performance"]["portfolio_metrics"]["cumulative_return"]["value"] is None
    assert b["performance"]["portfolio_metrics"]["cumulative_return"]["unavailable_reasons"]
    assert b["performance"]["benchmark_metrics"]["cumulative_return"]["value"] is None
    assert b["allocation"]["current"]["concentration"]["hhi"] is None
    assert b["allocation"]["current"]["unavailable_reasons"]


def test_missing_qqq_endpoint_price_keeps_historical_report_partial(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    path = tmp_path / "missing_qqq.db"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(path)) as copy:
            original.backup(copy)
            copy.execute("DELETE FROM prices WHERE date='2026-11-06' AND "
                         "instrument_id=(SELECT id FROM instruments WHERE ticker='QQQ')")
            copy.commit()
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    status, b = http_get(start_date="2026-11-02", end_date="2026-11-06")
    assert status == 200 and b["status"] == "partial"
    assert b["performance"]["benchmark_metrics"]["cumulative_return"]["value"] is not None
    assert b["endpoint"]["cash_balance"]["value"] == pytest.approx(727.35)
    assert b["endpoint"]["net_contributions"]["value"] == 100000
    assert b["endpoint"]["remaining_average_cost_basis"]["value"] == pytest.approx(99272.65)
    for name in ("total_equity", "invested_value"):
        assert b["endpoint"][name]["value"] is None
        assert b["endpoint"][name]["unavailable_reasons"]
    qqq = next(h for h in b["holdings"]["holdings"] if h["ticker"] == "QQQ")
    assert qqq["market_value"] is None
    assert any(w["ticker"] == "QQQ" and w["code"] == "stale_price" for w in b["holdings"]["warnings"])


@pytest.mark.parametrize("day", ["2026-01-07", "2026-01-09"])
def test_partial_prices_preserve_independent_sections_and_cutoff(api, golden_spy_db, day):
    c, _ = golden_spy_db
    c.execute("DELETE FROM prices WHERE date=? AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (day,))
    status, b = api()
    assert status == 200 and b["status"] == "partial" and b["warnings"]
    assert b["endpoint"]["valuation_date"] == "2026-01-09"
    assert b["performance"]["portfolio_metrics"]["cumulative_return"]["value"] is None
    assert b["performance"]["benchmark_metrics"]["cumulative_return"]["value"] is not None
    assert b["endpoint"]["cash_balance"]["value"] is not None
    if day.endswith("09"):
        assert b["endpoint"]["total_equity"]["value"] is None
        assert b["endpoint"]["invested_value"]["unavailable_reasons"]
        assert b["allocation"]["current"]["concentration"]["hhi"] is None
    else:
        assert b["endpoint"]["total_equity"]["value"] is not None


@pytest.mark.parametrize("table", ["transactions", "cash_flows", "instruments"])
def test_currency_conflicts_are_quality_outcomes(api, golden_spy_db, table):
    c, _ = golden_spy_db
    c.execute(f"UPDATE {table} SET currency='EUR'")
    status, b = api()
    assert status == 200
    assert all(v["value"] is None and v["unavailable_reasons"] for k, v in b["endpoint"].items() if isinstance(v, dict))
    assert b["allocation"]["current"]["invested_value"] is None


@pytest.mark.parametrize("kind", ["corporate_actions", "oversell"])
def test_integrity_conflicts(api, golden_spy_db, kind):
    c, _ = golden_spy_db
    if kind == "corporate_actions":
        c.execute("UPDATE prices SET source='tiingo'")
        c.execute("DELETE FROM corporate_action_coverage")
    else:
        c.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    assert api()[0] == 409


def test_invalid_reference_does_not_invalidate_independent_period(api, golden_spy_db):
    c, _ = golden_spy_db
    c.execute("UPDATE prices SET source='tiingo' WHERE date='2026-01-05'")
    c.execute("DELETE FROM corporate_action_coverage")
    status, b = api(start_date="2026-01-06")
    assert status == 200 and b["status"] == "partial"
    assert b["performance"]["portfolio_metrics"]["cumulative_return"]["value"] is not None
    assert b["allocation"]["reference"]["snapshot"] is None
    assert b["allocation"]["reference"]["unavailable_reasons"]


@pytest.mark.parametrize("query", [{}, {"start_date": "2026-01-05", "end_date": "2026-01-09"}])
def test_empty_portfolio_no_unrelated_date_fallback(api, golden_spy_db, query):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Empty report','USD')").lastrowid
    status, b = api(pid, **query)
    assert status == 200 and b["performance"]["portfolio_intervals"] == []
    if not query:
        assert b["holdings"] is b["allocation"] is None
        assert b["endpoint"]["valuation_date"] is None
        assert b["unavailable_reasons"]
    else:
        assert b["endpoint"]["invested_value"]["value"] == 0
        assert b["allocation"]["current"]["concentration"]["hhi"] is None


def test_non_session_explicit_end_not_silently_shifted(api):
    status, b = api(end_date="2026-01-10")
    assert status == 200
    assert b["period"]["selected_end_date"] == "2026-01-09"
    assert b["endpoint"]["valuation_date"] == b["holdings"]["effective_valuation_date"] == "2026-01-10"
    assert b["endpoint"]["invested_value"]["value"] is None


@pytest.mark.parametrize("query", [{"confidence": v} for v in [0, 1, "nan", "inf", "bad"]] + [
    {"start_date": "2026-02-30"}, {"end_date": "20260105"}, {"benchmark": " "},
    {"start_date": "2026-01-09", "end_date": "2026-01-05"}])
def test_invalid_requests(api, query):
    assert api(**query)[0] == 422


@pytest.mark.parametrize("pid", [0, -1, "bad", 9223372036854775808, 999])
def test_invalid_or_unknown_ids(api, pid):
    assert api(pid)[0] == (404 if pid == 999 else 422)


def test_missing_database(api, monkeypatch, tmp_path):
    monkeypatch.setenv("RISKENGINE_DB", str(tmp_path / "absent.db"))
    assert api()[0] == 404


def test_future_events_do_not_change_explicit_historical_report(api, golden_spy_db):
    c, pid = golden_spy_db
    before = api(end_date="2026-01-09")
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) "
              "VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) "
              "VALUES (?,'2026-01-12','DEPOSIT',100,'EUR','test')", (pid,))
    assert api(end_date="2026-01-09") == before


@pytest.mark.parametrize("failure", [False, True])
def test_readonly_cleanup_and_single_connection(api, monkeypatch, failure):
    opened = []
    original_open, original_prepare = main.get_connection, performance_service.prepare_analysis
    def connect(path, *, read_only):
        assert read_only
        c = original_open(path, read_only=read_only)
        opened.append(c)
        return c
    def prepare(c, *args, **kwargs):
        assert c.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("UPDATE portfolios SET name=name")
        if failure:
            raise RuntimeError("report preparation failed")
        return original_prepare(c, *args, **kwargs)
    monkeypatch.setattr(main, "get_connection", connect)
    monkeypatch.setattr(performance_service, "prepare_analysis", prepare)
    if failure:
        with pytest.raises(RuntimeError, match="report preparation failed"):
            api()
    else:
        assert api()[0] == 200
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = opened[0].in_transaction


def test_snapshot_survives_concurrent_commit(api, golden_spy_db, monkeypatch):
    c, _ = golden_spy_db
    c.commit()
    c.execute("PRAGMA journal_mode=WAL")
    path = c.execute("PRAGMA database_list").fetchone()[2]
    original = report_service.get_holdings_summary
    def holdings(snapshot, *args):
        assert snapshot.in_transaction
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("UPDATE cash_flows SET amount=amount+123")
            writer.commit()
        return original(snapshot, *args)
    monkeypatch.setattr(report_service, "get_holdings_summary", holdings)
    status, b = api()
    assert status == 200
    last = b["performance"]["valuations"][-1]
    assert b["endpoint"]["cash_balance"]["value"] == last["cash_balance"]
    assert b["endpoint"]["net_contributions"]["value"] == last["net_contributions"]
    assert b["allocation"]["current"]["cash_balance"] == last["cash_balance"]


def test_service_requires_transaction(golden_spy_db):
    c, pid = golden_spy_db
    c.commit()
    with pytest.raises(ValueError, match="begin"):
        report_service.get_report_response(c, pid)


def test_historical_reconciliation(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    status, b = http_get()
    assert status == 200
    assert len(b["performance"]["valuations"]) == 716
    assert len(b["risk"]["portfolio_intervals"]) == 715
    assert b["period"]["effective_baseline_date"] == "2024-01-02"
    expected = {"total_equity": 151597.56713256837, "invested_value": 150870.21713256836,
                "cash_balance": 727.3500000000058, "net_contributions": 100000,
                "remaining_average_cost_basis": 99272.65}
    for key, value in expected.items():
        assert b["endpoint"][key]["value"] == value
    assert b["allocation"]["reference"]["snapshot"]["invested_value"] == 99272.65
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        assert b["performance"] == performance_service.get_performance_response(c, 1).model_dump(mode="json")
        assert b["risk"] == performance_service.get_risk_response(c, 1).model_dump(mode="json")
        assert b["allocation"] == get_allocation_response(c, 1, "2026-11-06").model_dump(mode="json")


@pytest.mark.parametrize("case", ["healthy", "missing_endpoint", "non_session", "unavailable_reference"])
def test_point_in_time_reuse_call_dates_and_full_http_parity(monkeypatch, tmp_path, case):
    """Compare the prior standalone composition path with report-local reuse.

    Preparation still values its full window once; these instrument the extra
    point-in-time engines, rather than confusing their count with preparation.
    """
    from riskengine.risk import holdings_service, exposure
    source = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    path = tmp_path / "reuse.db"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(path)) as copy:
            original.backup(copy)
            if case in {"missing_endpoint", "unavailable_reference"}:
                day = "2026-11-06" if case == "missing_endpoint" else "2024-01-02"
                copy.execute("DELETE FROM prices WHERE date=? AND "
                             "instrument_id=(SELECT id FROM instruments WHERE ticker='QQQ')", (day,))
            copy.commit()
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    cutoff = "2026-11-07" if case == "non_session" else "2026-11-06"
    calls, cost_calls, preparations = [], [], []
    original_value = holdings_service.get_portfolio_value
    original_cost = holdings_service.get_cost_basis
    original_prepare = performance_service.prepare_analysis

    def value(connection, pid, day, **kwargs):
        assert connection.in_transaction
        calls.append(day)
        return original_value(connection, pid, day, **kwargs)

    def cost(connection, pid, day):
        cost_calls.append(day)
        return original_cost(connection, pid, day)

    def prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        preparations.append(result)
        return result

    monkeypatch.setattr(holdings_service, "get_portfolio_value", value)
    monkeypatch.setattr(exposure, "get_portfolio_value", value)
    monkeypatch.setattr(holdings_service, "get_cost_basis", cost)
    monkeypatch.setattr(performance_service, "prepare_analysis", prepare)
    allocation = report_service.get_allocation_response

    def standalone_allocation(connection, pid, day, **kwargs):
        # Replay the pre-reuse path: Allocation independently loads Holdings
        # and Exposure, using their unchanged standalone entry points.
        return allocation(connection, pid, day)

    monkeypatch.setattr(report_service, "get_allocation_response", standalone_allocation)
    before = http_get(start_date="2026-11-02", end_date=cutoff)
    expected_before = {
        "healthy": [cutoff, cutoff, cutoff, "2024-01-02", "2024-01-02"],
        "missing_endpoint": [cutoff, cutoff, "2024-01-02", "2024-01-02"],
        "non_session": [cutoff, cutoff, "2024-01-02", "2024-01-02"],
        "unavailable_reference": [cutoff, cutoff, cutoff, "2024-01-02"],
    }[case]
    assert calls == expected_before
    assert cost_calls == [cutoff, cutoff, "2024-01-02"]
    assert len(preparations) == 1
    before_prepared = preparations[0]
    calls.clear()
    cost_calls.clear()
    preparations.clear()
    monkeypatch.setattr(report_service, "get_allocation_response", allocation)
    after = http_get(start_date="2026-11-02", end_date=cutoff)
    assert calls == cost_calls == [cutoff, "2024-01-02"]
    assert len(preparations) == 1
    assert preparations[0] == before_prepared
    assert before == after
    status, body = after
    assert status == 200
    assert body["endpoint"]["valuation_date"] == cutoff
    assert body["holdings"]["effective_valuation_date"] == cutoff
    assert body["allocation"]["current"]["valuation_date"] == cutoff
    if case in {"missing_endpoint", "non_session"}:
        assert body["endpoint"]["invested_value"]["value"] is None
        assert body["endpoint"]["invested_value"]["unavailable_reasons"]
        assert body["period"]["selected_end_date"] == "2026-11-06"
    if case == "unavailable_reference":
        assert body["allocation"]["reference"]["unavailable_reasons"]
        assert body["allocation"]["changes"] is None
