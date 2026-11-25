import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Event, current_thread
from time import monotonic

import pytest

from riskengine.api import main
from riskengine.db.application_transactions import MutationRejected, TransactionError, TransactionExecutor
from riskengine.db.application_writes import ApplicationWrites
from riskengine.db.connection import get_read_only_connection
from riskengine.db.lifecycle import create_database


INSERT = "INSERT INTO portfolios VALUES (?, ?, 'USD')"
FLOW = "INSERT INTO cash_flows VALUES (?, ?, '2025-01-01', 'DEPOSIT', ?, 'USD', 'test')"


def executor_for(path, *, protected=True):
    return TransactionExecutor(ApplicationWrites(
        path, "override", protected=protected, data_root=str(path.parent),
    ))


@pytest.fixture
def managed(tmp_path):
    return create_database(tmp_path / "Transaction test # %")


def query(path, sql="SELECT id, name FROM portfolios ORDER BY id"):
    with closing(get_read_only_connection(path)) as connection:
        return connection.execute(sql).fetchall()


def assert_failure(failure, reason, phase, outcome):
    error = failure.value
    assert (error.reason, error.phase, error.outcome) == (reason, phase, outcome)


def test_committed_result_and_scope_lifetime(managed):
    scopes = []

    def operation(tx):
        scopes.append(tx)
        assert tx.execute(INSERT + " RETURNING id", (1, "First")) == ((1,),)
        tx.execute(FLOW, (1, 1, 200))
        assert query(managed) == []
        return {"portfolio": 1, "flows": tx.execute("SELECT amount FROM cash_flows")}

    assert executor_for(managed).run(operation) == {"portfolio": 1, "flows": ((200.0,),)}
    assert query(managed) == [(1, "First")]
    assert query(managed, "SELECT amount FROM cash_flows") == [(200.0,)]
    with pytest.raises(TransactionError) as error:
        scopes[0].execute("DELETE FROM portfolios")
    assert error.value.reason == "invalid_scope"
    assert query(managed) == [(1, "First")]


@pytest.mark.parametrize(
    ("failure", "reason", "cause"),
    [("before", "validation_rejected", MutationRejected),
     ("after", "validation_rejected", MutationRejected),
     ("unique", "constraint_violation", sqlite3.IntegrityError),
     ("fk", "constraint_violation", sqlite3.IntegrityError),
     ("statement", "statement_failure", sqlite3.OperationalError),
     ("unexpected", "operation_failure", RuntimeError)],
)
def test_atomic_failure_and_subsequent_success(managed, failure, reason, cause):
    def operation(tx):
        if failure == "before":
            raise MutationRejected("private record")
        tx.execute(INSERT, (1, "First"))
        if failure == "after":
            raise MutationRejected("private record")
        if failure == "unique":
            tx.execute(INSERT, (2, "First"))
        if failure == "fk":
            tx.execute(FLOW, (1, 99, 200))
        if failure == "statement":
            tx.execute("INSERT INTO unknown_table VALUES (1)")
        if failure == "unexpected":
            raise RuntimeError("private record")

    executor = executor_for(managed)
    with pytest.raises(TransactionError) as error:
        executor.run(operation)
    assert_failure(error, reason, "operation", "rolled_back")
    assert isinstance(error.value.__cause__, cause)
    assert "private record" not in str(error.value)
    assert query(managed) == []
    executor.run(lambda tx: tx.execute(INSERT, (2, "Next")))
    assert query(managed) == [(2, "Next")]


@pytest.mark.parametrize("sql", [
    "COMMIT", "END", "ROLLBACK", "BEGIN IMMEDIATE", "SAVEPOINT x", "RELEASE x",
    "PRAGMA foreign_keys=OFF", "ATTACH ':memory:' AS other", "CREATE TABLE unexpected(a)",
    "SELECT load_extension('extension')",
])
def test_sql_ownership_even_if_callback_catches_denial(managed, sql):
    def operation(tx):
        tx.execute(INSERT, (1, "First"))
        with pytest.raises(sqlite3.DatabaseError):
            tx.execute(sql)
        with pytest.raises(sqlite3.DatabaseError):
            tx.execute(INSERT, (2, "Second"))
        return "caught"

    with pytest.raises(TransactionError) as error:
        executor_for(managed).run(operation)
    assert_failure(error, "forbidden_sql", "operation", "rolled_back")
    assert query(managed) == []


def test_sqlite_implicit_rollback_cannot_resume_in_autocommit(managed):
    def operation(tx):
        tx.execute(INSERT, (1, "First"))
        with pytest.raises(sqlite3.IntegrityError):
            tx.execute("INSERT OR ROLLBACK INTO portfolios VALUES (2, 'First', 'USD')")
        with pytest.raises(sqlite3.IntegrityError):
            tx.execute(INSERT, (3, "Escape"))

    with pytest.raises(TransactionError) as error:
        executor_for(managed).run(operation)
    assert_failure(error, "constraint_violation", "operation", "rolled_back")
    assert query(managed) == []


@pytest.mark.parametrize(("handling", "error_type", "stage"), [
    ("return", KeyboardInterrupt, "execute"),
    ("retry", KeyboardInterrupt, "execute"),
    ("propagate", KeyboardInterrupt, "execute"),
    ("return", SystemExit, "fetch"),
    ("translate", KeyboardInterrupt, "execute"),
])
def test_statement_interruption_poison_survives_callback_handling(
    managed, monkeypatch, handling, error_type, stage,
):
    real_connect = sqlite3.connect
    interruption = error_type("injected statement interruption")
    handles = []
    scopes = []

    class Cursor(sqlite3.Cursor):
        def fetchall(self):
            raise interruption

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "SELECT 123":
                if stage == "execute":
                    raise interruption
                return self.cursor(factory=Cursor).execute(sql, parameters)
            if parameters == (9, "Unexpected"):
                pytest.fail("Poisoned scope reached SQLite again")
            return super().execute(sql, parameters)

    def connect(database, **kwargs):
        if str(database).endswith("?mode=rw"):
            connection = real_connect(database, factory=Connection, **kwargs)
            handles.append(connection)
            return connection
        return real_connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    executor = executor_for(managed)

    def operation(tx):
        scopes.append(tx)
        tx.execute(INSERT, (1, "Earlier"))
        if handling == "propagate":
            tx.execute("SELECT 123")
        with pytest.raises(error_type) as caught:
            tx.execute("SELECT 123")
        assert caught.value is interruption
        if handling == "retry":
            with pytest.raises(error_type) as retry:
                tx.execute(INSERT, (9, "Unexpected"))
            assert retry.value is interruption
        if handling == "translate":
            raise MutationRejected("Callback translated the interruption")
        return "Normally returned result"

    with pytest.raises(error_type) as stopped:
        executor.run(operation)
    assert stopped.value is interruption
    assert "Application transaction outcome: rolled_back." in interruption.__notes__
    if handling == "translate":
        assert "Secondary transaction failure (operation_failure)." in interruption.__notes__
    assert query(managed) == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        handles[0].execute("SELECT 1")
    with pytest.raises(TransactionError, match="invalid_scope"):
        scopes[0].execute("SELECT 1")
    executor.run(lambda tx: tx.execute(INSERT, (2, "After interruption")))
    assert query(managed) == [(2, "After interruption")]


def test_interruption_after_real_commit_keeps_unknown_outcome(managed, monkeypatch):
    real_connect = sqlite3.connect
    interruption = KeyboardInterrupt("injected after commit")

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql == "COMMIT":
                cursor.close()
                raise interruption
            return cursor

    def connect(database, **kwargs):
        if str(database).endswith("?mode=rw"):
            return real_connect(database, factory=Connection, **kwargs)
        return real_connect(database, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        with pytest.raises(KeyboardInterrupt) as stopped:
            executor_for(managed).run(lambda tx: tx.execute(INSERT, (1, "Committed")))
    assert stopped.value is interruption
    assert "Application transaction outcome: unknown." in interruption.__notes__
    assert query(managed) == [(1, "Committed")]


def test_nested_async_and_resource_results_are_rejected(managed):
    executor = executor_for(managed)

    def nested(tx):
        tx.execute(INSERT, (1, "First"))
        return executor.run(lambda inner: pytest.fail("Nested callback ran"))

    async def async_operation(tx):
        pytest.fail("Coroutine ran")

    with pytest.raises(TransactionError) as error:
        executor.run(nested)
    assert_failure(error, "nested_execution", "operation", "rolled_back")
    with pytest.raises(TransactionError) as error:
        executor.run(async_operation)
    assert_failure(error, "async_operation", "operation", "not_started")
    with pytest.raises(TransactionError) as error:
        executor.run(lambda tx: {"scope": tx})
    assert_failure(error, "invalid_result", "operation", "rolled_back")
    assert query(managed) == []
    executor.run(lambda tx: tx.execute(INSERT, (2, "Next")))


def test_denied_or_missing_never_calls_operation(managed):
    callback = lambda tx: pytest.fail("Ineligible callback ran")
    missing = managed.parent.parent / "missing" / "portfolio.db"
    for executor, detail in (
        (executor_for(managed, protected=False), "operating_mode"),
        (executor_for(missing), "missing_target"),
    ):
        with pytest.raises(TransactionError) as error:
            executor.run(callback)
        assert_failure(error, "authorization_denied", "acquire", "not_started")
        assert error.value.__cause__.reason == detail
    assert not missing.parent.exists()
    assert query(managed) == []


def test_concurrent_state_validation_follows_write_reservation(managed, monkeypatch):
    # A test-only capacity of one row. Both writers would pass a stale read of zero.
    executor = executor_for(managed)
    first_inside, second_at_begin, release = Event(), Event(), Event()
    observations = []
    real_connect = sqlite3.connect
    follower_thread = None

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE" and current_thread() is follower_thread:
                second_at_begin.set()
            return super().execute(sql, parameters)

    def connect(database, **kwargs):
        if str(database).endswith("?mode=rw"):
            return real_connect(database, factory=Connection, **kwargs)
        return real_connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)

    def claim(tx, identity):
        count, = tx.execute("SELECT COUNT(*) FROM portfolios")[0]
        observations.append((identity, count))
        if identity == 1:
            first_inside.set()
            assert release.wait(5)
        if count:
            raise MutationRejected("Capacity reached")
        tx.execute(INSERT, (identity, str(identity)))
        return identity

    def second():
        nonlocal follower_thread
        follower_thread = current_thread()
        return executor.run(lambda tx: claim(tx, 2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(executor.run, lambda tx: claim(tx, 1))
        try:
            assert first_inside.wait(5)
            follower = pool.submit(second)
            assert second_at_begin.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) == 1
        with pytest.raises(TransactionError) as error:
            follower.result(timeout=5)
    assert_failure(error, "validation_rejected", "operation", "rolled_back")
    assert observations == [(1, 0), (2, 1)]
    assert query(managed) == [(1, "1")]


@pytest.mark.parametrize("phase", ["begin", "commit"])
def test_real_sqlite_contention_and_explicit_retry(managed, phase):
    calls = []

    def operation(tx):
        calls.append(1)
        tx.execute(INSERT, (1, "First"))

    executor = executor_for(managed)
    with closing(sqlite3.connect(managed, isolation_level=None)) as external:
        external.execute("BEGIN IMMEDIATE" if phase == "begin" else "BEGIN")
        if phase == "commit":
            assert external.execute("SELECT * FROM portfolios").fetchall() == []
        start = monotonic()
        try:
            with pytest.raises(TransactionError) as error:
                executor.run(operation)
        finally:
            external.execute("ROLLBACK")
        elapsed = monotonic() - start
    assert 1.8 <= elapsed < 10
    assert_failure(error, "lock_contention", phase, "not_started" if phase == "begin" else "rolled_back")
    assert calls == ([] if phase == "begin" else [1])
    assert query(managed) == []
    executor.run(operation)
    assert query(managed) == [(1, "First")]


@pytest.mark.parametrize("fault", [
    "acquire_and_close", "begin", "unknown_commit", "rollback_and_close", "close_after_commit",
])
def test_injected_failures_preserve_primary_and_outcome(managed, monkeypatch, fault):
    real_connect = sqlite3.connect
    active_fault = fault
    operation_calls = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if active_fault == "acquire_and_close" and sql == "PRAGMA foreign_keys = ON":
                raise sqlite3.OperationalError("injected initialization")
            if active_fault == "begin" and sql == "BEGIN IMMEDIATE":
                raise sqlite3.OperationalError("injected begin")
            if active_fault == "unknown_commit" and sql == "COMMIT":
                super().execute(sql, parameters).close()
                raise sqlite3.OperationalError("injected after commit")
            if active_fault == "rollback_and_close" and sql == "ROLLBACK":
                raise sqlite3.OperationalError("injected rollback")
            return super().execute(sql, parameters)

        def close(self):
            super().close()
            if active_fault in ("acquire_and_close", "rollback_and_close", "close_after_commit"):
                raise sqlite3.OperationalError("injected close")

    def connect(database, **kwargs):
        if str(database).endswith("?mode=rw"):
            return real_connect(database, factory=Connection, **kwargs)
        return real_connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    executor = executor_for(managed)

    def operation(tx):
        operation_calls.append(1)
        tx.execute(INSERT, (1, "First"))
        if active_fault == "rollback_and_close":
            raise MutationRejected("primary")

    with pytest.raises(TransactionError) as error:
        executor.run(operation)
    expected = {
        "acquire_and_close": ("connection_failure", "acquire", "not_started"),
        "begin": ("begin_failure", "begin", "not_started"),
        "unknown_commit": ("commit_failure", "commit", "unknown"),
        "rollback_and_close": ("validation_rejected", "operation", "unknown"),
        "close_after_commit": ("cleanup_failure", "cleanup", "committed"),
    }
    assert_failure(error, *expected[fault])
    assert operation_calls == ([] if fault in ("acquire_and_close", "begin") else [1])
    if fault == "acquire_and_close":
        assert str(error.value.__cause__.__cause__) == "injected initialization"
        assert [reason for reason, _ in error.value.secondary] == ["cleanup_failure"]
    if fault == "rollback_and_close":
        assert isinstance(error.value.__cause__, MutationRejected)
        assert [reason for reason, _ in error.value.secondary] == ["rollback_failure", "cleanup_failure"]
    assert query(managed) == ([(1, "First")] if fault in ("unknown_commit", "close_after_commit") else [])
    active_fault = None
    executor.run(lambda tx: tx.execute(INSERT, (2, "Next")))
    assert query(managed)[-1] == (2, "Next")


def test_test_only_http_mutation_requires_session_and_request_proofs(managed, monkeypatch):
    from fastapi import Request

    monkeypatch.setenv("RISKENGINE_DB", str(managed))
    monkeypatch.setenv("RISKENGINE_WRITE_DATA_ROOT", str(managed.parent))
    monkeypatch.setenv("RISKENGINE_DEMO", "0")
    monkeypatch.setenv("RISKENGINE_API_MODE", "protected")
    monkeypatch.setenv("RISKENGINE_BOOTSTRAP_CODE", "a" * 43)
    monkeypatch.setenv("RISKENGINE_API_PORT", "8000")
    monkeypatch.setenv("RISKENGINE_VITE_PORT", "5173")
    app = main.PortfolioAPI(lifespan=main.lifespan)

    @app.post("/api/test-only")
    def mutate(request: Request):
        request.app.state.database_transactions.run(lambda tx: tx.execute(INSERT, (1, "Authorized")))
        return {"ok": True}

    async def post(path, **headers):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        headers = {"host": "127.0.0.1:5173", **headers}
        await app({
            "type": "http", "method": "POST", "path": path, "root_path": "", "query_string": b"",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
            "scheme": "http", "http_version": "1.1", "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 10000),
        }, receive, send)
        return messages[0]["status"], dict(messages[0]["headers"])

    async def check():
        origin = "http://127.0.0.1:5173"
        async with app.router.lifespan_context(app):
            proofs = {"origin": origin, "x-riskengine-request": "1"}
            assert (await post("/api/test-only", **proofs))[0] == 401
            status, headers = await post("/api/local-session", origin=origin, **{"x-riskengine-bootstrap": "a" * 43})
            assert status == 204
            cookie = headers[b"set-cookie"].split(b";")[0].decode()
            for override, expected in (
                ({"origin": "http://example.invalid"}, 403),
                ({"x-riskengine-request": "0"}, 403),
                ({"host": "example.invalid"}, 400),
                ({"sec-fetch-site": "cross-site"}, 403),
            ):
                assert (await post("/api/test-only", cookie=cookie, **(proofs | override)))[0] == expected
            assert query(managed) == []
            status, headers = await post("/api/test-only", cookie=cookie, **proofs)
            assert (status, headers[b"cache-control"]) == (200, b"no-store")

    asyncio.run(check())
    assert query(managed) == [(1, "Authorized")]
