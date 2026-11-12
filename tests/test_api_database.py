import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest
from starlette.requests import Request

from riskengine.api import main


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
    with sqlite3.connect(expected) as connection:
        connection.executescript(
            "CREATE TABLE portfolios (id INTEGER, name TEXT, currency TEXT);"
            "INSERT INTO portfolios VALUES (1, 'Test', 'USD');"
            "CREATE TABLE prices (date TEXT);"
            "INSERT INTO prices VALUES ('2026-01-02');"
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
            request = Request({"type": "http", "app": main.app})
            assert main.portfolio_summary(request)["profit_loss"] == 20.0

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(exercise())

    assert opened == [(expected, True)]
    assert f"module={Path(main.__file__).resolve()}" in caplog.text
    assert f"database={expected} mode={mode} access=read-only" in caplog.text


def test_explicit_database_expands_home(monkeypatch, tmp_path):
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", "~/portfolio.db")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = (tmp_path / "portfolio.db").resolve()
    assert main.select_database() == main.DatabaseSelection(expected, "override")


def test_absolute_database_override(monkeypatch, tmp_path):
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    expected = tmp_path / "explicit.db"
    monkeypatch.setenv("RISKENGINE_DB", str(expected))
    assert main.select_database() == main.DatabaseSelection(expected, "override")
