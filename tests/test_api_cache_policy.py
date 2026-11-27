import asyncio
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from starlette.responses import Response

from riskengine.api import main
from test_benchmark import golden_spy_db
from test_golden_portfolio import golden_db


def request(app, target, *, bypass_policy=False, raises=None, root_path="", method="GET"):
    path, _, query = target.partition("?")
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    async def run():
        scope = {
            "type": "http", "method": method, "path": path, "root_path": root_path,
            "query_string": query.encode(), "headers": [], "scheme": "http",
            "http_version": "1.1", "server": ("test", 80), "client": ("test", 1),
        }
        async with app.router.lifespan_context(app):
            if bypass_policy:
                await FastAPI.__call__(app, scope, receive, send)
            else:
                await app(scope, receive, send)

    with pytest.raises(raises) if raises else nullcontext():
        asyncio.run(run())
    start = messages[0]
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], start["headers"], body


@pytest.fixture
def portfolio_db(golden_spy_db, monkeypatch):
    connection, portfolio_id = golden_spy_db
    assert portfolio_id == 1
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", connection.execute("PRAGMA database_list").fetchone()[2])
    return connection


@pytest.mark.parametrize(("method", "suffix", "status"), [
    ("GET", "", 200),
    ("POST", "", 403),
    ("PATCH", "/1", 403),
    ("POST", "/mutations", 403),
    ("GET", "/mutations/" + "a" * 43, 403),
    ("POST", "/mutations/" + "a" * 43 + "/close", 403),
])
def test_legacy_read_only_management_routes(portfolio_db, method, suffix, status):
    before = portfolio_db.execute("SELECT * FROM portfolios").fetchall()
    actual, headers, body = request(main.app, "/api/portfolios" + suffix, method=method)
    assert actual == status
    assert dict(headers)[b"cache-control"] == b"no-store"
    if method == "GET" and not suffix:
        assert b'"management_configured":false' in body
    assert portfolio_db.execute("SELECT * FROM portfolios").fetchall() == before


def assert_no_store(target, expected_status, **kwargs):
    actual = request(main.app, target, **kwargs)
    original = request(main.app, target, bypass_policy=True, **kwargs)
    status, headers, body = actual
    assert status == original[0] == expected_status
    assert body == original[2]
    assert [v for k, v in headers if k == b"cache-control"] == [b"no-store"]
    assert [(k, v) for k, v in headers if k != b"cache-control"] == original[1]
    return dict(headers)


@pytest.mark.parametrize("target", [
    "/api/portfolio/summary",
    "/api/portfolios/1/summary",
    "/api/portfolios/1/holdings",
    "/api/portfolios/1/transactions",
    "/api/portfolios/1/performance",
    "/api/portfolios/1/risk",
    "/api/portfolios/1/allocation",
    "/api/portfolios/1/report",
])
def test_portfolio_json_no_store(portfolio_db, target):
    assert assert_no_store(target, 200)[b"content-type"] == b"application/json"


def test_portfolio_directory_no_store(portfolio_db):
    status, headers, body = request(main.app, "/api/portfolios")
    assert status == 200
    assert dict(headers)[b"cache-control"] == b"no-store"
    assert b'"portfolios":[{"id":1,' in body


@pytest.mark.parametrize(("format", "media_type", "extension"), [
    ("json", b"application/json", b"json"),
    ("markdown", b"text/markdown; charset=utf-8", b"md"),
])
def test_export_no_store_preserves_download(portfolio_db, format, media_type, extension):
    headers = assert_no_store(f"/api/portfolios/1/report/export?format={format}", 200)
    assert headers[b"content-type"] == media_type
    assert headers[b"content-disposition"] == (
        b'attachment; filename="portfolio-1-report-auto-to-auto-asof-2026-01-09.' + extension + b'"'
    )


@pytest.mark.parametrize(("target", "status"), [
    ("/api/portfolios/999/summary", 404),
    ("/api/portfolios/1/no-such-route", 404),
    ("/api/portfolios/0/summary", 422),
    ("/api/portfolios/1/risk?confidence=1", 422),
])
def test_client_errors_no_store(portfolio_db, target, status):
    assert_no_store(target, status)


@pytest.mark.parametrize("condition", ["mixed currency", "missing price"])
def test_valuation_conflicts_no_store(portfolio_db, condition):
    with portfolio_db:
        if condition == "mixed currency":
            portfolio_db.execute(
                "INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) "
                "VALUES(1,'2026-01-05','DEPOSIT',100,'EUR','test')"
            )
        else:
            portfolio_db.execute("DELETE FROM prices WHERE instrument_id = (SELECT id FROM instruments WHERE ticker='AAPL')")
    assert_no_store("/api/portfolios/1/summary", 409)


def test_unhandled_exception_no_store_preserves_default_500(portfolio_db, monkeypatch):
    def fail(*args):
        raise RuntimeError("Unexpected valuation failure")

    monkeypatch.setattr(main, "get_total_portfolio_equity", fail)
    assert_no_store("/api/portfolios/1/summary", 500, raises=RuntimeError)


def test_mounted_portfolio_path_no_store(portfolio_db):
    assert_no_store("/staging/api/portfolio/summary", 200, root_path="/staging")


def test_liveness_and_readiness_unchanged(portfolio_db):
    for path, body in [("/health", b'{"status":"ok"}'), ("/ready", b'{"status":"ready"}')]:
        status, headers, actual = request(main.app, path)
        assert status == 200
        assert actual == body
        assert b"cache-control" not in dict(headers)


@pytest.mark.parametrize("path", ["/assets/app.css", "/api/portfolios-assets/app.css"])
def test_static_responses_keep_existing_cache_policy(path):
    app = main.PortfolioAPI()
    app.state.local_access = False  # Isolated legacy read-contract fixture without lifespan.
    css = b"body { margin: 0; }"

    @app.get(path)
    def stylesheet():
        return Response(css, media_type="text/css", headers={"Cache-Control": "public, max-age=600"})

    status, headers, body = request(app, path)
    assert status == 200
    assert body == css
    assert dict(headers)[b"cache-control"] == b"public, max-age=600"
