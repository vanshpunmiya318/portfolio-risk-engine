import asyncio
from contextlib import closing
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from time import monotonic, sleep

import pytest

from riskengine.api import main
from riskengine.db import application_writes as writes
from riskengine.db.connection import get_read_only_connection
from riskengine.db.lifecycle import adopt_database, create_database
from riskengine.db.schema.loader import create_schema


def authorize(path, root, *, mode="override", protected=True):
    return writes.ApplicationWrites(
        path, mode, protected=protected, data_root=str(root) if root is not None else None,
    )


def denied(boundary, reason):
    with pytest.raises(writes.ApplicationWriteError) as failure, boundary.connect():
        pytest.fail("Unexpected writable connection")
    assert failure.value.reason == reason
    assert str(boundary.selected) not in str(failure.value)


@pytest.fixture
def managed(tmp_path):
    return create_database(tmp_path / "Managed # % café")


def test_default_provisioning_and_connection_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = create_database()
    boundary = authorize(path, "default")
    with boundary.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM portfolios").fetchone() == (0,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (2000,)
        assert connection.isolation_level is None
        connection.execute("INSERT INTO portfolios VALUES (1, 'Test', 'USD')")
        assert not connection.in_transaction
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    for _ in range(2):
        with boundary.connect() as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                connection.execute(
                    "INSERT INTO cash_flows VALUES (1, 99, '2025-01-01', 'DEPOSIT', 10, 'USD', 'test')"
                )
            connection.execute("BEGIN")
            connection.execute("INSERT INTO portfolios VALUES (2, 'Rolled back', 'USD')")
    with closing(get_read_only_connection(path)) as connection:
        assert connection.execute("SELECT name FROM portfolios").fetchall() == [("Test",)]
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM portfolios")


def test_adopted_database_with_records(tmp_path):
    source = tmp_path / "source.db"
    create_schema(source)
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("INSERT INTO portfolios VALUES (1, 'Preserved', 'USD')")
    original = source.read_bytes()
    adopted = adopt_database(source, tmp_path / "Isolated # % café")
    denied(authorize(source, adopted.parent), "ineligible_target")
    with authorize(adopted, adopted.parent).connect() as connection:
        assert connection.execute("SELECT name FROM portfolios").fetchall() == [("Preserved",)]
        connection.execute("INSERT INTO portfolios VALUES (2, 'New', 'USD')")
    assert source.read_bytes() == original


@pytest.mark.parametrize("root", [None, "", "relative", r"\\server\share", r"\\?\C:\managed"])
def test_invalid_or_absent_opt_in(managed, root):
    denied(authorize(managed, root), "ineligible_target")


def test_selection_denials(managed, tmp_path):
    denied(authorize(managed, managed.parent, protected=False), "operating_mode")
    denied(authorize(managed, managed.parent, mode="demo"), "demo")
    denied(authorize(managed, managed.parent, mode="historical"), "protected_target")
    other = create_database(tmp_path / "Unauthorized")
    denied(authorize(other, managed.parent), "ineligible_target")
    denied(authorize(tmp_path / "missing.db", managed.parent), "ineligible_target")
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("reason", ["operating_mode", "demo", "ineligible_target", None])
def test_direct_construction_cannot_supply_a_managed_grant(managed, reason):
    before = managed.read_bytes()
    with pytest.raises(TypeError):
        writes.ApplicationWrites(managed, managed, reason)
    with pytest.raises(TypeError):
        writes.ApplicationWrites(selected=managed, managed=managed, denial_reason=reason)
    assert managed.read_bytes() == before


def test_missing_startup_configuration_cannot_authorize_external_database(managed):
    before = managed.read_bytes()
    with pytest.raises(TypeError):
        writes.ApplicationWrites()
    with pytest.raises(TypeError):
        writes.ApplicationWrites(managed)
    denied(writes.ApplicationWrites(managed, "override", protected=True, data_root=None), "ineligible_target")
    assert managed.read_bytes() == before


def test_validated_grant_is_immutable(managed):
    boundary = authorize(managed, managed.parent)
    with pytest.raises(FrozenInstanceError):
        boundary.managed = managed.with_name("other.db")
    with boundary.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_hot_journal_requires_controlled_recovery(managed, tmp_path):
    baseline = "a" * 1_000_000
    with closing(sqlite3.connect(managed)) as connection, connection:
        connection.execute("INSERT INTO portfolios VALUES (1, ?, 'USD')", (baseline,))
    committed_bytes = managed.read_bytes()

    ready = tmp_path / "uncommitted-write-ready"
    worker = """
from pathlib import Path
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA cache_size=4")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE portfolios SET name=?", ("b" * 1_100_000,))
Path(sys.argv[2]).touch()
sys.stdin.buffer.read(1)
"""
    journal = managed.with_name(managed.name + "-journal")
    process = subprocess.Popen(
        [sys.executable, "-c", worker, str(managed), str(ready)], stdin=subprocess.PIPE,
    )
    try:
        deadline = monotonic() + 20
        while not ready.exists():
            assert process.poll() is None, "Writer exited before signalling"
            assert monotonic() < deadline, "Writer did not finish staging uncommitted data"
            sleep(0.01)
        assert journal.stat().st_size > 512
        assert process.poll() is None
    finally:
        try:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        finally:
            process.stdin.close()
    assert process.returncode != 0

    boundary = authorize(managed, managed.parent)
    database_bytes, journal_bytes = managed.read_bytes(), journal.read_bytes()
    assert database_bytes != committed_bytes, "The writer did not spill uncommitted pages"
    with pytest.raises(writes.ApplicationWriteError, match="rollback journal recovery") as failure:
        with boundary.connect():
            pytest.fail("Unexpected writable connection before recovery")
    assert failure.value.reason == "recovery_required"
    assert failure.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK
    assert str(managed) not in str(failure.value)
    assert managed.read_bytes() == database_bytes
    assert journal.read_bytes() == journal_bytes

    # Only the test opens ordinary SQLite to perform its built-in recovery.
    with closing(sqlite3.connect(managed)) as recovery:
        assert recovery.execute("SELECT name FROM portfolios").fetchall() == [(baseline,)]
    with boundary.connect() as connection:
        assert connection.execute("SELECT name FROM portfolios").fetchall() == [(baseline,)]
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        connection.execute("INSERT INTO portfolios VALUES (2, 'After recovery', 'USD')")


@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_live_journal_or_wal_remains_usable(managed, mode):
    with closing(sqlite3.connect(managed, isolation_level=None)) as writer:
        assert writer.execute(f"PRAGMA journal_mode={mode}").fetchone() == (mode,)
        writer.execute("INSERT INTO portfolios VALUES (1, 'Committed', 'USD')")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO portfolios VALUES (2, 'Uncommitted', 'USD')")
        sidecars = ("-wal", "-shm") if mode == "wal" else ("-journal",)
        assert all(managed.with_name(managed.name + suffix).is_file() for suffix in sidecars)
        with authorize(managed, managed.parent).connect() as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == (mode,)
            assert connection.execute("SELECT id FROM portfolios").fetchall() == [(1,)]
            writer.execute("ROLLBACK")
            connection.execute("INSERT INTO portfolios VALUES (3, 'After rollback', 'USD')")
    with closing(get_read_only_connection(managed)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == (mode,)
        assert connection.execute("SELECT id FROM portfolios ORDER BY id").fetchall() == [(1,), (3,)]


def test_generic_read_only_error_does_not_report_recovery(managed, monkeypatch):
    def fail(connection):
        error = sqlite3.OperationalError("injected read-only failure")
        error.sqlite_errorcode = sqlite3.SQLITE_READONLY
        raise error

    monkeypatch.setattr(writes.ApplicationWrites, "_validate", staticmethod(fail))
    denied(authorize(managed, managed.parent), "connection_failure")


@pytest.mark.parametrize("target", sorted((main.ROOT / "data").glob("*.db")))
def test_bundled_paths_ineligible(target):
    denied(authorize(target, target.parent), "ineligible_target")


@pytest.mark.parametrize(
    ("state", "reason"),
    [("missing", "missing_target"), ("directory", "protected_target"),
     ("corrupt", "connection_failure"), ("version_zero", "schema_version"),
     ("newer", "schema_version"), ("wrong_schema", "incompatible_schema")],
)
def test_invalid_existing_targets(tmp_path, state, reason):
    path = tmp_path / "portfolio.db"
    if state == "directory":
        path.mkdir()
    elif state == "corrupt":
        path.write_bytes(b"invalid SQLite data")
    elif state == "version_zero":
        create_schema(path)
    elif state in {"newer", "wrong_schema"}:
        create_schema(path)
        with closing(sqlite3.connect(path)) as connection:
            if state == "newer":
                connection.execute("PRAGMA user_version=2")
            else:
                connection.executescript("PRAGMA user_version=1; DROP TABLE prices;")
    original = path.read_bytes() if path.is_file() else None
    denied(authorize(path, tmp_path), reason)
    if original is not None:
        assert path.read_bytes() == original
    assert not list(tmp_path.glob("portfolio.db-*"))
    if state == "missing":
        assert not path.exists()


def test_hard_links_and_linked_sidecars(managed, tmp_path):
    alias = tmp_path / "alias.db"
    alias.hardlink_to(managed)
    denied(authorize(managed, managed.parent), "file_identity")
    alias.unlink()
    sidecar = managed.with_name("portfolio.db-journal")
    source = tmp_path / "source.db"
    source.write_bytes(b"keep")
    sidecar.hardlink_to(source)
    denied(authorize(managed, managed.parent), "file_identity")
    assert source.read_bytes() == b"keep"


def test_symlink_to_protected_source(managed, tmp_path):
    source = tmp_path / "source.db"
    managed.rename(source)
    try:
        managed.symlink_to(source)
    except OSError as error:
        pytest.skip(f"File symlinks unavailable: {error.winerror if os.name == 'nt' else error.errno}")
    denied(authorize(managed.resolve(), managed.parent), "ineligible_target")
    denied(authorize(managed, managed.parent), "protected_target")


@pytest.mark.skipif(os.name != "nt", reason="Windows path aliases")
def test_windows_case_and_junction_aliases(managed, tmp_path, monkeypatch):
    from riskengine.db import lifecycle

    alternate_case = managed.with_name(managed.name.upper())
    with authorize(alternate_case, managed.parent).connect() as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(managed.parent)],
        capture_output=True, check=False,
    )
    if result.returncode:
        pytest.skip("NTFS junction creation unavailable")
    try:
        monkeypatch.setattr(lifecycle, "REPOSITORY_ROOT", managed.parent)
        denied(authorize((junction / managed.name).resolve(), junction), "ineligible_target")
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows path forms")
@pytest.mark.parametrize("component", ["NUL", "folder:stream", "folder."])
def test_unsupported_windows_components(managed, tmp_path, component):
    denied(authorize(managed, tmp_path / component), "ineligible_target")


@pytest.mark.parametrize("failure", ["identity", "initialization", "removed"])
def test_connection_failure_closes_handles(managed, monkeypatch, failure):
    original_connect = sqlite3.connect
    connections = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if failure == "initialization" and sql == "PRAGMA foreign_keys = ON":
                raise sqlite3.OperationalError("internal diagnostic")
            return super().execute(sql, *args)

    def connect(database, **kwargs):
        if database == ":memory:":
            return original_connect(database, **kwargs)
        if failure == "removed" and database.endswith("mode=rw"):
            managed.unlink()
        connection = original_connect(database, factory=Connection, **kwargs)
        connections.append(connection)
        return connection

    if failure == "identity":
        identity = writes.ApplicationWrites._identity
        monkeypatch.setattr(
            writes.ApplicationWrites, "_identity",
            staticmethod(lambda path: (99, 99) if len(connections) == 2 else identity(path)),
        )
    monkeypatch.setattr(writes.sqlite3, "connect", connect)
    denied(authorize(managed, managed.parent), "file_identity" if failure == "identity" else "connection_failure")
    assert len(connections) == (1 if failure == "removed" else 2)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    assert managed.exists() == (failure != "removed")


def test_unavailable_identity(managed, monkeypatch):
    def unavailable(path):
        raise writes.DatabaseLifecycleError("Unreliable metadata")

    monkeypatch.setattr(writes, "_source_identity", unavailable)
    denied(authorize(managed, managed.parent), "file_identity")


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only file attribute")
def test_permission_failure_does_not_fall_back_to_read_only(managed):
    original = managed.read_bytes()
    managed.chmod(0o444)
    try:
        denied(authorize(managed, managed.parent), "connection_failure")
        assert managed.read_bytes() == original
    finally:
        managed.chmod(0o666)


@pytest.mark.parametrize("demo,legacy", [(False, False), (True, False), (False, True)])
def test_lifespan_fixes_effective_selection(managed, monkeypatch, demo, legacy):
    monkeypatch.setenv("RISKENGINE_DB", str(managed))
    monkeypatch.setenv("RISKENGINE_WRITE_DATA_ROOT", str(managed.parent))
    monkeypatch.setenv("RISKENGINE_DEMO", "1" if demo else "0")
    monkeypatch.setenv("RISKENGINE_API_MODE", "legacy-read-only" if legacy else "protected")
    monkeypatch.setenv("RISKENGINE_BOOTSTRAP_CODE", "a" * 43)
    monkeypatch.setenv("RISKENGINE_API_PORT", "8000")
    monkeypatch.setenv("RISKENGINE_VITE_PORT", "5173")

    async def check():
        async with main.lifespan(main.app):
            boundary = main.app.state.database_writes
            assert boundary.selected == main.app.state.database.path
            monkeypatch.delenv("RISKENGINE_WRITE_DATA_ROOT")
            monkeypatch.setenv("RISKENGINE_DB", str(managed.parent / "different.db"))
            if demo or legacy:
                denied(boundary, "demo" if demo else "operating_mode")
            else:
                with boundary.connect() as connection:
                    assert Path(connection.execute("PRAGMA database_list").fetchone()[2]) == managed
        assert main.app.state.database_writes is None

    asyncio.run(check())


def test_invalid_protected_startup_does_not_install_boundary(managed, monkeypatch):
    monkeypatch.setenv("RISKENGINE_DB", str(managed))
    monkeypatch.setenv("RISKENGINE_WRITE_DATA_ROOT", str(managed.parent))
    monkeypatch.setenv("RISKENGINE_API_MODE", "protected")
    monkeypatch.delenv("RISKENGINE_BOOTSTRAP_CODE", raising=False)

    async def check():
        with pytest.raises(RuntimeError, match="local session configuration"):
            async with main.lifespan(main.app):
                pytest.fail("Invalid protected configuration started the app")
        assert getattr(main.app.state, "database_writes", None) is None

    asyncio.run(check())
