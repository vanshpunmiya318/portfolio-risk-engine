"""Connection boundary for future protected local mutation services."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat

from riskengine.db.lifecycle import (
    SCHEMA_VERSION,
    DatabaseLifecycleError,
    _database_files,
    _source_identity,
    managed_database_path,
    validate_structure,
)


BUSY_TIMEOUT_SECONDS = 2
_TRANSACTION_OPTIONS = {"isolation_level": None}
if hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"):
    # Keep explicit BEGIN/COMMIT semantics if Python changes its default.
    _TRANSACTION_OPTIONS["autocommit"] = sqlite3.LEGACY_TRANSACTION_CONTROL


def _close_after_failure(connection: sqlite3.Connection, primary: BaseException) -> None:
    try:
        connection.close()
    except BaseException as error:
        primary.add_note("Secondary SQLite connection cleanup failure.")
        primary.cleanup_error = error


@contextmanager
def _closing(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield connection
    except BaseException as error:
        _close_after_failure(connection, error)
        raise
    else:
        connection.close()


class ApplicationWriteError(RuntimeError):
    """Internal failure with a stable reason and no path in its public text."""

    def __init__(self, reason: str):
        self.reason = reason
        message = f"Application database write unavailable ({reason})."
        if reason == "recovery_required":
            message += " SQLite requires rollback journal recovery before the database can be validated."
        super().__init__(message)


@dataclass(frozen=True, init=False)
class ApplicationWrites:
    """Startup-selected database and explicit authorization, with no request inputs."""

    selected: Path
    managed: Path | None
    denial_reason: str | None

    def __init__(
        self, selected: Path, selection_mode: str, *, protected: bool, data_root: str | None
    ) -> None:
        managed, denial_reason = self._authorize(selected, selection_mode, protected, data_root)
        object.__setattr__(self, "selected", selected)
        object.__setattr__(self, "managed", managed)
        object.__setattr__(self, "denial_reason", denial_reason)

    @staticmethod
    def _authorize(
        selected: Path, selection_mode: str, protected: bool, data_root: str | None
    ) -> tuple[Path | None, str | None]:
        if not protected:
            return None, "operating_mode"
        if selection_mode == "demo":
            return None, "demo"
        if selection_mode == "historical":
            return None, "protected_target"
        if selection_mode != "override" or not data_root:
            return None, "ineligible_target"
        try:
            managed = managed_database_path(None if data_root == "default" else Path(data_root))
        except (OSError, RuntimeError, ValueError):
            return None, "ineligible_target"
        if selected != managed:
            return None, "ineligible_target"
        return managed, None

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        try:
            resolved = path.resolve()
        except RuntimeError as error:
            raise ApplicationWriteError("file_identity") from error
        if resolved != path:
            raise ApplicationWriteError("protected_target")
        for database_file in _database_files(path):
            try:
                metadata = database_file.lstat()
            except FileNotFoundError:
                if database_file == path:
                    raise ApplicationWriteError("missing_target") from None
                continue
            if getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ApplicationWriteError("protected_target")
            if not stat.S_ISREG(metadata.st_mode):
                raise ApplicationWriteError("protected_target")
            try:
                identity = _source_identity(database_file)
            except DatabaseLifecycleError as error:
                raise ApplicationWriteError("file_identity") from error
            if database_file == path:
                database_identity = identity
        return database_identity

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone() != (SCHEMA_VERSION,):
            raise ApplicationWriteError("schema_version")
        try:
            validate_structure(connection)
        except DatabaseLifecycleError as error:
            raise ApplicationWriteError("incompatible_schema") from error

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        path = self.managed
        if self.denial_reason or path is None:
            raise ApplicationWriteError(self.denial_reason or "ineligible_target")
        if self.selected != path:
            raise ApplicationWriteError("ineligible_target")
        connection = None
        try:
            try:
                identity = self._identity(path)
                # Windows Path equality folds case, including on directories
                # configured for case-sensitive lookup.
                if str(self.selected) != str(path) and self._identity(self.selected) != identity:
                    raise ApplicationWriteError("file_identity")
                uri = path.as_uri()
                try:
                    with _closing(sqlite3.connect(
                        uri + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS, **_TRANSACTION_OPTIONS,
                    )) as probe:
                        if self._identity(path) != identity:
                            raise ApplicationWriteError("file_identity")
                        self._validate(probe)
                except sqlite3.Error as error:
                    if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_READONLY_ROLLBACK:
                        raise ApplicationWriteError("recovery_required") from error
                    raise
                # SQLite can fall back to a read-only handle for OS permission
                # failures. Check write-open permission without changing bytes.
                with path.open("r+b"):
                    pass
                if self._identity(path) != identity:
                    raise ApplicationWriteError("file_identity")
                connection = sqlite3.connect(
                    uri + "?mode=rw", uri=True, timeout=BUSY_TIMEOUT_SECONDS, **_TRANSACTION_OPTIONS,
                )
                if self._identity(path) != identity:
                    raise ApplicationWriteError("file_identity")
                connection.execute("PRAGMA foreign_keys = ON")
                if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                    raise ApplicationWriteError("connection_failure")
                self._validate(connection)
            except (sqlite3.Error, OSError) as error:
                raise ApplicationWriteError("connection_failure") from error
            yield connection
        except BaseException as error:
            if connection is not None:
                _close_after_failure(connection, error)
            raise
        else:
            if connection is not None:
                connection.close()
