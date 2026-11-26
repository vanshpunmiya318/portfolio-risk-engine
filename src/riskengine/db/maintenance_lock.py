"""Shared/exclusive ownership of a managed database location."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from riskengine.db.application_writes import ApplicationWrites, _closing
from riskengine.db.lifecycle import (
    DATABASE_NAME, DatabaseLifecycleError, _database_files, _provisioning_file, managed_database_path,
)


LOCK_APPLICATION_ID = 0x5052454C


class MaintenanceError(DatabaseLifecycleError):
    """A maintenance operation could not safely finish."""


def exists(path: Path) -> bool:
    """Detect dangling aliases and propagate inaccessible paths."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(file for file in _database_files(path) if file != path and exists(file))


def pending_path(database: Path) -> Path:
    return database.with_name(".portfolio-maintenance.json")


@contextmanager
def database_ownership(
    database: Path, *, exclusive: bool = False, allow_pending: bool = False,
) -> Iterator[None]:
    """Acquire ownership for a maintenance command at an authorized target."""
    if managed_database_path(database.parent) != database:
        raise MaintenanceError("The database location is outside the managed path policy.")
    with _location_ownership(database, exclusive=exclusive, allow_pending=allow_pending):
        yield


@contextmanager
def _location_ownership(
    database: Path, *, exclusive: bool = False, allow_pending: bool = False,
) -> Iterator[None]:
    """Coordinate by database location regardless of the reader's installation."""
    if database.parent.resolve(strict=True) != database.parent:
        raise MaintenanceError("Database ownership requires a canonical directory.")
    lock = database.with_name(".portfolio-access.lock")
    if not exists(lock):
        with _provisioning_file(lock) as temporary:
            with _closing(sqlite3.connect(temporary, isolation_level=None)) as initializer:
                initializer.execute(f"PRAGMA application_id={LOCK_APPLICATION_ID}")
    identity = ApplicationWrites._identity(lock)
    if not lock.stat().st_size or sidecars(lock):
        raise MaintenanceError("Unsafe database ownership file. Keep it in place and inspect it offline.")
    with _closing(sqlite3.connect(lock.as_uri() + "?mode=rw", uri=True, timeout=2, isolation_level=None)) as owner:
        if ApplicationWrites._identity(lock) != identity:
            raise MaintenanceError("Database ownership file changed while opening.")
        try:
            owner.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN")
            # BEGIN alone takes no shared reservation. Reading the schema does.
            if owner.execute("PRAGMA application_id").fetchone() != (LOCK_APPLICATION_ID,):
                raise MaintenanceError("Unrecognized ownership database. Keep it in place and inspect it offline.")
            if owner.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise MaintenanceError("Unsupported ownership journal mode. Keep the ownership database in place.")
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise MaintenanceError("Managed database is in use. Stop the application and other maintenance commands before retrying offline work.") from error
            raise
        if not allow_pending and exists(pending_path(database)):
            raise MaintenanceError("Unresolved database maintenance. Inspect preserved files and use check-active before restarting.")
        yield


@contextmanager
def application_database_ownership(database: Path) -> Iterator[None]:
    """Hold shared ownership for supported API access to a managed location."""
    if database == database.with_name(DATABASE_NAME):
        # The API has already resolved the selected path. A checkout's write
        # restrictions do not exempt its readers from taking this lock.
        with _location_ownership(database):
            yield
    else:
        yield
