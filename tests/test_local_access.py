import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import socket
from threading import Barrier, Thread
from time import monotonic, sleep

import pytest
import uvicorn

from riskengine.api import local_access, main
from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema


CODE = "a" * 43
BROWSER_HOST = "127.0.0.1:15328"
ORIGIN = f"http://{BROWSER_HOST}"
SESSION = "/api/local-session"
FINANCIAL = [
    "/api/portfolio/summary",
    *(f"/api/portfolios/1/{path}" for path in (
        "summary", "holdings", "transactions", "performance", "risk", "allocation", "report",
        "report/export?format=json", "report/export?format=markdown",
    )),
]


@pytest.fixture
def database(tmp_path, monkeypatch):
    database = tmp_path / "private-location" / "portfolio.db"
    database.parent.mkdir()
    create_schema(database)
    with closing(get_connection(database)) as connection, connection:
        connection.execute("INSERT INTO portfolios VALUES (1, 'private portfolio label', 'USD')")
        connection.execute("INSERT INTO cash_flows VALUES (1, 1, '2024-01-02', 'DEPOSIT', 1000, 'USD', 'test')")
        connection.execute(
            "INSERT INTO instruments(id,ticker,name,asset_type,currency,primary_source) "
            "VALUES (1,'SPY','Test benchmark','ETF','USD','test')"
        )
        connection.execute(
            "INSERT INTO prices(instrument_id,date,open,high,low,close,adjusted_close,source,fetched_at) "
            "VALUES (1,'2024-01-02',100,100,100,100,100,'test','2024-01-02T00:00:00Z')"
        )
    monkeypatch.setenv("RISKENGINE_DB", str(database))
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    return database


@contextmanager
def serving(monkeypatch, browser_host=BROWSER_HOST):
    with closing(socket.socket()) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        monkeypatch.setenv("RISKENGINE_API_MODE", "protected")
        monkeypatch.setenv("RISKENGINE_API_PORT", str(port))
        monkeypatch.setenv("RISKENGINE_VITE_PORT", browser_host.split(":")[1])
        monkeypatch.setenv("RISKENGINE_BOOTSTRAP_CODE", CODE)
        server = uvicorn.Server(uvicorn.Config(
            main.app, host="127.0.0.1", port=port, lifespan="on", proxy_headers=False, access_log=False,
        ))
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            until = monotonic() + 5
            while not server.started and worker.is_alive() and monotonic() < until:
                sleep(0.01)
            assert server.started
            yield port
        finally:
            server.should_exit = True
            worker.join(5)
            assert not worker.is_alive()


def request(port, path, method="GET", **headers):
    with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as client:
        client.request(method, path, headers=headers)
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()


def launch(port, method="POST", code=CODE, **overrides):
    headers = {"Host": BROWSER_HOST, "Origin": ORIGIN, "X-RiskEngine-Bootstrap": code}
    return request(port, SESSION, method, **(headers | overrides))


def issue_cookie(port, host=BROWSER_HOST):
    status, headers, _ = launch(port, Host=host, Origin=f"http://{host}")
    assert status == 204
    fields = headers["set-cookie"].split("; ")
    assert fields[0].startswith(f"riskengine_local_session_{host.split(':')[1]}=")
    assert {"HttpOnly", "Path=/api", "SameSite=strict"}.issubset(fields)
    assert not any(field.startswith("Domain=") for field in fields)
    return fields[0]


def assert_browser_confirmed(access):
    until = monotonic() + 2
    while not access.browser_confirmed.is_set() and monotonic() < until:
        sleep(0.01)
    assert access.browser_confirmed.is_set()


@pytest.mark.parametrize("environment", [
    {},
    {"RISKENGINE_API_MODE": "unknown"},
    {"RISKENGINE_BOOTSTRAP_CODE": "short"},
    {"RISKENGINE_BOOTSTRAP_CODE": CODE, "RISKENGINE_API_PORT": "65536"},
])
def test_protected_startup_fails_closed(monkeypatch, environment):
    for name in ("RISKENGINE_API_MODE", "RISKENGINE_BOOTSTRAP_CODE", "RISKENGINE_API_PORT", "RISKENGINE_VITE_PORT"):
        monkeypatch.delenv(name, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    async def start():
        with pytest.raises(RuntimeError, match="configuration"):
            async with main.app.router.lifespan_context(main.app):
                pytest.fail("Unconfigured protected app started")

    asyncio.run(start())


def test_host_validation_and_public_probes(database, monkeypatch):
    with serving(monkeypatch) as port:
        for host in (f"127.0.0.1:{port}", BROWSER_HOST):
            for path, expected in (("/health", {"status": "ok"}), ("/ready", {"status": "ready"})):
                status, _, body = request(port, path, Host=host)
                assert status == 200 and json.loads(body) == expected
                assert b"private-location" not in body and CODE.encode() not in body
        for host in ("localhost:15328", "[::1]:15328", "127.0.0.1:1", "evil.example:15328",
                     "127.0.0.1.evil.example:15328", "127.0.0.1:15328,evil.example", "127.0.0.1"):
            assert request(port, "/health", Host=host)[0] == 400
        for name in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-For", "X-Forwarded-Proto"):
            assert request(port, "/ready", **{name: BROWSER_HOST})[0] == 400
        with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as client:
            client.putrequest("GET", "/health", skip_host=True)
            client.putheader("Host", BROWSER_HOST)
            client.putheader("Host", "evil.example")
            client.endheaders()
            assert client.getresponse().status == 400
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
            assert request(port, path)[0] == 404


def test_financial_routes_require_session_before_processing(database, monkeypatch, caplog):
    with serving(monkeypatch) as port:
        for path in FINANCIAL:
            for cookie in ("", "riskengine_local_session_15328=" + "q" * 43):
                status, response_headers, data = request(port, path, Cookie=cookie)
                assert status == 401 and response_headers["cache-control"] == "no-store"
                assert b"private portfolio label" not in data and CODE.encode() not in data
        assert launch(port, method="HEAD")[0] == 204
        assert launch(port, code="z" * 43)[0] == 401
        session = issue_cookie(port)
        assert launch(port)[0] == 401
        assert request(port, SESSION, Cookie=session)[0] == 200
        status, headers, data = request(port, FINANCIAL[0], Host=BROWSER_HOST, Cookie=session)
        assert status == 200 and headers["cache-control"] == "no-store"
        assert json.loads(data)["name"] == "private portfolio label"
        for fmt in ("json", "markdown"):
            status, headers, _ = request(port, f"/api/portfolios/1/report/export?format={fmt}", Cookie=session)
            assert status == 200 and headers["content-disposition"].startswith("attachment;")
            assert headers["cache-control"] == "no-store"
    assert CODE not in caplog.text and session.split("=", 1)[1] not in caplog.text


def test_origin_and_csrf_guard(database, monkeypatch):
    with serving(monkeypatch) as port:
        session = issue_cookie(port)
        headers = {"Host": BROWSER_HOST, "Cookie": session}
        for origin in ("null", "https://unrelated.example", "http://127.0.0.1:15411",
                       ORIGIN + "/", f"{ORIGIN}, {ORIGIN}"):
            assert request(port, FINANCIAL[0], **headers, Origin=origin)[0] == 403
        assert request(port, FINANCIAL[0], **headers, **{"Sec-Fetch-Site": "same-site"})[0] == 403
        assert request(port, FINANCIAL[0], **headers, **{"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert request(port, FINANCIAL[0], **headers, Origin=ORIGIN)[0] == 200
        assert request(port, FINANCIAL[0], **headers)[0] == 200
        assert request(port, FINANCIAL[0], Origin=ORIGIN, Host=BROWSER_HOST)[0] == 401
        assert launch(port, Origin="null")[0] == 403
        preflight = request(port, SESSION, "OPTIONS", Host=BROWSER_HOST, Origin="https://other.example",
                            **{"Access-Control-Request-Method": "POST"})
        assert preflight[0] == 403 and "access-control-allow-origin" not in preflight[1]
        assert request(port, FINANCIAL[0], "POST", **headers)[0] == 403
        assert request(port, FINANCIAL[0], "POST", **headers, Origin=ORIGIN)[0] == 403
        assert request(port, FINANCIAL[0], "POST", **headers, Origin=ORIGIN,
                       **{"X-RiskEngine-Request": "1"})[0] == 405


def test_expiry_restart_and_session_replay(database, monkeypatch):
    clock = [500.0]
    monkeypatch.setattr(local_access, "monotonic", lambda: clock[0])
    with serving(monkeypatch) as port:
        clock[0] += local_access.BOOTSTRAP_SECONDS
        assert launch(port)[0] == 401
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        assert request(port, SESSION, Cookie=cookie)[0] == 200
        assert request(port, SESSION, Cookie=cookie)[0] == 200  # Shares a session across tabs.
        clock[0] += local_access.SESSION_SECONDS
        assert request(port, SESSION, Cookie=cookie)[0] == 401
    with serving(monkeypatch) as port:
        assert request(port, SESSION, Cookie=cookie)[0] == 401


def test_only_authenticated_browser_check_confirms_current_startup(database, monkeypatch):
    with serving(monkeypatch) as port:
        access = main.app.state.local_access
        assert launch(port, method="HEAD")[0] == 204
        assert not access.browser_confirmed.is_set()
        assert request(port, SESSION, Host=BROWSER_HOST)[0] == 401
        cookie = issue_cookie(port)
        assert request(port, SESSION, Cookie=cookie)[0] == 200
        assert not access.browser_confirmed.is_set()
        status, _, body = request(port, SESSION, Cookie=cookie, Host=BROWSER_HOST)
        assert status == 200 and json.loads(body)["generation"] == access.generation
        assert_browser_confirmed(access)
    with serving(monkeypatch) as port:
        restarted = main.app.state.local_access
        assert restarted.generation != access.generation
        assert request(port, SESSION, Cookie=cookie, Host=BROWSER_HOST)[0] == 401
        assert not restarted.browser_confirmed.is_set()


def test_concurrent_bootstrap_grants_one_session(database, monkeypatch):
    with serving(monkeypatch) as port:
        access = main.app.state.local_access
        barrier = Barrier(2, timeout=5)

        def exchange():
            barrier.wait()
            return launch(port)

        with ThreadPoolExecutor(2) as executor:
            first, second = (executor.submit(exchange) for _ in range(2))
            outcomes = sorted((first.result(), second.result()), key=lambda result: result[0])
        winner, loser = outcomes
        assert winner[0] == 204 and loser[0] == 401
        assert "set-cookie" not in loser[1] and not access.browser_confirmed.is_set()
        assert request(port, SESSION, Host=BROWSER_HOST)[0] == 401
        assert not access.browser_confirmed.is_set()
        cookie = winner[1]["set-cookie"].split(";", 1)[0]
        assert request(port, SESSION, Host=BROWSER_HOST, Cookie=cookie)[0] == 200
        assert_browser_confirmed(access)
        assert launch(port)[0] == 401


def test_sessions_use_configured_cookie_namespace(database, monkeypatch):
    other_host = "127.0.0.1:15329"
    with serving(monkeypatch) as port:
        first = issue_cookie(port)
    with serving(monkeypatch, other_host) as port:
        other = issue_cookie(port, other_host)
        assert first.split("=")[0] != other.split("=")[0]
        assert request(port, SESSION, Host=other_host, Cookie=first)[0] == 401
        assert request(port, SESSION, Host=other_host, Cookie=f"{first}; {other}")[0] == 200
