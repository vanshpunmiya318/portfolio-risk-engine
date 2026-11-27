"""Compare the ten financial request variants against the published V2 checkpoint."""

from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
import subprocess
import sys
from types import ModuleType

from riskengine.api import main
from riskengine.db.connection import get_connection
from riskengine.db.lifecycle import adopt_database
from test_benchmark import golden_spy_db
from test_golden_portfolio import golden_db
from test_local_access import BROWSER_HOST, issue_cookie, serving
from test_portfolio_management import execute, prepare


CHECKPOINT = "3392739a641d70de718a6d8b808df68f9726fc74"
OLD_NAME = "Parity original label"
NEW_NAME = "Parity renamed label"


def variants(portfolio):
    prefix = f"/api/portfolios/{portfolio}"
    cutoff = "start_date=2026-01-05&end_date=2026-01-09"
    return [
        f"{prefix}/summary",
        "/api/portfolio/summary",
        f"{prefix}/holdings?as_of_date=2026-01-09",
        f"{prefix}/transactions",
        f"{prefix}/performance?{cutoff}",
        f"{prefix}/risk?{cutoff}&benchmark=ABSENT",
        f"{prefix}/allocation?as_of_date=2026-01-09",
        f"{prefix}/report?{cutoff}&benchmark=ABSENT",
        f"{prefix}/report/export?format=json&{cutoff}",
        f"{prefix}/report/export?format=markdown&{cutoff}",
    ]


def snapshot(port, cookie):
    result = {}
    for key in set(variants(1) + variants(2)):
        with closing(HTTPConnection("127.0.0.1", port, timeout=15)) as client:
            client.request("GET", key, headers={"Host": BROWSER_HOST, "Cookie": cookie})
            response = client.getresponse()
            result[key] = response.status, tuple(response.getheader(header) for header in
                                                ("Content-Type", "Content-Disposition", "Cache-Control")), response.read()
    return result


def test_checkpoint_parity_and_rename_preservation(golden_spy_db, tmp_path, monkeypatch):
    source, _ = golden_spy_db
    with source:
        source.execute("UPDATE portfolios SET name = ? WHERE id = 1", (OLD_NAME,))
        source.execute("INSERT INTO portfolios VALUES (2, 'Parity second', 'USD')")
        source.execute("INSERT INTO cash_flows VALUES (300, 2, '2026-01-05', 'DEPOSIT', 222, 'USD', 'parity')")
    root = tmp_path / "managed"
    path = adopt_database(Path(source.execute("PRAGMA database_list").fetchone()[2]), root)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    monkeypatch.setenv("RISKENGINE_WRITE_DATA_ROOT", str(root))
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)

    def ledger():
        with closing(get_connection(path, read_only=True)) as db:
            return ([db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                     for table in ("transactions", "cash_flows", "prices")],
                    db.execute("PRAGMA foreign_key_check").fetchall())

    published = ModuleType("published_phase25_checkpoint")
    published.__file__ = main.__file__
    source_bytes = subprocess.check_output(
        ["git", "show", CHECKPOINT + ":src/riskengine/api/main.py"], cwd=main.ROOT,
    )
    with monkeypatch.context() as isolated:
        isolated.setitem(sys.modules, published.__name__, published)
        exec(compile(source_bytes, main.__file__, "exec"), published.__dict__)
        isolated.setattr(main, "app", published.app)
        with serving(monkeypatch) as port:
            expected = snapshot(port, issue_cookie(port))

    before = ledger()
    with serving(monkeypatch) as port:
        cookie = issue_cookie(port)
        observed = snapshot(port, cookie)
        assert observed == expected
        assert all(status == 200 for status, _, _ in observed.values())
        assert observed[variants(1)[0]][2] != observed[variants(2)[0]][2]
        ticket = prepare(port, cookie, NEW_NAME, target=1)
        assert execute(port, cookie, ticket, NEW_NAME, target=1)[2]["outcome"] == "committed"
        actual = snapshot(port, cookie)
        assert actual == {path: (status, headers, body.replace(OLD_NAME.encode(), NEW_NAME.encode()))
                          for path, (status, headers, body) in expected.items()}
    assert ledger() == before
