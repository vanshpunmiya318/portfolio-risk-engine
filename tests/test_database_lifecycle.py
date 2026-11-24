import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO, TextIOWrapper
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace

import pytest

from riskengine.api import main
from riskengine.db import lifecycle
from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema
from test_api_database import get


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "v1.db"
    create_schema(path)
    return path


def assert_no_artifacts(directory):
    assert not list(directory.glob(".portfolio-setup-*"))


def test_default_path_is_lazy(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = lifecycle.managed_database_path()
    assert path == tmp_path / "Portfolio Risk Engine" / "portfolio.db"
    assert not path.parent.exists()
    monkeypatch.delenv("LOCALAPPDATA")
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="LOCALAPPDATA"):
        lifecycle.managed_database_path()


def test_create_empty_and_refuse_overwrite(tmp_path):
    root = tmp_path / "Portfolio café 資料"
    path = lifecycle.create_database(root)
    assert lifecycle.inspect_database(path) == 1
    with closing(get_connection(path)) as connection:
        for table in ("portfolios", "instruments", "transactions", "cash_flows", "prices",
                      "corporate_actions", "corporate_action_coverage"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute("INSERT INTO cash_flows VALUES (1, 1, '2024-01-02', 'DEPOSIT', 1, 'USD', 'test')")
    before = path.read_bytes()
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="already exists"):
        lifecycle.create_database(root)
    assert path.read_bytes() == before
    assert_no_artifacts(root)


def test_concurrent_publish_never_clobbers(tmp_path, monkeypatch):
    original = lifecycle._publish
    barrier = Barrier(2, timeout=10)

    def publish(temporary, destination):
        barrier.wait()
        original(temporary, destination)

    monkeypatch.setattr(lifecycle, "_publish", publish)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(lifecycle.create_database, tmp_path) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=15))
            except lifecycle.DatabaseLifecycleError as error:
                assert "Another operation" in str(error)
    assert outcomes == [tmp_path / "portfolio.db"]
    assert lifecycle.inspect_database(outcomes[0]) == 1
    assert_no_artifacts(tmp_path)


@pytest.mark.parametrize("step", ["create_schema", "_finalize_database", "_publish"])
def test_create_failure_cleanup_and_retry(tmp_path, monkeypatch, step):
    def fail(path, *args):
        assert path.is_file()
        raise PermissionError("injected failure")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, step, fail)
        with pytest.raises(PermissionError, match="injected failure"):
            lifecycle.create_database(tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert lifecycle.inspect_database(lifecycle.create_database(tmp_path)) == 1


def test_schema_loader_rolls_back_incomplete_ddl(tmp_path, monkeypatch):
    from riskengine.db.schema import loader

    path = tmp_path / "partial.db"
    monkeypatch.setattr(loader, "schema_sql", lambda: "CREATE TABLE test(id); invalid SQL;")
    with pytest.raises(sqlite3.OperationalError):
        loader.create_schema(path)
    with closing(get_connection(path, read_only=True)) as connection:
        assert connection.execute("SELECT name FROM sqlite_schema").fetchall() == []


def test_adopt_includes_committed_wal_and_excludes_uncommitted_rows(source, tmp_path):
    with closing(get_connection(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO portfolios VALUES (77, 'User account', 'EUR')")
        writer.execute("INSERT INTO cash_flows VALUES (31, 77, '2024-01-02', 'DEPOSIT', 4321.25, 'EUR', 'manual')")
        writer.commit()
        writer.execute("INSERT INTO portfolios VALUES (99, 'Uncommitted', 'EUR')")
        wal = source.with_name(source.name + "-wal")
        assert wal.stat().st_size > 0
        source_before = source.read_bytes(), wal.read_bytes()
        destination = lifecycle.adopt_database(source, tmp_path / "managed")
        assert (source.read_bytes(), wal.read_bytes()) == source_before
        assert lifecycle.inspect_database(source) == 0
        assert lifecycle.inspect_database(destination) == 1
        with closing(get_connection(destination, read_only=True)) as copy:
            assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert copy.execute("SELECT * FROM portfolios").fetchall() == [(77, "User account", "EUR")]
            assert copy.execute("SELECT * FROM cash_flows").fetchall() == [
                (31, 77, "2024-01-02", "DEPOSIT", 4321.25, "EUR", "manual"),
            ]
        assert list(destination.parent.iterdir()) == [destination]


def test_reject_both_names_of_hard_linked_active_wal_source(source, tmp_path):
    alias = tmp_path / "alias.db"
    os.link(source, alias)
    assert source.samefile(alias) and source.stat().st_nlink == 2
    with closing(get_connection(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO portfolios VALUES (77, 'Committed in WAL', 'USD')")
        writer.commit()
        wal = source.with_name(source.name + "-wal")
        assert wal.stat().st_size > 0
        source_before = source.read_bytes(), wal.read_bytes()
        for name in (source, alias):
            root = tmp_path / (name.stem + "-managed")
            with pytest.raises(lifecycle.DatabaseLifecycleError, match="multiple hard links.*SQLite backup"):
                lifecycle.adopt_database(name, root)
            with pytest.raises(lifecycle.DatabaseLifecycleError, match="multiple hard links"):
                lifecycle.inspect_database(name)
            assert not root.exists()
        assert not list(tmp_path.glob("alias.db-*"))
        assert (source.read_bytes(), wal.read_bytes()) == source_before
        assert writer.execute("SELECT id FROM portfolios").fetchall() == [(77,)]
        assert writer.execute("PRAGMA user_version").fetchone() == (0,)


@pytest.mark.parametrize("failure", ["permission", "unknown_count", "unknown_identity", "changed_identity"])
def test_source_identity_fails_closed(source, tmp_path, monkeypatch, failure):
    original_stat = Path.stat
    initial = source.stat()
    canonical = source.resolve()
    opened = False

    def read_stat(path, *args, **kwargs):
        if path != canonical or (failure == "changed_identity" and not opened):
            return original_stat(path, *args, **kwargs)
        if failure == "permission":
            raise PermissionError("metadata access denied")
        return SimpleNamespace(
            st_mode=initial.st_mode,
            st_dev=initial.st_dev,
            st_ino={"unknown_identity": 0, "changed_identity": initial.st_ino + 1}.get(failure, initial.st_ino),
            st_nlink=0 if failure == "unknown_count" else initial.st_nlink,
        )

    def open_connection(path, *, read_only):
        nonlocal opened
        assert failure == "changed_identity" and read_only
        connection = get_connection(path, read_only=read_only)
        opened = True
        return connection

    # Match only the explicit identity probes. Path.resolve may also inspect
    # the file on some platforms.
    def source_identity(path):
        with monkeypatch.context() as inner:
            inner.setattr(Path, "stat", read_stat)
            return verify_identity(path)

    verify_identity = lifecycle._source_identity
    monkeypatch.setattr(lifecycle, "_source_identity", source_identity)
    monkeypatch.setattr(lifecycle, "get_connection", open_connection)
    root = tmp_path / "managed"
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="verify source|changed while opening"):
        lifecycle.adopt_database(source, root)
    assert not root.exists()


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8"])
def test_unicode_cli_with_redirected_output(source, tmp_path, encoding):
    source = source.rename(tmp_path / "Source \u8cc7.db")
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": encoding}

    def cli(*args, expected=0):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8=0", "-m",
                                 "riskengine.db.lifecycle", *map(str, args)],
                                capture_output=True, env=env, timeout=20)
        output = result.stdout.decode(encoding), result.stderr.decode(encoding)
        assert result.returncode == expected, output
        return output

    display = "\\u8cc7" if encoding == "cp1252" else "\u8cc7"
    for command, inputs in (("create", ()), ("adopt", (source,))):
        root = tmp_path / f"{command} \u8cc7"
        stdout, stderr = cli(command, *inputs, "--data-root", root)
        assert display in stdout and not stderr
        published = root / "portfolio.db"
        assert lifecycle.inspect_database(published) == 1
        assert "schema version 1" in cli("inspect", published)[0]
        _, stderr = cli(command, *inputs, "--data-root", root, expected=1)
        assert display in stderr and "already exists" in stderr
    _, stderr = cli("inspect", tmp_path / "Missing \u8cc7.db", expected=1)
    assert display in stderr and "not found" in stderr


def test_publication_and_cleanup_failures_preserve_both_causes(tmp_path, monkeypatch):
    root = tmp_path / "Managed \u8cc7"
    publish_failure = PermissionError("publication denied")
    cleanup_failure = PermissionError("unlink denied")
    unlink = Path.unlink

    def deny_publish(temporary, destination):
        raise publish_failure

    def deny_cleanup(path, *args, **kwargs):
        if path.suffix == ".db" and path.name.startswith(".portfolio-setup-"):
            raise cleanup_failure
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_publish", deny_publish)
        patch.setattr(Path, "unlink", deny_cleanup)
        with pytest.raises(PermissionError) as failure:
            lifecycle.create_database(root)
        assert failure.value is publish_failure
        retained, = root.iterdir()
        assert any(str(retained) in note and "unlink denied" in note for note in failure.value.__notes__)
        patch.setattr(sys, "argv", ["lifecycle", "create", "--data-root", str(root)])
        with TextIOWrapper(BytesIO(), encoding="cp1252", write_through=True) as errors:
            patch.setattr(sys, "stderr", errors)
            assert lifecycle.main() == 1
            stderr = errors.buffer.getvalue().decode("cp1252")
        assert "publication denied" in stderr and "unlink denied" in stderr and "\\u8cc7" in stderr
    assert not (root / "portfolio.db").exists()
    assert lifecycle.inspect_database(lifecycle.create_database(root)) == 1
    assert retained.is_file()


def test_persistent_backup_contention_does_not_publish(source, tmp_path, monkeypatch):
    create_connection = lifecycle.get_connection
    blocker = None

    def connect(path, *, read_only=False):
        nonlocal blocker
        if read_only or not path.name.startswith(".portfolio-setup-"):
            return create_connection(path, read_only=read_only)
        blocker = sqlite3.connect(path)
        blocker.execute("BEGIN EXCLUSIVE")
        return sqlite3.connect(path, timeout=0)

    monkeypatch.setattr(lifecycle, "get_connection", connect)
    monkeypatch.setattr(lifecycle, "BACKUP_CONTENTION_SECONDS", 0.2)
    copy = lifecycle._copy_snapshot

    def release_test_lock(original, destination):
        try:
            copy(original, destination)
        finally:
            blocker.close()

    monkeypatch.setattr(lifecycle, "_copy_snapshot", release_test_lock)
    root = tmp_path / "managed"
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="repeatedly locked"):
        lifecycle.adopt_database(source, root)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("sql, match", [
    ("DROP TABLE instruments", "Incompatible database schema"),
    ("ALTER TABLE instruments DROP COLUMN sector", "Incompatible database schema"),
    ("CREATE INDEX extra ON portfolios(currency)", "Incompatible database schema"),
    ("PRAGMA user_version=1; CREATE TRIGGER extra AFTER INSERT ON portfolios BEGIN SELECT 1; END", "Incompatible"),
    ("PRAGMA user_version=2", "Unsupported schema version"),
    ("INSERT INTO cash_flows VALUES (1, 999, '2024-01-02', 'DEPOSIT', 1, 'USD', 'test')", "foreign-key"),
])
def test_reject_bad_schema_or_integrity(source, tmp_path, sql, match):
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(sql)
    with pytest.raises(lifecycle.DatabaseLifecycleError, match=match):
        lifecycle.adopt_database(source, tmp_path / "managed")
    assert not (tmp_path / "managed").exists()


@pytest.mark.parametrize("original, replacement", [
    ("quantity > 0", "quantity >= 0"),
    ("REFERENCES portfolios(id)", "REFERENCES portfolios(name)"),
    ("name TEXT NOT NULL UNIQUE", "name TEXT NOT NULL"),
])
def test_structural_validation_checks_rules_with_same_columns(tmp_path, original, replacement):
    from riskengine.db.schema.loader import schema_sql

    source = tmp_path / "modified.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.executescript(schema_sql().replace(original, replacement))
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="Incompatible"):
        lifecycle.inspect_database(source)


@pytest.mark.parametrize("content, exception, match", [
    (None, lifecycle.DatabaseLifecycleError, "not found"),
    (b"", lifecycle.DatabaseLifecycleError, "Incompatible"),
    (b"not SQLite", sqlite3.DatabaseError, "not a database"),
])
def test_missing_empty_file_and_malformed_sources(tmp_path, content, exception, match):
    path = tmp_path / "source.db"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(exception, match=match):
        lifecycle.adopt_database(path, tmp_path / "managed")
    assert path.exists() == (content is not None)
    assert not (tmp_path / "managed").exists()


@pytest.mark.parametrize("step", ["copy", "validation"])
def test_adoption_failure_keeps_source_and_removes_temporary(source, tmp_path, monkeypatch, step):
    before = source.read_bytes()

    class FailingBackup(sqlite3.Connection):
        def backup(self, target, **kwargs):
            raise sqlite3.OperationalError("injected copy failure")

    def connection(path, *, read_only=False):
        if read_only:
            return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, factory=FailingBackup)
        return get_connection(path)

    def fail_validation(path):
        raise sqlite3.OperationalError("injected validation failure")

    if step == "copy":
        monkeypatch.setattr(lifecycle, "get_connection", connection)
    else:
        monkeypatch.setattr(lifecycle, "_finalize_database", fail_validation)
    root = tmp_path / "managed"
    with pytest.raises(sqlite3.OperationalError, match=f"injected {step} failure"):
        lifecycle.adopt_database(source, root)
    assert source.read_bytes() == before
    assert list(root.iterdir()) == []


def test_reject_repository_paths_and_unsafe_roots(tmp_path, monkeypatch):
    protected = tmp_path / "repo"
    monkeypatch.setattr(lifecycle, "REPOSITORY_ROOT", protected)
    for path in (protected, protected / "data", tmp_path):
        with pytest.raises(lifecycle.DatabaseLifecycleError, match="outside"):
            lifecycle.create_database(path)
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="absolute"):
        lifecycle.create_database(Path("relative"))
    assert not protected.exists()


def test_reject_all_bundled_destination_hard_links(tmp_path):
    for source in sorted((ROOT / "data").glob("*.db")):
        # Work on isolated copies. Link identity has the same semantics without
        # changing link counts on any bundled database.
        original = tmp_path / source.name
        shutil.copyfile(source, original)
        directory = tmp_path / source.stem
        directory.mkdir()
        destination = directory / "portfolio.db"
        os.link(original, destination)
        with pytest.raises(lifecycle.DatabaseLifecycleError, match="already exists"):
            lifecycle.adopt_database(original, directory)
        assert destination.samefile(original)
        assert destination.read_bytes() == source.read_bytes()


def test_existing_destination_identity_and_sidecar(source, tmp_path):
    destination = tmp_path / "portfolio.db"
    source.rename(destination)
    before = destination.read_bytes()
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="already exists"):
        lifecycle.adopt_database(destination, tmp_path)
    assert destination.read_bytes() == before
    destination.unlink()
    sidecar = tmp_path / "portfolio.db-wal"
    sidecar.write_bytes(b"unrelated")
    with pytest.raises(lifecycle.DatabaseLifecycleError, match="sidecar already exists"):
        lifecycle.create_database(tmp_path)
    assert sidecar.read_bytes() == b"unrelated"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and case alias")
def test_resolved_windows_root_rejects_repository_alias(tmp_path, monkeypatch):
    protected = tmp_path / "Protected"
    protected.mkdir()
    alias = tmp_path / "alias"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(protected)],
                   check=True, capture_output=True)
    monkeypatch.setattr(lifecycle, "REPOSITORY_ROOT", protected)
    for directory in (alias, protected.with_name("PROTECTED")):
        with pytest.raises(lifecycle.DatabaseLifecycleError, match="outside"):
            lifecycle.create_database(directory)
    assert list(protected.iterdir()) == []


def api_snapshot(monkeypatch, path, routes):
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)

    async def exercise():
        async with main.app.router.lifespan_context(main.app):
            return {route: await get(route) for route in routes}

    return asyncio.run(exercise())


def test_empty_database_readiness_and_missing_database(tmp_path, monkeypatch):
    empty = lifecycle.create_database(tmp_path / "empty")
    assert api_snapshot(monkeypatch, empty, ["/ready"]) == {"/ready": (200, {"status": "ready"})}
    missing = tmp_path / "missing.db"
    assert api_snapshot(monkeypatch, missing, ["/ready"])["/ready"][0] == 503
    assert not missing.exists()


def test_adopted_historical_records_and_financial_api_parity(tmp_path, monkeypatch):
    historical = ROOT / "data" / "historical_portfolio.db"
    if not historical.is_file():
        pytest.skip("Local historical database is required for release parity")
    source = tmp_path / "historical.db"
    shutil.copyfile(historical, source)
    before = source.read_bytes()
    destination = lifecycle.adopt_database(source, tmp_path / "managed")
    assert source.read_bytes() == before
    assert lifecycle.inspect_database(source) == 0
    assert lifecycle.inspect_database(destination) == 1
    with closing(get_connection(source, read_only=True)) as original, closing(
        get_connection(destination, read_only=True)
    ) as adopted:
        assert tuple(original.iterdump()) == tuple(adopted.iterdump())
        portfolio_id = original.execute("SELECT MIN(id) FROM portfolios").fetchone()[0]

    prefix = f"/api/portfolios/{portfolio_id}"
    routes = ["/health", "/ready", "/api/portfolio/summary"] + [
        f"{prefix}/{endpoint}"
        for endpoint in ("summary", "holdings", "transactions", "performance", "risk", "allocation", "report")
    ]
    original = api_snapshot(monkeypatch, source, routes)
    assert all(status == 200 for status, _ in original.values()), {
        route: status for route, (status, _) in original.items()
    }
    assert api_snapshot(monkeypatch, destination, routes) == original
