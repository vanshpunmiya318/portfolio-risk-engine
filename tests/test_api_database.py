import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from riskengine.api import main
from riskengine.db.schema.loader import create_schema


async def get(path):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await main.app({
        "type": "http", "method": "GET", "path": path, "root_path": "", "query_string": b"",
        "headers": [], "scheme": "http", "http_version": "1.1",
        "server": ("test", 80), "client": ("test", 1),
    }, receive, send)
    return messages[0]["status"], json.loads(messages[1]["body"])


@pytest.mark.parametrize(
    "demo,override,relative_path,mode",
    [
        (None, None, "data/historical_portfolio.db", "historical"),
        ("0", None, "data/historical_portfolio.db", "historical"),
        ("1", None, "data/demo_portfolio.db", "demo"),
        (None, "custom.db", "custom.db", "override"),
        ("0", "custom.db", "custom.db", "override"),
        ("1", "custom.db", "data/demo_portfolio.db", "demo"),
    ],
)
def test_startup_and_endpoint_use_same_database(
    monkeypatch, tmp_path, caplog, demo, override, relative_path, mode
):
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    for key, value in (("RISKENGINE_DEMO", demo), ("RISKENGINE_DB", override)):
        monkeypatch.delenv(key, raising=False)
        if value is not None:
            monkeypatch.setenv(key, value)

    expected = (tmp_path / relative_path).resolve()
    expected.parent.mkdir(parents=True, exist_ok=True)
    create_schema(expected)
    with sqlite3.connect(expected) as connection:
        connection.executescript(
            "INSERT INTO portfolios VALUES (1, 'Test', 'USD');"
            "INSERT INTO instruments (id, ticker, name, asset_type, currency, primary_source) "
            "VALUES (1, 'TEST', 'Test instrument', 'STOCK', 'USD', 'test');"
            "INSERT INTO prices (instrument_id, date, open, high, low, close, adjusted_close, source, fetched_at) "
            "VALUES (1, '2026-01-02', 120, 120, 120, 120, 120, 'test', '2026-01-02T00:00:00Z');"
        )

    opened = []
    original_get_connection = main.get_connection

    def record_connection(path, *, read_only):
        opened.append((path, read_only))
        return original_get_connection(path, read_only=read_only)

    monkeypatch.setattr(main, "get_connection", record_connection)
    monkeypatch.setattr(main, "get_total_portfolio_equity", lambda *args: 120.0)
    monkeypatch.setattr(main, "get_net_cash_flows", lambda *args: 100.0)

    async def exercise():
        async with main.app.router.lifespan_context(main.app):
            assert main.app.state.database.path == expected
            assert main.app.state.database.mode == mode
            # Changing the environment cannot make requests diverge from startup logs.
            monkeypatch.setenv("RISKENGINE_DB", "different.db")
            monkeypatch.setenv("RISKENGINE_DEMO", "0")
            assert await get("/ready") == (200, {"status": "ready"})
            request = Request({"type": "http", "app": main.app})
            assert main.portfolio_summary(request)["profit_loss"] == 20.0

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(exercise())

    assert opened == [(expected, True), (expected, True)]
    assert f"module={Path(main.__file__).resolve()}" in caplog.text
    assert f"database={expected} mode={mode} access=read-only" in caplog.text


def test_explicit_database_expands_home(monkeypatch, tmp_path):
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", "~/portfolio.db")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = (tmp_path / "portfolio.db").resolve()
    assert main.select_database() == main.DatabaseSelection(expected, "override")


@pytest.fixture
def readiness_db(monkeypatch, tmp_path):
    path = tmp_path / "readiness.db"
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    return path


def probe_readiness():
    async def exercise():
        async with main.app.router.lifespan_context(main.app):
            assert await get("/health") == (200, {"status": "ok"})
            return await get("/ready")

    return asyncio.run(exercise())


@pytest.mark.parametrize("usable", [True, False])
def test_ready_closes_readonly_connection(readiness_db, monkeypatch, usable):
    create_schema(readiness_db)
    if not usable:
        with closing(sqlite3.connect(readiness_db)) as connection:
            connection.execute("DROP TABLE prices")
    original = main.get_connection
    connections = []

    def record_connection(path, *, read_only):
        assert read_only and path == readiness_db
        connection = Mock(wraps=original(path, read_only=read_only))
        connections.append(connection)
        return connection

    monkeypatch.setattr(main, "get_connection", record_connection)
    before = readiness_db.read_bytes()
    expected = (200, {"status": "ready"}) if usable else (503, {"detail": "Configured database is not ready"})
    assert probe_readiness() == expected
    assert readiness_db.read_bytes() == before
    assert len(connections) == 1
    connections[0].close.assert_called_once_with()


@pytest.mark.parametrize("condition", ["missing", "directory", "invalid", "missing table", "missing column"])
def test_unusable_database_returns_generic_503(readiness_db, condition):
    if condition == "directory":
        readiness_db.mkdir()
    elif condition == "invalid":
        readiness_db.write_bytes(b"invalid database contents")
    elif condition in {"missing table", "missing column"}:
        create_schema(readiness_db)
        with closing(sqlite3.connect(readiness_db)) as connection:
            connection.execute("DROP TABLE corporate_action_coverage")
            if condition == "missing column":
                connection.execute("CREATE TABLE corporate_action_coverage (instrument_id INTEGER)")

    assert probe_readiness() == (503, {"detail": "Configured database is not ready"})
    if condition == "missing":
        assert not readiness_db.exists()


def test_ready_sanitizes_connection_failure(readiness_db, monkeypatch):
    def fail(path, *, read_only):
        assert path == readiness_db and read_only
        raise PermissionError(f"Internal diagnostics for {path}")

    monkeypatch.setattr(main, "get_connection", fail)
    assert probe_readiness() == (503, {"detail": "Configured database is not ready"})


@pytest.mark.parametrize(("table", "column"), [
    ("instruments", "name"),
    ("instruments", "sector"),
    ("transactions", "id"),
    ("transactions", "source"),
    ("cash_flows", "id"),
    ("cash_flows", "source"),
    ("prices", "open"),
    ("prices", "high"),
    ("prices", "low"),
    ("prices", "volume"),
    ("prices", "fetched_at"),
    ("corporate_actions", "id"),
])
def test_ready_rejects_missing_columns_used_by_serving_queries(readiness_db, table, column):
    create_schema(readiness_db)
    with closing(sqlite3.connect(readiness_db)) as connection:
        # Rebuild the empty table because SQLite cannot drop a primary-key column in place.
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        assert column in columns
        remaining = ", ".join(name for name in columns if name != column)
        connection.executescript(f"""
            CREATE TABLE replacement AS SELECT {remaining} FROM {table} LIMIT 0;
            DROP TABLE {table};
            ALTER TABLE replacement RENAME TO {table};
        """)

    assert probe_readiness() == (503, {"detail": "Configured database is not ready"})


def test_absolute_database_override(monkeypatch, tmp_path):
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    expected = tmp_path / "explicit.db"
    monkeypatch.setenv("RISKENGINE_DB", str(expected))
    assert main.select_database() == main.DatabaseSelection(expected, "override")
