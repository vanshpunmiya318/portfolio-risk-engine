"""Explicit provisioning tools for the local user's portfolio database."""

import argparse
from collections.abc import Iterator
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from time import monotonic
from typing import TextIO

from riskengine.db.connection import get_connection
from riskengine.db.schema.loader import create_schema, schema_sql


SCHEMA_VERSION = 1
DATABASE_NAME = "portfolio.db"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SIDECARS = ("-journal", "-wal", "-shm")
BACKUP_CONTENTION_SECONDS = 30


class DatabaseLifecycleError(ValueError):
    """A setup operation could not safely complete."""


def _database_files(path: Path) -> Iterator[Path]:
    yield path
    for suffix in SIDECARS:
        yield path.with_name(path.name + suffix)


def _invalid_windows_component(part: str) -> bool:
    if hasattr(os.path, "isreserved"):
        return os.path.isreserved(part)
    return Path(part).is_reserved() or ":" in part or part.endswith((" ", "."))


def managed_database_path(data_root: Path | None = None) -> Path:
    """Resolve the managed path without creating any directories."""
    if data_root is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data or not Path(local_app_data).is_absolute():
            raise DatabaseLifecycleError("Set LOCALAPPDATA or supply an absolute --data-root.")
        data_root = Path(local_app_data) / "Portfolio Risk Engine"
    root = Path(data_root)
    if not root.is_absolute() or root.drive.startswith("\\\\"):
        raise DatabaseLifecycleError("Choose an absolute local directory for --data-root.")
    if os.name == "nt" and any(_invalid_windows_component(part) for part in root.parts[1:]):
        raise DatabaseLifecycleError("Choose a valid Windows directory for --data-root.")
    root = root.resolve()
    if root == Path(root.anchor) or root.drive.startswith("\\\\"):
        raise DatabaseLifecycleError("Choose a dedicated local directory for --data-root.")
    if root.is_relative_to(REPOSITORY_ROOT) or REPOSITORY_ROOT.is_relative_to(root):
        raise DatabaseLifecycleError("Choose a data directory outside the application repository.")
    return root / DATABASE_NAME


def _schema_objects(connection: sqlite3.Connection) -> list[tuple]:
    # Table DDL includes column definitions, CHECKs, UNIQUEs and foreign keys.
    # Include explicit and automatically generated indexes and reject unknown objects.
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT IN ('sqlite_stat1', 'sqlite_stat4') "
        "ORDER BY type, name"
    ).fetchall()


def validate_structure(connection: sqlite3.Connection) -> None:
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(schema_sql())
        expected = _schema_objects(reference)
    actual = _schema_objects(connection)
    if actual != expected:
        expected_objects = {(kind, name): definition for kind, name, *definition in expected}
        actual_objects = {(kind, name): definition for kind, name, *definition in actual}
        differences = sorted(
            f"{kind} {name}" for kind, name in expected_objects.keys() | actual_objects.keys()
            if expected_objects.get((kind, name)) != actual_objects.get((kind, name))
        )
        raise DatabaseLifecycleError("Incompatible database schema: " + ", ".join(differences))


def _validate(connection: sqlite3.Connection) -> int:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise DatabaseLifecycleError(f"Unsupported schema version {version}. Use a compatible V1 database.")
    validate_structure(connection)
    if connection.execute("PRAGMA integrity_check(1)").fetchone() != ("ok",):
        raise DatabaseLifecycleError("SQLite integrity check failed. Use a verified backup.")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DatabaseLifecycleError("Database has foreign-key violations. Use a verified backup.")
    return version


def _source_identity(path: Path) -> tuple[int, int]:
    backup_help = "Use a standalone database produced by a supported SQLite backup or export."
    try:
        metadata = path.stat()
    except FileNotFoundError as error:
        raise DatabaseLifecycleError(f"Database file not found: {path}") from error
    except OSError as error:
        raise DatabaseLifecycleError(f"Could not verify source database identity: {path}. {backup_help}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise DatabaseLifecycleError(f"Source is not a regular database file: {path}. {backup_help}")
    if not metadata.st_dev or not metadata.st_ino or metadata.st_nlink < 1:
        raise DatabaseLifecycleError(f"Could not verify source identity or hard-link count: {path}. {backup_help}")
    if metadata.st_nlink != 1:
        raise DatabaseLifecycleError(
            f"Source database has multiple hard links: {path}. SQLite journal files depend on the filename. "
            + backup_help
        )
    return metadata.st_dev, metadata.st_ino


@contextmanager
def _read_snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    path = path.resolve()
    identity = _source_identity(path)
    with closing(get_connection(path, read_only=True)) as connection:
        if _source_identity(path) != identity:
            raise DatabaseLifecycleError("Source database changed while opening. Retry with a stable SQLite backup or export.")
        connection.execute("BEGIN")
        yield connection


def inspect_database(path: Path) -> int:
    """Return the version of a structurally compatible database, including V1 version zero."""
    with _read_snapshot(Path(path)) as connection:
        return _validate(connection)


def _check_destination(destination: Path) -> None:
    for path in _database_files(destination):
        # Includes dangling symlinks, reparse points and hard links.
        if os.path.lexists(path):
            raise DatabaseLifecycleError(f"Destination or SQLite sidecar already exists: {path}. Choose a new data root.")


def _publish(temporary: Path, destination: Path) -> None:
    # Windows rename fails if the target exists. On POSIX, linking provides
    # the equivalent atomic no-clobber operation on the same filesystem.
    if os.name == "nt":
        temporary.rename(destination)
    else:
        os.link(temporary, destination)


@contextmanager
def _provisioning_file(destination: Path) -> Iterator[Path]:
    _check_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.resolve() != destination.parent:
        raise DatabaseLifecycleError("Managed data directory changed during setup. Retry with a stable local path.")
    fd, name = tempfile.mkstemp(prefix=".portfolio-setup-", suffix=".db", dir=destination.parent)
    temporary = Path(name)
    primary_error = None
    try:
        os.close(fd)
        yield temporary
        _check_destination(destination)
        try:
            _publish(temporary, destination)
        except FileExistsError as error:
            raise DatabaseLifecycleError("Another operation created the destination. Choose a new data root.") from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors = []
        for path in _database_files(temporary):
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                cleanup_errors.append((path, error))
        if cleanup_errors:
            failure = primary_error or DatabaseLifecycleError("Could not finish temporary database cleanup.")
            for path, error in cleanup_errors:
                failure.add_note(f"Could not remove temporary artifact {path}: {error}")
            if primary_error is None:
                raise failure from cleanup_errors[0][1]


def _finalize_database(path: Path) -> None:
    with closing(get_connection(path)) as connection:
        # A source in WAL mode can carry that setting into its backup. Publish
        # a standalone file with no dependency on temporary SQLite sidecars.
        if connection.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise DatabaseLifecycleError("Could not finalize the database as a standalone file.")
        with connection:
            connection.execute("BEGIN")
            _validate(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def create_database(data_root: Path | None = None) -> Path:
    destination = managed_database_path(data_root)
    with _provisioning_file(destination) as temporary:
        create_schema(temporary)
        _finalize_database(temporary)
    return destination


def _copy_snapshot(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    contention_started = None

    def progress(status: int, _remaining: int, _total: int) -> None:
        nonlocal contention_started
        if status not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            contention_started = None
            return
        now = monotonic()
        if contention_started is None:
            contention_started = now
        elif now - contention_started >= BACKUP_CONTENTION_SECONDS:
            raise DatabaseLifecycleError("SQLite backup is repeatedly locked. Close other database tools and retry.")

    source.backup(target, pages=128, progress=progress, sleep=0.1)


def adopt_database(source: Path, data_root: Path | None = None) -> Path:
    destination = managed_database_path(data_root)
    _check_destination(destination)
    with _read_snapshot(Path(source)) as source_connection:
        _validate(source_connection)
        with _provisioning_file(destination) as temporary:
            with closing(get_connection(temporary)) as target_connection:
                _copy_snapshot(source_connection, target_connection)
            _finalize_database(temporary)
    return destination


def _print_cli(message: str, stream: TextIO) -> None:
    if stream.encoding:
        message = message.encode(stream.encoding, errors="backslashreplace").decode(stream.encoding)
    print(message, file=stream)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create an empty managed database.")
    adopt = commands.add_parser("adopt", help="Copy a compatible database into managed storage.")
    inspect = commands.add_parser("inspect", help="Verify compatibility and show the schema version.")
    for command in (create, adopt):
        command.add_argument("--data-root", type=Path, help="Use an alternate absolute local data directory.")
    for command in (adopt, inspect):
        command.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            version = inspect_database(args.database)
            _print_cli(f"Compatible database (schema version {version}).", sys.stdout)
        else:
            path = (
                create_database(args.data_root) if args.command == "create"
                else adopt_database(args.database, args.data_root)
            )
            _print_cli(f"Managed database (schema version {SCHEMA_VERSION}): {path}", sys.stdout)
            _print_cli("Set RISKENGINE_DB to this path and clear RISKENGINE_DEMO before starting the application.", sys.stdout)
    except (DatabaseLifecycleError, OSError, RuntimeError, sqlite3.Error) as error:
        _print_cli(f"Database setup failed: {error}", sys.stderr)
        for note in getattr(error, "__notes__", ()):
            _print_cli(note, sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
