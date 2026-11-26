"""Back up, verify, restore and explicitly recover managed portfolio databases."""

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
from typing import TypeVar
from uuid import uuid4
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from riskengine.db.application_writes import ApplicationWrites, _closing
from riskengine.db.connection import get_connection
from riskengine.db.lifecycle import (
    DATABASE_NAME, SCHEMA_VERSION, DatabaseLifecycleError, _copy_snapshot, _finalize_database,
    _print_cli, _provisioning_file, _read_snapshot, _validate, managed_database_path,
)
from riskengine.db.maintenance_lock import MaintenanceError, database_ownership, exists, pending_path, sidecars


FORMAT_VERSION = 1
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class BackupInfo:
    format_version: int
    created_utc: str
    schema_version: int
    size: int
    sha256: str


@dataclass(frozen=True)
class MaintenanceResult:
    database: Path
    preserved: Path


_Result = TypeVar("_Result", Path, MaintenanceResult)


def _owned_operation(
    database: Path, run: Callable[[], _Result], *, exclusive: bool, allow_pending: bool = False,
) -> _Result:
    completed = None
    try:
        with database_ownership(database, exclusive=exclusive, allow_pending=allow_pending):
            completed = run()
            return completed
    except BaseException as error:
        if isinstance(completed, MaintenanceResult):
            error.add_note(
                f"Active database verified at {completed.database}. Preserved files at {completed.preserved}. "
                "Ownership cleanup did not finish."
            )
        elif completed is not None:
            error.add_note(f"Operation completed and verified at {completed}. Ownership cleanup did not finish.")
        raise


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()


def _validate_managed(connection: sqlite3.Connection) -> None:
    if _validate(connection) != SCHEMA_VERSION:
        raise MaintenanceError("Expected the managed schema version. Use lifecycle adoption for a legacy database.")


def _standalone(path: Path, *, archived: bool = False) -> None:
    ApplicationWrites._identity(path)
    if sidecars(path):
        raise MaintenanceError("SQLite sidecars are present. Close all SQLite users and resolve the sidecars through SQLite before retrying. Keep the files together.")
    if archived:
        # The read/write format bytes precede the schema and indicate whether
        # the image can depend on a WAL, even when opened as immutable.
        with path.open("rb") as image:
            header = image.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise MaintenanceError("Expected a standalone rollback-journal database image in the backup.")
    # Only for an operation-owned or exclusively held file with no sidecars.
    # Immutable access avoids creating WAL/SHM while inspecting clean WAL headers.
    with _closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        _validate_managed(connection)


@contextmanager
def _workspace(parent: Path | None = None) -> Iterator[Path]:
    owned = Path(tempfile.mkdtemp(prefix=".portfolio-maintenance-", dir=parent))
    primary = None
    try:
        yield owned
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            shutil.rmtree(owned)
        except OSError as cleanup:
            message = f"Temporary artifacts retained at {owned}."
            if primary is None:
                raise MaintenanceError(message) from cleanup
            primary.add_note(message)


def _artifact_directory(database: Path, name: str) -> Path:
    directory = database.parent / name
    directory.mkdir(exist_ok=True)
    if directory.resolve() != directory or (
        getattr(directory.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise MaintenanceError("Maintenance storage is a filesystem alias.")
    return directory


def _managed_target(data_root: Path | None) -> Path:
    database = managed_database_path(data_root)
    ApplicationWrites._identity(database)
    return database


def _bundle_name(kind: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{timestamp}-{uuid4().hex}.riskbackup"


def _manifest(database: Path) -> BackupInfo:
    return BackupInfo(
        FORMAT_VERSION, datetime.now(timezone.utc).isoformat(), SCHEMA_VERSION,
        database.stat().st_size, _digest(database),
    )


def _extract_backup(bundle: Path, destination: Path) -> BackupInfo:
    bundle = bundle.absolute()
    identity = ApplicationWrites._identity(bundle)
    try:
        with bundle.open("rb") as source, ZipFile(source) as archive:
            if sorted(archive.namelist()) != sorted((MANIFEST, DATABASE_NAME)):
                raise MaintenanceError("Expected a complete application backup bundle.")
            entries = (archive.getinfo(MANIFEST), archive.getinfo(DATABASE_NAME))
            if any(entry.compress_type != ZIP_STORED for entry in entries) or entries[0].file_size > 4096:
                raise MaintenanceError("Unsupported backup container.")
            try:
                info = BackupInfo(**json.loads(archive.read(MANIFEST)))
                timestamp = datetime.fromisoformat(info.created_utc)
                if (
                    type(info.format_version) is not int or info.format_version != FORMAT_VERSION
                    or type(info.schema_version) is not int or info.schema_version != SCHEMA_VERSION
                    or timestamp.utcoffset() != timezone.utc.utcoffset(None)
                    or type(info.size) is not int or info.size <= 0 or entries[1].file_size != info.size
                    or not _valid_digest(info.sha256)
                ):
                    raise ValueError("Invalid manifest values")
            except (TypeError, ValueError) as error:
                raise MaintenanceError("Unsupported or invalid backup manifest.") from error
            with archive.open(DATABASE_NAME) as archived_db, destination.open("xb") as output:
                shutil.copyfileobj(archived_db, output)
    except (BadZipFile, EOFError, RuntimeError) as error:
        raise MaintenanceError("Backup bundle is incomplete, unreadable or corrupt.") from error
    if ApplicationWrites._identity(bundle) != identity:
        raise MaintenanceError("Backup source changed during verification.")
    if destination.stat().st_size != info.size or _digest(destination) != info.sha256:
        raise MaintenanceError("Backup size or SHA-256 digest does not match the manifest.")
    _standalone(destination, archived=True)
    return info


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def verify_backup(bundle: Path) -> BackupInfo:
    """Verify a standalone bundle in disposable storage."""
    with _workspace() as work:
        return _extract_backup(Path(bundle), work / DATABASE_NAME)


def _backup(database: Path, kind: str, *, offline: bool = False) -> Path:
    identity = ApplicationWrites._identity(database)
    directory = _artifact_directory(database, "backups")
    bundle = directory / _bundle_name(kind)
    with _provisioning_file(bundle) as pending:
        with _workspace(directory) as work:
            snapshot = work / DATABASE_NAME
            try:
                if offline:
                    # Caller holds exclusive ownership; checkpointed main bytes include all commits.
                    _standalone(database)
                    shutil.copyfile(database, snapshot)
                else:
                    with _read_snapshot(database) as source:
                        _validate_managed(source)
                        with _closing(get_connection(snapshot)) as output:
                            _copy_snapshot(source, output)
            except sqlite3.Error as error:
                if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
                    raise MaintenanceError("Rollback journal recovery is required. Stop the application and use recover.") from error
                raise
            if ApplicationWrites._identity(database) != identity:
                raise MaintenanceError("Managed database changed during backup.")
            _finalize_database(snapshot)
            _standalone(snapshot)
            info = _manifest(snapshot)
            with ZipFile(pending, "w", compression=ZIP_STORED) as archive:
                archive.writestr(MANIFEST, json.dumps(asdict(info)))
                archive.write(snapshot, DATABASE_NAME)
            if _extract_backup(pending, work / "verification.db") != info:
                raise MaintenanceError("Verified backup metadata changed before publication.")
        if directory.resolve() != directory:
            raise MaintenanceError("Backup directory changed before publication.")
    return bundle


def create_backup(data_root: Path | None = None) -> Path:
    """Publish a verified online snapshot of an explicitly selected managed location."""
    database = _managed_target(data_root)
    return _owned_operation(database, lambda: _backup(database, "backup"), exclusive=False)


def _write_marker(database: Path, previous: str | None, expected: str) -> None:
    with pending_path(database).open("x", encoding="utf-8") as stream:
        json.dump({"previous": previous, "expected": expected}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def _check_active(database: Path, expected: str) -> None:
    _standalone(database)
    if _digest(database) != expected:
        raise MaintenanceError("Active database differs from the expected database. Keep all preservation artifacts.")


def restore_backup(bundle: Path, data_root: Path | None = None) -> MaintenanceResult:
    """Restore a verified bundle while retaining a mandatory safety backup."""
    database = _managed_target(data_root)

    def restore() -> MaintenanceResult:
        identity = ApplicationWrites._identity(database)
        _standalone(database)
        previous = _digest(database)
        preserved = None
        verified = False
        try:
            with _workspace(database.parent) as work:
                replacement = work / DATABASE_NAME
                info = _extract_backup(Path(bundle), replacement)
                preserved = _backup(database, "pre-restore", offline=True)
                if ApplicationWrites._identity(database) != identity or sidecars(database) or _digest(database) != previous:
                    raise MaintenanceError("Managed database changed before replacement.")
                _write_marker(database, previous, info.sha256)
                # Same-volume replace. No SQLite handles are held on either file.
                os.replace(replacement, database)
                _check_active(database, info.sha256)
                verified = True
            pending_path(database).unlink()
        except BaseException as error:
            if preserved is not None:
                error.add_note(f"Verified pre-restore backup retained at {preserved}.")
                if verified:
                    error.add_note("The restored database was verified. Cleanup or pending maintenance acknowledgement did not finish.")
                error.add_note("Check the active database and the retained safety backup. If maintenance is pending, use check-active before restarting.")
            raise
        return MaintenanceResult(database, preserved)

    return _owned_operation(database, restore, exclusive=True)


def _require_hot_journal(database: Path) -> Path:
    ApplicationWrites._identity(database)
    journal = database.with_name(database.name + "-journal")
    if sidecars(database) != (journal,):
        raise MaintenanceError("Controlled recovery requires a single rollback journal. Keep any WAL/SHM or other files together for offline investigation.")
    try:
        with _read_snapshot(database) as source:
            source.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
            return journal
        raise
    raise MaintenanceError("SQLite did not report a hot rollback journal. Keep the existing files intact.")


def _recover_with_sqlite(database: Path) -> None:
    with _closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=2)) as connection:
        _validate_managed(connection)


def recover_database(data_root: Path | None = None) -> MaintenanceResult:
    """Explicitly recover a single-database hot rollback journal through SQLite."""
    database = _managed_target(data_root)

    def recover() -> MaintenanceResult:
        journal = _require_hot_journal(database)
        originals = (database, journal)
        identities = tuple(ApplicationWrites._identity(path) for path in originals)
        hashes = tuple(_digest(path) for path in originals)
        directory = _artifact_directory(database, "recovery")
        preserved = Path(tempfile.mkdtemp(prefix="original-", dir=directory))
        try:
            for source, digest in zip(originals, hashes):
                saved = preserved / source.name
                shutil.copyfile(source, saved)
                if _digest(saved) != digest:
                    raise MaintenanceError("Original recovery files could not be preserved intact.")
            # Rehearse with the original basenames. The supported application
            # writes one database per transaction and denies ATTACH.
            with _workspace(database.parent) as work:
                for source in originals:
                    shutil.copyfile(preserved / source.name, work / source.name)
                rehearsal = work / database.name
                _require_hot_journal(rehearsal)
                _recover_with_sqlite(rehearsal)
                _standalone(rehearsal)
                expected = _digest(rehearsal)
            if (
                tuple(ApplicationWrites._identity(path) for path in originals) != identities
                or tuple(_digest(path) for path in originals) != hashes
                or sidecars(database) != (journal,)
            ):
                raise MaintenanceError("Original recovery files changed during preservation.")
            # The unrecovered main file is not a valid state to acknowledge, even
            # if someone later separates it from the hot journal.
            _write_marker(database, None, expected)
            _recover_with_sqlite(database)
            _check_active(database, expected)
            pending_path(database).unlink()
        except BaseException as error:
            error.add_note(f"Recovery artifacts retained at {preserved}. Keep original files together.")
            error.add_note("If maintenance is pending, verify the active database before restarting. Do not delete or edit journals.")
            raise
        return MaintenanceResult(database, preserved)

    return _owned_operation(database, recover, exclusive=True)


def check_active(data_root: Path | None = None) -> Path:
    """Validate and acknowledge a pending outcome if its expected active bytes are known."""
    database = _managed_target(data_root)

    def acknowledge() -> Path:
        marker = pending_path(database)
        ApplicationWrites._identity(marker)
        try:
            with marker.open("rb") as stream:
                expected = json.loads(stream.read(4097))
            if (
                set(expected) != {"previous", "expected"}
                or not _valid_digest(expected["expected"])
                or (expected["previous"] is not None and not _valid_digest(expected["previous"]))
            ):
                raise ValueError("Invalid marker")
        except (ValueError, TypeError, AttributeError) as error:
            raise MaintenanceError("Pending marker is invalid. Keep the database and all maintenance artifacts for offline inspection.") from error
        _standalone(database)
        if _digest(database) not in expected.values():
            raise MaintenanceError("Active database differs from the recorded valid states. Keep it offline and preserve all copies.")
        marker.unlink()
        return database

    return _owned_operation(database, acknowledge, exclusive=True, allow_pending=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "verify", "restore", "recover", "check-active"):
        command = commands.add_parser(name)
        if name in ("verify", "restore"):
            command.add_argument("bundle", type=Path)
        if name != "verify":
            command.add_argument("--data-root", type=Path)
        if name in ("restore", "recover", "check-active"):
            command.add_argument("--confirm", required=True, action="store_true", help="Acknowledge offline maintenance.")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            verified = verify_backup(args.bundle)
            message = f"Verified backup (schema {verified.schema_version}, SHA-256 {verified.sha256})."
        elif args.command == "backup":
            message = f"Verified backup published at {create_backup(args.data_root)}"
        elif args.command == "check-active":
            message = f"Verified active database and cleared pending maintenance at {check_active(args.data_root)}"
        else:
            result = (
                restore_backup(args.bundle, args.data_root) if args.command == "restore"
                else recover_database(args.data_root)
            )
            message = f"Verified active database at {result.database}. Preserved files at {result.preserved}"
        _print_cli(message, sys.stdout)
    except (DatabaseLifecycleError, RuntimeError, sqlite3.Error, OSError) as error:
        _print_cli(f"Maintenance did not complete: {error}", sys.stderr)
        for note in getattr(error, "__notes__", ()):
            _print_cli(note, sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
