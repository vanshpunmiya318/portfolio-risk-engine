import asyncio
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

import pandas as pd
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from riskengine.api import main
from riskengine.api.analysis_models import PerformanceResponse
from riskengine.risk import performance_service
from riskengine.risk.performance import calculate_performance_metrics


def http_get(portfolio_id=1, **query):
    async def execute():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        async with main.app.router.lifespan_context(main.app):
            await main.app({
                "type": "http", "http_version": "1.1", "method": "GET",
                "path": f"/api/portfolios/{portfolio_id}/performance", "root_path": "",
                "query_string": urlencode(query).encode(), "headers": [],
                "scheme": "http", "server": ("test", 80), "client": ("test", 1),
            }, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        body = json.loads(raw, parse_constant=lambda value: pytest.fail(f"Non-finite JSON: {value}"))
        if status == 200:
            PerformanceResponse.model_validate(body)
        return status, body

    return asyncio.run(execute())


@pytest.fixture
def api(golden_spy_db, monkeypatch):
    connection, pid = golden_spy_db
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", path)

    def call(portfolio_id=pid, **query):
        connection.commit()
        return http_get(portfolio_id, **query)

    return call


def test_complete_performance_default_window_and_existing_calculations(api):
    status, body = api()
    assert status == 200 and body["status"] == "ok"
    assert body["period"]["requested_start_date"] is None
    assert body["period"]["requested_end_date"] is None
    assert body["period"]["effective_baseline_date"] == "2026-01-05"
    assert body["period"]["first_return_end_date"] == "2026-01-06"
    assert body["period"]["effective_end_date"] == "2026-01-09"
    assert body["portfolio_coverage"]["expected_sessions"] == 5
    assert body["portfolio_coverage"]["complete"]
    assert body["benchmark_coverage"]["complete"]
    assert body["comparison_eligible"]
    assert len(body["wealth_index"]["portfolio"]["points"]) == 5
    assert body["wealth_index"]["portfolio"]["points"][0] == {"date": "2026-01-05", "value": 1.0}
    returns = pd.Series([r["return_value"] for r in body["portfolio_intervals"]])
    expected = calculate_performance_metrics(returns, initial_capital=1.)
    assert body["portfolio_metrics"]["cumulative_return"]["value"] == expected["cumulative_return"]
    assert body["portfolio_metrics"]["annualized_return"]["value"] == expected["annualized_return"]
    provenance = json.loads(body["methodology"]["calendar_source"])
    assert provenance["provider"] == "exchange_calendars" and provenance["calendar"] == "XNYS"


def test_explicit_window_includes_baseline_and_both_boundaries(api):
    status, body = api(start_date="2026-01-06", end_date="2026-01-08", benchmark=" spy ")
    assert status == 200
    assert body["benchmark_ticker"] == "SPY"
    assert body["period"]["requested_start_date"] == "2026-01-06"
    assert body["period"]["effective_baseline_date"] == "2026-01-06"
    assert body["period"]["effective_end_date"] == "2026-01-08"
    assert len(body["portfolio_intervals"]) == 2


def test_weekend_boundary_padding_does_not_invent_sessions(api):
    status, body = api(start_date="2026-01-04", end_date="2026-01-10")
    assert status == 200
    assert body["period"]["effective_baseline_date"] == "2026-01-05"
    assert body["period"]["effective_end_date"] == "2026-01-09"
    assert body["portfolio_coverage"]["calendar_verified"]


def test_omitted_bounds_use_portfolio_not_benchmark_or_global_prices(golden_spy_db, api, monkeypatch):
    c, _ = golden_spy_db
    c.execute("DELETE FROM prices WHERE date='2026-01-05' AND instrument_id=(SELECT id FROM instruments WHERE ticker='SPY')")
    c.execute("UPDATE prices SET date='2030-01-02' WHERE date='2026-01-09' AND instrument_id=(SELECT id FROM instruments WHERE ticker='SPY')")
    original = performance_service.verified_nyse_calendar
    bounds = []

    def record(start, end):
        bounds.append((start.isoformat(), end.isoformat()))
        return original(start, end)

    monkeypatch.setattr(performance_service, "verified_nyse_calendar", record)
    status, body = api()
    assert status == 200
    assert bounds == [("2026-01-05", "2026-01-09")]
    assert body["period"]["effective_baseline_date"] == "2026-01-05"
    assert body["period"]["effective_end_date"] == "2026-01-09"
    assert body["portfolio_metrics"]["cumulative_return"]["value"] is not None
    assert body["comparison"]["excess_cumulative_return"]["value"] is None


def test_missing_benchmark_is_structured_200_with_portfolio_metrics(api):
    status, body = api(benchmark="NO_SUCH_BENCHMARK")
    assert status == 200 and body["status"] == "partial"
    assert body["portfolio_metrics"]["annualized_return"]["value"] is not None
    assert body["benchmark_metrics"]["annualized_return"]["value"] is None
    assert body["benchmark_metrics"]["annualized_return"]["unavailable_reasons"]
    assert "benchmark_unavailable" in {issue["code"] for issue in body["warnings"]}


def test_missing_active_price_returns_null_metrics_and_no_chart_bridge(golden_spy_db, api):
    c, _ = golden_spy_db
    c.execute("DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    status, body = api()
    assert status == 200
    assert body["portfolio_metrics"]["cumulative_return"]["value"] is None
    assert body["portfolio_metrics"]["cumulative_return"]["unavailable_reasons"]
    assert not body["wealth_index"]["portfolio"]["points"]
    assert body["benchmark_metrics"]["cumulative_return"]["value"] is not None
    assert "missing_price" in {issue["code"] for issue in body["warnings"]}


def test_missing_first_price_preserves_missing_baseline(golden_spy_db, api):
    c, _ = golden_spy_db
    c.execute("DELETE FROM prices WHERE date='2026-01-05' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    status, body = api()
    assert status == 200
    assert body["period"]["selected_start_date"] == "2026-01-05"
    assert body["period"]["effective_baseline_date"] is None
    assert body["portfolio_metrics"]["cumulative_return"]["value"] is None


def test_empty_portfolio_has_no_clock_or_benchmark_based_period(golden_spy_db, api, monkeypatch):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Empty API','USD')").lastrowid
    monkeypatch.setattr(performance_service, "verified_nyse_calendar", lambda *args: pytest.fail("Empty portfolio must not invent calendar bounds"))
    status, body = api(pid)
    assert status == 200 and body["status"] == "unavailable"
    assert body["period"]["effective_baseline_date"] is None
    assert body["valuations"] == body["portfolio_intervals"] == []


def test_cash_only_portfolio_uses_cash_flow_history(api, golden_spy_db):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Cash only','USD')").lastrowid
    c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,'2026-01-06','DEPOSIT',1000,'USD','test')", (pid,))
    status, body = api(pid)
    assert status == 200
    assert body["period"]["effective_baseline_date"] == "2026-01-06"
    assert body["period"]["effective_end_date"] == "2026-01-06"
    assert not body["portfolio_intervals"]


@pytest.mark.parametrize("query", [
    {"start_date": "2026-02-30"}, {"start_date": "20260105"},
    {"end_date": "2026-01-05T00:00:00"},
    {"start_date": "2026-01-09", "end_date": "2026-01-05"},
    {"benchmark": " "}, {"benchmark": ""}, {"start_date": "0001-01-01"},
])
def test_invalid_query_inputs_are_422(api, query):
    assert api(**query)[0] == 422


@pytest.mark.parametrize("portfolio_id", [0, -1, "abc", 9223372036854775808])
def test_invalid_portfolio_ids_are_422(api, portfolio_id):
    assert api(portfolio_id)[0] == 422


def test_unknown_portfolio_is_404(api):
    assert api(999)[0] == 404


def test_missing_database_is_404(api, monkeypatch, tmp_path):
    monkeypatch.setenv("RISKENGINE_DB", str(tmp_path / "absent.db"))
    assert api()[0] == 404


def test_genuine_position_conflict_is_409_with_structured_issues(api, golden_spy_db):
    c, _ = golden_spy_db
    c.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    status, body = api()
    assert status == 409
    assert body["detail"][0]["code"] == "invalid_positions"
    assert body["detail"][0]["category"] == "integrity"


def test_future_invalid_transactions_do_not_invalidate_explicit_history(api, golden_spy_db):
    c, pid = golden_spy_db
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    status, body = api(end_date="2026-01-09")
    assert status == 200
    assert body["portfolio_metrics"]["cumulative_return"]["value"] is not None


@pytest.mark.parametrize("failure", [None, "not_found", "integrity", "input", "unexpected"])
def test_read_only_transaction_and_connection_cleanup(api, golden_spy_db, monkeypatch, failure):
    c, _ = golden_spy_db
    if failure == "integrity":
        c.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    opened = []
    original_open = main.get_connection
    original_prepare = performance_service.prepare_analysis
    prepared = []

    def tracked_open(path, *, read_only):
        assert read_only is True
        connection = original_open(path, read_only=read_only)
        opened.append(connection)
        return connection

    def checked_prepare(connection, *args, **kwargs):
        assert connection.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE portfolios SET name=name")
        prepared.append(True)
        if failure == "unexpected":
            raise RuntimeError("unexpected preparation failure")
        return original_prepare(connection, *args, **kwargs)

    monkeypatch.setattr(main, "get_connection", tracked_open)
    monkeypatch.setattr(performance_service, "prepare_analysis", checked_prepare)
    if failure == "unexpected":
        with pytest.raises(RuntimeError, match="unexpected preparation"):
            api()
    else:
        query = {"start_date": "0001-01-01"} if failure == "input" else {}
        status, _ = api(999 if failure == "not_found" else 1, **query)
        assert status == ({None: 200, "not_found": 404, "integrity": 409, "input": 422}[failure])
    assert len(opened) == 1
    if failure not in {"not_found", "input"}:
        assert prepared
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = opened[0].in_transaction


def test_actual_historical_full_performance_api(monkeypatch, request):
    path = Path(__file__).resolve().parents[1] / "data" / "historical_portfolio.db"
    if not path.is_file():
        pytest.skip("Historical database is not present")
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    status, body = http_get()
    assert status == 200 and body["status"] == "ok"
    assert body["name"] == "Historical Diversified ETF Portfolio"
    assert body["period"]["effective_baseline_date"] == "2024-01-02"
    assert body["period"]["first_return_end_date"] == "2024-01-03"
    assert body["period"]["effective_end_date"] == "2026-11-06"
    assert body["portfolio_coverage"]["expected_sessions"] == 716
    assert len(body["portfolio_intervals"]) == 715
    assert len(body["wealth_index"]["portfolio"]["points"]) == 716
    assert body["portfolio_coverage"]["complete"] and body["benchmark_coverage"]["complete"]
    assert body["valuations"][-1]["total_equity"] == 151597.56713256837
    expected_values = {"cumulative_return": .5159756713256836, "annualized_return": .15793594278891598,
                       "annualized_volatility": .13777583564567764, "sharpe_ratio": 1.13333948032402,
                       "maximum_drawdown": .15156451190550635}
    returns = pd.Series([item["return_value"] for item in body["portfolio_intervals"]])
    reference = calculate_performance_metrics(returns, initial_capital=1.)
    for metric, value in expected_values.items():
        assert body["portfolio_metrics"][metric]["value"] == value
        assert reference["max_drawdown" if metric == "maximum_drawdown" else metric] == value
    assert body["benchmark_metrics"]["cumulative_return"]["value"] == .5556754004598383
    request.node.user_properties.append(("historical_performance_summary", {
        "http_status": status, "name": body["name"], "period": body["period"],
        "portfolio_metrics": {key: item["value"] for key, item in body["portfolio_metrics"].items()},
        "benchmark_cumulative_return": body["benchmark_metrics"]["cumulative_return"]["value"],
        "final_equity": body["valuations"][-1]["total_equity"],
    }))
