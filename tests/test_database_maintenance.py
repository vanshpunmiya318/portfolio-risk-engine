import asyncio
from contextlib import closing, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from time import monotonic, sleep
from zipfile import ZipFile

import pytest

from riskengine.api import main
from riskengine.db import maintenance
from riskengine.db.application_transactions import TransactionExecutor
from riskengine.db.application_writes import ApplicationWriteError, ApplicationWrites
from riskengine.db.lifecycle import create_database
from riskengine.db.maintenance_lock import MaintenanceError, database_ownership, pending_path


@pytest.fixture
def db(tmp_path):
    return create_database(tmp_path / "Managed # % café")


def connect(path):
    return sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)


def names(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute("SELECT id, name FROM portfolios ORDER BY id").fetchall()


def insert(path, identity, name):
    with closing(connect(path)) as connection:
        connection.execute("INSERT INTO portfolios VALUES (?, ?, 'USD')", (identity, name))


def contents(bundle, destination):
    with ZipFile(bundle) as archive:
        destination.write_bytes(archive.read("portfolio.db"))
    return names(destination)


def wait_for(path, worker):
    deadline = monotonic() + 45
    while not path.exists():
        assert worker.poll() is None, "Worker exited before acquiring ownership"
        assert monotonic() < deadline, "Worker did not signal"
        sleep(0.02)


@pytest.mark.parametrize("populated", [False, True])
def test_verified_backup_and_restore_with_retained_safety_copy(db, tmp_path, populated):
    if populated:
        insert(db, 7, "Backed up")
    backup = maintenance.create_backup(db.parent)
    info = maintenance.verify_backup(backup)
    original = backup.read_bytes()
    assert info.format_version == info.schema_version == 1
    insert(db, 8, "Before restore")
    result = maintenance.restore_backup(backup, db.parent)
    expected = [(7, "Backed up")] if populated else []
    assert names(db) == expected
    assert backup.read_bytes() == original
    assert maintenance.verify_backup(result.preserved).sha256 != info.sha256
    assert contents(result.preserved, tmp_path / "safety.db") == expected + [(8, "Before restore")]
    assert not pending_path(db).exists()


def test_wal_backup_includes_only_committed_rows(db, tmp_path):
    with closing(connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO portfolios VALUES (1,'Committed WAL','USD')")
        wal = db.with_name(db.name + "-wal")
        assert wal.stat().st_size > 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO portfolios VALUES (2,'Uncommitted','USD')")
        bundle = maintenance.create_backup(db.parent)
        assert contents(bundle, tmp_path / "backed.db") == [(1, "Committed WAL")]
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        with pytest.raises(MaintenanceError, match="sidecars"):
            maintenance.restore_backup(bundle, db.parent)
        writer.execute("ROLLBACK")
        assert names(db) == [(1, "Committed WAL")]
    assert maintenance.verify_backup(bundle).schema_version == 1
    maintenance.restore_backup(bundle, db.parent)
    assert names(db) == [(1, "Committed WAL")]


def test_incomplete_archived_wal_image_is_rejected_before_restore(db, tmp_path):
    insert(db, 1, "Checkpointed")
    backup = maintenance.create_backup(db.parent)
    external = tmp_path / "incomplete.riskbackup"
    with closing(connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO portfolios VALUES (2,'Only in WAL','USD')")
        assert names(db) == [(1, "Checkpointed"), (2, "Only in WAL")]
        main_image = db.read_bytes()
        assert main_image[18:20] == b"\x02\x02"
        main_only = tmp_path / "main-only.db"
        main_only.write_bytes(main_image)
        with closing(sqlite3.connect(main_only.as_uri() + "?immutable=1", uri=True)) as reader:
            assert reader.execute("SELECT id FROM portfolios").fetchall() == [(1,)]
        with ZipFile(backup) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest.update(size=len(main_image), sha256=sha256(main_image).hexdigest())
        with ZipFile(external, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("portfolio.db", main_image)

    original = db.read_bytes(), external.read_bytes()
    for operation in (maintenance.verify_backup, lambda path: maintenance.restore_backup(path, db.parent)):
        with pytest.raises(MaintenanceError, match="standalone rollback-journal database image"):
            operation(external)
    assert (db.read_bytes(), external.read_bytes()) == original
    assert names(db) == [(1, "Checkpointed"), (2, "Only in WAL")]
    assert not pending_path(db).exists()
    assert not list(backup.parent.glob("pre-restore-*"))


def test_live_rollback_journal_and_financial_provenance(db, tmp_path):
    with closing(connect(db)) as writer:
        writer.executescript("""
            INSERT INTO portfolios VALUES (7,'Example','USD');
            INSERT INTO instruments(id,ticker,name,asset_type,currency,primary_source)
              VALUES (9,'TEST','Example instrument','ETF','USD','tiingo');
            INSERT INTO transactions VALUES (10,7,9,'2025-01-03','BUY',2,100,'USD','test-ledger');
            INSERT INTO cash_flows VALUES (11,7,'2025-01-02','DEPOSIT',1000,'USD','test-ledger');
            INSERT INTO prices VALUES (12,9,'2025-01-03',100,102,98,100,100,300,'tiingo','2025-01-03');
            INSERT INTO corporate_actions VALUES (13,9,'2025-02-03','SPLIT',2,'tiingo','2025-02-03');
            INSERT INTO corporate_action_coverage VALUES (14,9,'2025-01-01','2025-03-01','tiingo','2025-03-01');
        """)
        tables = ("portfolios", "instruments", "transactions", "cash_flows", "prices",
                  "corporate_actions", "corporate_action_coverage")
        expected = {table: writer.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE prices SET adjusted_close=50")
        assert db.with_name(db.name + "-journal").is_file()
        bundle = maintenance.create_backup(db.parent)
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        writer.execute("ROLLBACK")
        writer.execute("UPDATE prices SET adjusted_close=75")
    replacement = maintenance.restore_backup(bundle, db.parent)
    with closing(connect(db)) as reader:
        assert {table: reader.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == expected
    safety = tmp_path / "before.db"
    contents(replacement.preserved, safety)
    with closing(connect(safety)) as reader:
        assert reader.execute("SELECT adjusted_close FROM prices").fetchall() == [(75,)]


@pytest.mark.parametrize("defect", ["absent_manifest", "bad_digest", "version", "truncated", "schema"])
def test_invalid_bundle_never_replaces_database(db, tmp_path, defect):
    insert(db, 1, "Keep")
    bundle = maintenance.create_backup(db.parent)
    invalid = tmp_path / "invalid.riskbackup"
    with ZipFile(bundle) as source:
        manifest = json.loads(source.read("manifest.json"))
        payload = source.read("portfolio.db")
    if defect == "schema":
        altered = tmp_path / "altered.db"
        altered.write_bytes(payload)
        with closing(connect(altered)) as connection:
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON portfolios BEGIN SELECT 1; END")
        payload = altered.read_bytes()
        manifest.update(size=len(payload), sha256=sha256(payload).hexdigest())
    if defect == "bad_digest":
        manifest["sha256"] = "0" * 64
    if defect == "version":
        manifest["format_version"] = 999
    with ZipFile(invalid, "w") as archive:
        if defect != "absent_manifest":
            archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("portfolio.db", payload)
    if defect == "truncated":
        invalid.write_bytes(invalid.read_bytes()[:100])
    before, bad_before = db.read_bytes(), invalid.read_bytes()
    for operation in (maintenance.verify_backup, lambda p: maintenance.restore_backup(p, db.parent)):
        with pytest.raises(ValueError):
            operation(invalid)
    assert (db.read_bytes(), invalid.read_bytes()) == (before, bad_before)
    assert list(bundle.parent.glob("pre-restore-*")) == []


def test_backup_names_collide_without_overwrite(db, monkeypatch):
    monkeypatch.setattr(maintenance, "_bundle_name", lambda kind: "collision.riskbackup")
    bundle = maintenance.create_backup(db.parent)
    before = bundle.read_bytes()
    insert(db, 1, "New data")
    with pytest.raises(ValueError, match="already exists"):
        maintenance.create_backup(db.parent)
    assert bundle.read_bytes() == before
    assert list(bundle.parent.iterdir()) == [bundle]


def test_missing_and_protected_paths_do_not_create_sources(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(ApplicationWriteError, match="missing_target"):
        maintenance.create_backup(root)
    assert not root.exists()
    with pytest.raises(ValueError, match="outside"):
        maintenance.create_backup(main.ROOT / "data")


def test_hard_linked_target_and_bundle_are_denied(db, tmp_path):
    bundle = maintenance.create_backup(db.parent)
    alias = tmp_path / "alias.riskbackup"
    alias.hardlink_to(bundle)
    with pytest.raises(ApplicationWriteError, match="file_identity"):
        maintenance.verify_backup(alias)
    alias = tmp_path / "alias.db"
    alias.hardlink_to(db)
    with pytest.raises(ApplicationWriteError, match="file_identity"):
        maintenance.create_backup(db.parent)


def test_safety_backup_failure_prevents_replacement(db, monkeypatch):
    bundle = maintenance.create_backup(db.parent)
    insert(db, 1, "Keep current")
    before = db.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected safety backup failure")

    monkeypatch.setattr(maintenance, "_backup", fail)
    with pytest.raises(OSError, match="safety backup"):
        maintenance.restore_backup(bundle, db.parent)
    assert db.read_bytes() == before and not pending_path(db).exists()


def test_backup_primary_failure_survives_workspace_cleanup_failure(db, monkeypatch):
    remove = maintenance.shutil.rmtree
    retained = []

    def fail_backup(*args):
        raise OSError("injected snapshot failure")

    def fail_cleanup(path):
        retained.append(path)
        raise PermissionError("injected cleanup failure")

    try:
        with monkeypatch.context() as faults:
            faults.setattr(maintenance, "_copy_snapshot", fail_backup)
            faults.setattr(maintenance.shutil, "rmtree", fail_cleanup)
            with pytest.raises(OSError, match="snapshot failure") as error:
                maintenance.create_backup(db.parent)
        assert len(retained) == 1
        assert str(retained[0]) in " ".join(error.value.__notes__)
        assert not list((db.parent / "backups").glob("*.riskbackup"))
    finally:
        for path in retained:
            remove(path)


@pytest.mark.parametrize("failure", ["replace", "post_verify"])
def test_restore_failure_retains_safety_backup_and_blocks_startup(db, tmp_path, monkeypatch, failure):
    backup = maintenance.create_backup(db.parent)
    insert(db, 1, "Before restore")

    def fail(*args, **kwargs):
        raise OSError("injected " + failure)

    with monkeypatch.context() as faults:
        if failure == "replace":
            faults.setattr(maintenance.os, "replace", fail)
        else:
            faults.setattr(maintenance, "_check_active", fail)
        with pytest.raises(OSError, match=failure) as error:
            maintenance.restore_backup(backup, db.parent)
    assert pending_path(db).exists()
    preserved, = backup.parent.glob("pre-restore-*")
    assert str(preserved) in " ".join(error.value.__notes__)
    assert contents(preserved, tmp_path / "safety.db") == [(1, "Before restore")]
    assert names(db) == ([(1, "Before restore")] if failure == "replace" else [])
    with pytest.raises(MaintenanceError, match="Unresolved"):
        with database_ownership(db):
            pytest.fail("Pending restoration admitted a reader")
    assert maintenance.check_active(db.parent) == db
    assert not pending_path(db).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing")
def test_windows_open_handle_prevents_replacement_without_data_loss(db):
    bundle = maintenance.create_backup(db.parent)
    insert(db, 1, "Current")
    before = db.read_bytes()
    with db.open("rb"):
        with pytest.raises(PermissionError):
            maintenance.restore_backup(bundle, db.parent)
    assert db.read_bytes() == before
    assert pending_path(db).exists()
    assert len(list(bundle.parent.glob("pre-restore-*"))) == 1
    maintenance.check_active(db.parent)


def test_release_failure_reports_verified_restore_and_retained_backup(db, monkeypatch):
    bundle = maintenance.create_backup(db.parent)
    insert(db, 1, "Current")
    ownership = maintenance.database_ownership

    @contextmanager
    def fail_release(*args, **kwargs):
        with ownership(*args, **kwargs):
            yield
        raise OSError("injected ownership release failure")

    with monkeypatch.context() as faults:
        faults.setattr(maintenance, "database_ownership", fail_release)
        with pytest.raises(OSError, match="ownership release failure") as error:
            maintenance.restore_backup(bundle, db.parent)
    preserved, = bundle.parent.glob("pre-restore-*")
    note, = error.value.__notes__
    assert "Active database verified" in note and str(preserved) in note
    assert names(db) == [] and not pending_path(db).exists()
    maintenance.verify_backup(preserved)
    assert maintenance.create_backup(db.parent).is_file()


@pytest.mark.parametrize("exclusive", [False, True])
@pytest.mark.parametrize("nested_checkout", [False, True])
def test_cross_process_ownership_blocks_conflicting_work(db, tmp_path, exclusive, nested_checkout, monkeypatch):
    bundle = maintenance.create_backup(db.parent)
    ready = tmp_path / "ready"
    child = """
import asyncio, os, sys
from pathlib import Path
from riskengine.db.maintenance_lock import database_ownership

def hold():
    Path(sys.argv[2]).touch()
    if sys.stdin.buffer.read(1) == b"!":
        os._exit(18)

async def application():
    from riskengine.api.main import app
    async with app.router.lifespan_context(app):
        hold()

if sys.argv[3] == 'exclusive':
    with database_ownership(Path(sys.argv[1]), exclusive=True):
        hold()
else:
    asyncio.run(application())
"""
    monkeypatch.setenv("RISKENGINE_DB", str(db))
    monkeypatch.setenv("RISKENGINE_DEMO", "0")
    monkeypatch.setenv("RISKENGINE_API_MODE", "legacy-read-only")
    child_cwd = None
    if nested_checkout:
        child_cwd = db.parent / "application-checkout"
        shutil.copytree(main.ROOT / "src", child_cwd / "src", ignore=shutil.ignore_patterns("__pycache__"))
        monkeypatch.setenv("PYTHONPATH", str(child_cwd / "src"))
        monkeypatch.setenv("RISKENGINE_API_MODE", "protected")
        monkeypatch.setenv("RISKENGINE_BOOTSTRAP_CODE", "a" * 43)
        monkeypatch.setenv("RISKENGINE_API_PORT", "8000")
        monkeypatch.setenv("RISKENGINE_VITE_PORT", "5173")
        child = (
            "from pathlib import Path\n"
            "from riskengine.db.lifecycle import REPOSITORY_ROOT\n"
            "assert REPOSITORY_ROOT == Path.cwd()\n"
            + child
        )
    if exclusive and nested_checkout:
        # The authorized checkout takes exclusive ownership; the other
        # installation must refuse startup even though it cannot write there.
        with database_ownership(db, exclusive=True):
            blocked = subprocess.run(
                [sys.executable, "-c", child, str(db), str(ready), "application"],
                cwd=child_cwd, input=b"x", capture_output=True, timeout=45,
            )
            assert blocked.returncode != 0
            assert b"Managed database is in use" in blocked.stderr
            assert not ready.exists()
        pending_path(db).write_text("{}", encoding="utf-8")
        blocked = subprocess.run(
            [sys.executable, "-c", child, str(db), str(ready), "application"],
            cwd=child_cwd, input=b"x", capture_output=True, timeout=45,
        )
        assert blocked.returncode != 0
        assert b"Unresolved database maintenance" in blocked.stderr
        assert not ready.exists()
        return
    process = subprocess.Popen(
        [sys.executable, "-c", child, str(db), str(ready), "exclusive" if exclusive else "application"],
        stdin=subprocess.PIPE, cwd=child_cwd,
    )
    try:
        wait_for(ready, process)
        with pytest.raises(MaintenanceError, match="in use"):
            maintenance.restore_backup(bundle, db.parent)
        if exclusive:
            async def start():
                async with main.lifespan(main.app):
                    pytest.fail("Startup during maintenance")
            with pytest.raises(MaintenanceError, match="in use"):
                asyncio.run(start())
            with pytest.raises(MaintenanceError, match="in use"):
                maintenance.create_backup(db.parent)
        else:
            with pytest.raises(MaintenanceError, match="in use"):
                maintenance.recover_database(db.parent)
            assert maintenance.verify_backup(maintenance.create_backup(db.parent)).schema_version == 1
        process.communicate(input=b"!" if exclusive else b"x", timeout=10)
        assert process.returncode == (18 if exclusive else 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    maintenance.restore_backup(bundle, db.parent)


def test_cli_default_explicit_target_and_confirmation(db, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("RISKENGINE_DB", str(main.ROOT / "data" / "historical_portfolio.db"))
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    default = create_database()
    command = [sys.executable, "-m", "riskengine.db.maintenance"]

    def cli(*arguments):
        return subprocess.run(command + list(arguments), timeout=30, capture_output=True)

    assert cli("backup").returncode == 0
    assert len(list((default.parent / "backups").glob("*.riskbackup"))) == 1
    assert cli("backup", "--data-root", str(db.parent)).returncode == 0
    backup, = (db.parent / "backups").glob("*.riskbackup")
    assert cli("verify", str(backup)).returncode == 0
    assert cli("restore", str(backup), "--data-root", str(db.parent)).returncode == 2
    assert cli("restore", str(backup), "--data-root", str(db.parent), "--confirm").returncode == 0


def _interrupt_writer(db):
    insert(db, 1, "a" * 1_000_000)
    original = db.read_bytes()
    child = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA cache_size=4")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE portfolios SET name=?", ("b"*1_100_000,))
os._exit(19)
"""
    result = subprocess.run([sys.executable, "-c", child, str(db)], timeout=15)
    journal = db.with_name(db.name + "-journal")
    assert result.returncode == 19 and db.read_bytes() != original and journal.stat().st_size > 512
    return journal


@pytest.mark.parametrize("failure", [False, True])
def test_hot_journal_preservation_and_controlled_recovery(db, monkeypatch, failure):
    journal = _interrupt_writer(db)
    before = db.read_bytes(), journal.read_bytes()
    writes = ApplicationWrites(db, "override", protected=True, data_root=str(db.parent))
    with pytest.raises(ApplicationWriteError, match="recovery_required"):
        with writes.connect():
            pytest.fail("Normal application recovered a hot journal")
    with pytest.raises(MaintenanceError, match="recovery is required"):
        maintenance.create_backup(db.parent)
    assert (db.read_bytes(), journal.read_bytes()) == before
    if failure:
        def fail(path):
            raise OSError("injected rehearsal failure")

        monkeypatch.setattr(maintenance, "_recover_with_sqlite", fail)
        with pytest.raises(OSError, match="injected rehearsal failure"):
            maintenance.recover_database(db.parent)
        preserved, = (db.parent / "recovery").iterdir()
        assert (db.read_bytes(), journal.read_bytes()) == before
    else:
        result = maintenance.recover_database(db.parent)
        preserved = result.preserved
        assert not journal.exists() and not pending_path(db).exists()
        assert names(db) == [(1, "a" * 1_000_000)]
        TransactionExecutor(writes).run(
            lambda tx: tx.execute("INSERT INTO portfolios VALUES (2,'After recovery','USD')")
        )
        assert names(db)[1] == (2, "After recovery")
    assert ((preserved / db.name).read_bytes(), (preserved / journal.name).read_bytes()) == before


def test_pending_recovery_cannot_acknowledge_unrecovered_main_bytes(db, monkeypatch):
    journal = _interrupt_writer(db)
    recover = maintenance._recover_with_sqlite

    def fail_active(database):
        if database == db:
            raise OSError("injected active recovery failure")
        recover(database)

    monkeypatch.setattr(maintenance, "_recover_with_sqlite", fail_active)
    with pytest.raises(OSError, match="active recovery failure"):
        maintenance.recover_database(db.parent)
    marker = pending_path(db)
    assert json.loads(marker.read_text())["previous"] is None
    # Simulate a user separating a disposable hot journal from its database.
    journal.rename(journal.with_suffix(".retained"))
    monkeypatch.setattr(maintenance, "_standalone", lambda path: None)
    with pytest.raises(MaintenanceError, match="recorded valid states"):
        maintenance.check_active(db.parent)
    assert marker.exists()
