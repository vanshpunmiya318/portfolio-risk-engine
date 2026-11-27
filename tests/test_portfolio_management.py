from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
import json
from threading import Event

import pytest

from riskengine.api import main
from riskengine.api.mutation_tickets import MutationTickets
from riskengine.db.application_transactions import TransactionError, TransactionExecutor
from riskengine.db.connection import get_connection
from riskengine.db.lifecycle import create_database
from test_local_access import BROWSER_HOST, ORIGIN, issue_cookie, serving


DIRECTORY = "/api/portfolios"
OPERATIONS = DIRECTORY + "/mutations"


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    path = create_database(root)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    monkeypatch.setenv("RISKENGINE_WRITE_DATA_ROOT", str(root))
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    return path


def call(port, cookie, path=DIRECTORY, method="GET", body=None, **overrides):
    headers = {"Host": BROWSER_HOST, "Cookie": cookie, "Origin": ORIGIN, "X-RiskEngine-Request": "1"}
    headers.update(overrides)
    if body is not None:
        headers["Content-Type"] = "application/json"
    with closing(HTTPConnection("127.0.0.1", port, timeout=8)) as client:
        client.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = client.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())


def prepare(port, cookie, name="Example", target=None, currency="USD"):
    data = call(port, cookie)[2]
    body = {"context": data["context"], "target": target, "name": name}
    if target is None:
        body["currency"] = currency
    status, _, ticket = call(port, cookie, OPERATIONS, "POST", body)
    assert status == 201, ticket
    return ticket["ticket"]


def execute(port, cookie, ticket, name="Example", target=None, currency="USD"):
    payload = {"name": name}
    if target is None:
        payload["currency"] = currency
    return call(port, cookie, DIRECTORY if target is None else f"{DIRECTORY}/{target}",
                "POST" if target is None else "PATCH", payload, **{"X-RiskEngine-Ticket": ticket})


def test_directory_create_rename_and_legacy_ids(managed, monkeypatch):
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        status, headers, directory = call(port, cookie)
        assert (status, headers["cache-control"]) == (200, "no-store")
        assert not directory["portfolios"]
        assert directory["management_configured"] and directory["write_availability"] == "available"
        ticket = prepare(port, cookie, "  Caf\u00e9   \u0915\u094d\u200d\u0937  ", currency="EUR")
        created = execute(port, cookie, ticket, "Cafe\u0301 \u0915\u094d\u200d\u0937", currency="eur")[2]
        assert created["outcome"] == "committed"
        assert created["portfolio"] == {"id": 1, "name": "Caf\u00e9 \u0915\u094d\u200d\u0937", "currency": "EUR"}
        assert call(port, cookie, OPERATIONS + "/" + ticket)[2] == created
        assert execute(port, cookie, ticket, "Different")[0] == 409

        for label in ("Renamed", "Renamed"):
            ticket = prepare(port, cookie, label, target=1)
            result = execute(port, cookie, ticket, label, target=1)[2]
            assert result["outcome"] == "committed"
            assert result["portfolio"] == {"id": 1, "name": label, "currency": "EUR"}
        ticket = prepare(port, cookie, "Absent", target=99)
        assert execute(port, cookie, ticket, "Absent", target=99)[2]["reason"] == "Portfolio not found."
        assert call(port, cookie, DIRECTORY + "/0", "PATCH", {"name": "Invalid"})[0] == 422
        assert call(port, cookie, DIRECTORY, "POST", {"name": "X", "currency": "USD", "unexpected": True})[0] == 422

        with closing(get_connection(managed)) as connection, connection:
            connection.executemany("INSERT INTO portfolios VALUES (?, ?, 'GBP')",
                                   [(-2, "Negative"), (9007199254740993, "Large"), (3, "Legacy")])
        listing = call(port, cookie)[2]
        assert [row["id"] for row in listing["portfolios"]] == [1, 3]
        assert listing["portfolios"][1]["currency"] == "GBP"
        assert listing["unsupported"] == [
            {"id": "-2", "reason": "nonpositive"},
            {"id": "9007199254740993", "reason": "outside_client_range"},
        ]
        refused = execute(port, cookie, prepare(port, cookie, "Unrepresentable"), "Unrepresentable")[2]
        assert refused["outcome"] == "rolled_back"
        with closing(get_connection(managed, read_only=True)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM portfolios").fetchone() == (4,)


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "x" * 257, "a\x00", "a\u202e", "\u200d\u200c", "\u034f"])
def test_invalid_names(managed, monkeypatch, name):
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        context = call(port, cookie)[2]["context"]
        assert call(port, cookie, OPERATIONS, "POST", {"name": name, "context": context, "currency": "USD"})[0] == 422
        assert not call(port, cookie)[2]["portfolios"]


def test_proofs_close_and_restart(managed, monkeypatch):
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        context = call(port, cookie)[2]["context"]
        data = {"name": "Name", "context": context, "currency": "USD"}
        assert call(port, "", OPERATIONS, "POST", data)[0] == 401
        assert call(port, cookie, OPERATIONS, "POST", data, Origin="https://elsewhere.invalid")[0] == 403
        assert call(port, cookie, OPERATIONS, "POST", data, **{"X-RiskEngine-Request": ""})[0] == 403
        assert call(port, cookie, OPERATIONS, "POST", data | {"context": "wrong"})[0] == 409
        ticket = prepare(port, cookie)
        path = OPERATIONS + "/" + ticket
        assert call(port, "", path)[0] == 401
        assert call(port, cookie, path + "/close", "POST", **{"X-RiskEngine-Request": ""})[0] == 403
        assert call(port, cookie, path + "/close", "POST")[2]["outcome"] == "not_started"
        assert execute(port, cookie, ticket)[2]["outcome"] == "not_started"
        old = prepare(port, cookie)
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        assert call(port, cookie)[2]["context"] != context
        assert execute(port, cookie, old)[2]["outcome"] == "unavailable"
        assert not call(port, cookie)[2]["portfolios"]


@pytest.mark.parametrize("mode", ["unmanaged", "demo", "historical"])
def test_protected_read_only_routes(managed, monkeypatch, mode):
    if mode == "unmanaged":
        monkeypatch.delenv("RISKENGINE_WRITE_DATA_ROOT")
    elif mode == "demo":
        monkeypatch.setenv("RISKENGINE_DEMO", "1")
    else:
        monkeypatch.delenv("RISKENGINE_DB")
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        _, _, directory = call(port, cookie)
        assert not directory["management_configured"]
        body = {"context": directory["context"], "name": "No write", "currency": "USD"}
        assert call(port, cookie, OPERATIONS, "POST", body)[0] == 403
        assert execute(port, cookie, "a" * 43)[0] == 403
        assert execute(port, cookie, "a" * 43, target=1)[0] == 403


def test_concurrent_claim_normalized_duplicates_and_early_reads(managed, monkeypatch):
    entered, release = Event(), Event()
    original_create = main.create_portfolio

    def delayed_create(transaction, name, currency):
        if name == "Example":
            entered.set()
            assert release.wait(5)
        return original_create(transaction, name, currency)

    monkeypatch.setattr(main, "create_portfolio", delayed_create)
    with serving(monkeypatch) as port, ThreadPoolExecutor(max_workers=2) as pool:
        cookie = issue_cookie(port)
        first = prepare(port, cookie)
        second = prepare(port, cookie, "EXAMPLE")
        future = pool.submit(execute, port, cookie, first)
        try:
            assert entered.wait(4)
            assert not call(port, cookie)[2]["portfolios"]
            path = OPERATIONS + "/" + first
            assert call(port, cookie, path + "/close", "POST")[2]["outcome"] == "running"
            assert execute(port, cookie, first)[2]["outcome"] == "running"
            competitor = pool.submit(execute, port, cookie, second, "EXAMPLE")
        finally:
            release.set()
        committed = future.result()[2]
        assert committed["outcome"] == "committed"
        assert competitor.result()[2]["outcome"] == "rolled_back"
        assert call(port, cookie, path)[2] == committed
        assert len(call(port, cookie)[2]["portfolios"]) == 1


def test_unknown_executor_outcome_is_terminal_and_sanitized(managed, monkeypatch):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise TransactionError("commit_failure", "commit", "unknown") from RuntimeError("Private detail")

    monkeypatch.setattr(TransactionExecutor, "run", fail)
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        ticket = prepare(port, cookie)
        result = execute(port, cookie, ticket)[2]
        assert result["outcome"] == "unknown"
        assert "Private detail" not in result["reason"]
        assert call(port, cookie, OPERATIONS + "/" + ticket)[2] == result
        assert execute(port, cookie, ticket)[2] == result
        assert attempts == [1]


def test_registry_retains_running_entries_and_fences_expired_tickets():
    now = [0]
    registry = MutationTickets(capacity=2, lifetime=5, retention=10, clock=lambda: now[0])
    scope = ("generation", "session", (1, 10))
    command = (None, "Example", "USD")
    first = registry.prepare(scope, command)
    second = registry.prepare(scope, command)
    with pytest.raises(main.HTTPException) as full:
        registry.prepare(scope, command)
    assert full.value.status_code == 503
    assert registry.inspect(first.ticket, ("generation", "other"))[0].outcome == "unavailable"
    assert registry.inspect(first.ticket, ("other", "session"))[0].outcome == "unavailable"
    with pytest.raises(main.HTTPException):
        registry.inspect(first.ticket, scope[:2], command=command, identity=(1, 20))
    assert registry.inspect(first.ticket, scope[:2], command=command, identity=scope[2])[1]
    now[0] = 100
    assert registry.inspect(first.ticket, scope[:2], close=True)[0].outcome == "running"
    assert registry.inspect(second.ticket, scope[:2], command=command, identity=scope[2])[0].outcome == "unavailable"
    registry.finish(main.TicketResult(ticket=first.ticket, outcome="unknown"))
    now[0] = 109
    assert registry.inspect(first.ticket, scope[:2])[0].outcome == "unknown"
    now[0] = 110
    assert registry.inspect(first.ticket, scope[:2])[0].outcome == "unavailable"


def test_replacement_rejects_prepared_target_and_financial_context(managed, monkeypatch, tmp_path):
    replacement = create_database(tmp_path / "replacement")
    with closing(get_connection(replacement)) as database, database:
        database.execute("INSERT INTO portfolios VALUES (1, 'Different database', 'EUR')")
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        previous = call(port, cookie)[2]["context"]
        ticket = prepare(port, cookie)
        replacement.replace(managed)
        current = call(port, cookie)[2]
        assert current["context"] != previous
        assert current["portfolios"][0]["name"] == "Different database"
        assert execute(port, cookie, ticket)[0] == 409
        status, headers, _ = call(port, cookie, DIRECTORY + "/1/transactions",
                                  **{"X-RiskEngine-Database-Context": previous})
        assert (status, headers["x-riskengine-context-changed"]) == (409, "1")
        assert call(port, cookie, OPERATIONS + "/" + ticket + "/close", "POST")[2]["outcome"] == "not_started"
        assert len(call(port, cookie)[2]["portfolios"]) == 1


def test_create_and_rename_share_normalized_validation(managed, monkeypatch):
    entered, release = Event(), Event()
    original_create = main.create_portfolio

    def wait_inside_create(transaction, name, currency):
        entered.set()
        assert release.wait(5)
        return original_create(transaction, name, currency)

    with closing(get_connection(managed)) as connection, connection:
        connection.execute("INSERT INTO portfolios VALUES (1, 'Existing', 'GBP')")
    monkeypatch.setattr(main, "create_portfolio", wait_inside_create)
    with serving(monkeypatch) as port, ThreadPoolExecutor(max_workers=2) as pool:
        cookie = issue_cookie(port)
        create = prepare(port, cookie, "Same name")
        rename = prepare(port, cookie, "SAME NAME", target=1)
        first = pool.submit(execute, port, cookie, create, "Same name")
        try:
            assert entered.wait(4)
            second = pool.submit(execute, port, cookie, rename, "SAME NAME", 1)
        finally:
            release.set()
        assert first.result()[2]["outcome"] == "committed"
        status, _, failure = second.result()
        assert (status, failure["outcome"]) == (409, "rolled_back")
        assert call(port, cookie)[2]["portfolios"][0]["name"] == "Existing"


def test_disconnected_client_can_reconcile_without_resending(managed, monkeypatch):
    from time import monotonic, sleep

    entered, release = Event(), Event()
    original_create = main.create_portfolio

    def wait_inside_create(transaction, name, currency):
        entered.set()
        assert release.wait(5)
        return original_create(transaction, name, currency)

    monkeypatch.setattr(main, "create_portfolio", wait_inside_create)
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        ticket = prepare(port, cookie)
        with closing(HTTPConnection("127.0.0.1", port, timeout=5)) as client:
            client.request("POST", DIRECTORY, json.dumps({"name": "Example", "currency": "USD"}), {
                "Host": BROWSER_HOST, "Origin": ORIGIN, "Cookie": cookie, "Content-Type": "application/json",
                "X-RiskEngine-Request": "1", "X-RiskEngine-Ticket": ticket,
            })
            try:
                assert entered.wait(4)
            finally:
                client.close()
                release.set()
        deadline = monotonic() + 4
        while monotonic() < deadline:
            result = call(port, cookie, OPERATIONS + "/" + ticket)[2]
            if result["outcome"] != "running":
                break
            sleep(0.025)
        assert result["outcome"] == "committed"
        assert result["portfolio"]["id"] == 1
        assert len(call(port, cookie)[2]["portfolios"]) == 1


def test_real_process_restart_fences_both_unclaimed_and_committed_tickets(managed, monkeypatch):
    from contextlib import contextmanager
    import os
    import socket
    import subprocess
    import sys
    from time import monotonic, sleep

    @contextmanager
    def backend():
        with closing(socket.socket()) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = os.environ | {
            "RISKENGINE_API_MODE": "protected", "RISKENGINE_API_PORT": str(port),
            "RISKENGINE_VITE_PORT": BROWSER_HOST.split(":")[1], "RISKENGINE_BOOTSTRAP_CODE": "a" * 43,
        }
        with subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", "riskengine.api.main:app",
             "--host", "127.0.0.1", "--port", str(port), "--no-proxy-headers", "--no-access-log"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as process:
            try:
                deadline = monotonic() + 15
                while monotonic() < deadline and process.poll() is None:
                    try:
                        if call(port, "", "/health")[0] == 200:
                            break
                    except OSError:
                        sleep(0.05)
                else:
                    pytest.fail("Disposable backend did not start")
                yield port
            finally:
                process.terminate()
                process.wait(timeout=10)

    with backend() as port:
        cookie = issue_cookie(port)
        unclaimed = prepare(port, cookie, "Not started")
        committed = prepare(port, cookie)
        assert execute(port, cookie, committed)[2]["outcome"] == "committed"
        old_context = call(port, cookie)[2]["context"]
    with backend() as port:
        cookie = issue_cookie(port)
        for ticket in (unclaimed, committed):
            assert call(port, cookie, OPERATIONS + "/" + ticket)[2]["outcome"] == "unavailable"
        assert execute(port, cookie, unclaimed, "Not started")[2]["outcome"] == "unavailable"
        assert execute(port, cookie, committed)[2]["outcome"] == "unavailable"
        directory = call(port, cookie)[2]
        assert directory["context"] != old_context
        assert [row["name"] for row in directory["portfolios"]] == ["Example"]
