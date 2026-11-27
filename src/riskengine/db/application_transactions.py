"""Synchronous transaction boundary for authorized application mutations."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from contextvars import ContextVar
import inspect
import sqlite3
from threading import get_ident
from typing import Literal, TypeVar

from riskengine.db.application_writes import ApplicationWriteError, ApplicationWrites


Outcome = Literal["not_started", "rolled_back", "unknown", "committed"]
Result = TypeVar("Result")
_active = ContextVar("application_transaction_active", default=False)
_data_actions = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_RECURSIVE,
}


class MutationRejected(ValueError):
    """An internal operation rejected its inputs or the current financial state."""


class TransactionError(RuntimeError):
    """Path-free diagnosis. Causes and secondary exceptions are internal."""

    def __init__(self, reason: str, phase: str, outcome: Outcome):
        self.reason = reason
        self.phase = phase
        self.outcome: Outcome = outcome
        self.secondary: list[tuple[str, BaseException]] = []
        super().__init__(f"Application transaction failed ({reason}).")

    def record_secondary(self, reason: str, error: BaseException) -> None:
        self.secondary.append((reason, error))
        self.add_note(f"Secondary transaction failure ({reason}).")


class Transaction:
    """SQL execution and eagerly fetched rows on the calling thread."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection: sqlite3.Connection | None = connection
        self._owner = get_ident()
        self._failure: BaseException | None = None
        self._denied_sql = False

    def _authorize_sql(self, action, arg1, arg2, database, trigger):
        if action in _data_actions or (
            action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() != "load_extension"
        ):
            return sqlite3.SQLITE_OK
        self._denied_sql = True
        return sqlite3.SQLITE_DENY

    def execute(
        self, sql: str, parameters: Sequence | Mapping = (),
    ) -> tuple[tuple, ...]:
        """Execute one parameterized statement and materialize its rows."""
        connection = self._connection
        if connection is None or get_ident() != self._owner:
            raise TransactionError("invalid_scope", "operation", "not_started")
        if self._failure is not None:
            raise self._failure
        try:
            # SQLite can roll the transaction back itself (e.g. OR ROLLBACK).
            if not connection.in_transaction:
                raise TransactionError("transaction_ended", "operation", "rolled_back")
            with closing(connection.execute(sql, parameters)) as cursor:
                return tuple(cursor.fetchall())
        except BaseException as error:
            self._failure = error
            raise


def _validate_result(value: object) -> None:
    if type(value) in (type(None), bool, int, float, str, bytes):
        return
    if type(value) in (list, tuple):
        for item in value:
            _validate_result(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_result(item)
        return
    if inspect.iscoroutine(value):
        value.close()
    raise TransactionError("invalid_result", "operation", "not_started")


def _failure_reason(error: BaseException, phase: str, denied_sql: bool) -> str:
    if isinstance(error, TransactionError):
        return error.reason
    if denied_sql:
        return "forbidden_sql"
    if isinstance(error, MutationRejected):
        return "validation_rejected"
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", 0)
        if code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return "lock_contention"
        if phase == "operation":
            return "constraint_violation" if isinstance(error, sqlite3.IntegrityError) else "statement_failure"
    return f"{phase}_failure"


def _run(connection: sqlite3.Connection, operation: Callable[[Transaction], Result]) -> Result:
    phase = "begin"
    scope = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        phase = "operation"
        scope = Transaction(connection)
        connection.set_authorizer(scope._authorize_sql)
        try:
            result = operation(scope)
        finally:
            scope._connection = None
        if scope._failure is not None:
            raise scope._failure
        _validate_result(result)
        if not connection.in_transaction:
            raise TransactionError("transaction_ended", phase, "rolled_back")
        connection.set_authorizer(None)
        phase = "commit"
        connection.execute("COMMIT")
    except BaseException as error:
        first_statement_error = scope._failure if scope is not None else None
        primary = first_statement_error if isinstance(error, Exception) and first_statement_error else error
        failure = TransactionError(
            _failure_reason(primary, phase, scope is not None and scope._denied_sql),
            phase,
            "unknown",
        )
        if primary is not error:
            failure.record_secondary("operation_failure", error)
        try:
            if connection.in_transaction:
                connection.set_authorizer(None)
                connection.execute("ROLLBACK")
                if connection.in_transaction:
                    raise RuntimeError("SQLite transaction remained active after rollback.")
                failure.outcome = "rolled_back"
            elif phase == "begin":
                failure.outcome = "not_started"
            elif phase == "operation":
                # Callback SQL cannot commit. An inactive transaction here was rolled back.
                failure.outcome = "rolled_back"
        except BaseException as rollback_error:
            failure.record_secondary("rollback_failure", rollback_error)
        if not isinstance(primary, Exception):
            primary.add_note(f"Application transaction outcome: {failure.outcome}.")
            for note in getattr(failure, "__notes__", ()):
                primary.add_note(note)
            if primary is error:
                raise
            raise primary
        raise failure from primary
    return result


class TransactionExecutor:
    """Acquire a fresh managed connection and own its write transaction."""

    def __init__(self, writes: ApplicationWrites):
        self._writes = writes

    @property
    def writes_configured(self) -> bool:
        """Report configured eligibility. Every mutation still checks the database."""
        return self._writes.managed is not None and self._writes.denial_reason is None

    def probe(self, identity: tuple[int, int]) -> None:
        self._writes.probe(identity)

    def run(self, operation: Callable[[Transaction], Result], *,
            expected_identity: tuple[int, int] | None = None) -> Result:
        if _active.get():
            raise TransactionError("nested_execution", "operation", "not_started")
        if inspect.iscoroutinefunction(operation) or inspect.isasyncgenfunction(operation):
            raise TransactionError("async_operation", "operation", "not_started")
        token = _active.set(True)
        primary = None
        acquired = False
        try:
            try:
                grant = (self._writes.connect() if expected_identity is None
                         else self._writes.connect(expected_identity=expected_identity))
                with grant as connection:
                    acquired = True
                    try:
                        result = _run(connection, operation)
                    except BaseException as error:
                        # Close outside exception propagation so close cannot mask this error.
                        primary = error
            except BaseException as error:
                if primary is not None:
                    if isinstance(primary, TransactionError):
                        primary.record_secondary("cleanup_failure", error)
                    else:
                        primary.add_note("Secondary transaction failure (cleanup_failure).")
                elif not isinstance(error, Exception):
                    if acquired:
                        error.add_note("Application transaction outcome: committed.")
                    raise
                elif acquired:
                    raise TransactionError("cleanup_failure", "cleanup", "committed") from error
                elif isinstance(error, ApplicationWriteError):
                    reason = error.reason
                    if reason not in ("connection_failure", "recovery_required"):
                        reason = "authorization_denied"
                    failure = TransactionError(reason, "acquire", "not_started")
                    cleanup = getattr(error, "cleanup_error", None) or getattr(error.__cause__, "cleanup_error", None)
                    if cleanup is not None:
                        failure.record_secondary("cleanup_failure", cleanup)
                    raise failure from error
                else:
                    raise TransactionError("connection_failure", "acquire", "not_started") from error
            if primary is not None:
                raise primary
            return result
        finally:
            _active.reset(token)
