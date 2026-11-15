"""Export purity, complete documents, financial presentation and HTTP semantics."""
import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from test_report_api import http_get as preview_get
from riskengine.api import main
from riskengine.api.report_models import ReportResponse
from riskengine.risk import performance_service, report_service
from riskengine.risk.report_exports import report_json, report_markdown, report_filename


def request_export(pid=1, **query):
    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        async with main.app.router.lifespan_context(main.app):
            await main.app({"type": "http", "http_version": "1.1", "method": "GET",
                            "path": f"/api/portfolios/{pid}/report/export", "root_path": "",
                            "query_string": urlencode(query).encode(), "headers": [], "scheme": "http",
                            "server": ("test", 80), "client": ("test", 1)}, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        return start["status"], dict((k.decode(), v.decode()) for k, v in start["headers"]), b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return asyncio.run(run())


@pytest.fixture
def api(golden_spy_db, monkeypatch):
    connection, pid = golden_spy_db
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", connection.execute("PRAGMA database_list").fetchone()[2])

    def call(format="json", **query):
        connection.commit()
        return request_export(pid, format=format, **query)
    return call


@pytest.fixture
def report(golden_spy_db):
    connection, pid = golden_spy_db
    connection.commit()
    connection.execute("BEGIN")
    try:
        return report_service.get_report_response(connection, pid)
    finally:
        connection.rollback()


def test_complete_json_and_pure_formatters_do_not_mutate_or_read_database(report, monkeypatch):
    expected = copy.deepcopy(report.model_dump(mode="json"))

    def forbidden(*args, **kwargs):
        pytest.fail("Pure formatters must not read or compose financial results")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(main, "get_connection", forbidden)
    monkeypatch.setattr(report_service, "get_report_response", forbidden)
    monkeypatch.setattr(performance_service, "prepare_analysis", forbidden)
    text = report_json(report)
    assert json.loads(text) == expected
    assert ReportResponse.model_validate_json(text) == report
    assert report_markdown(report) == report_markdown(report)
    assert report.model_dump(mode="json") == expected
    assert '"target_allocation":null' in text
    assert '"persisted_snapshot_id":null' in text
    assert len(json.loads(text)["performance"]["valuations"]) == 5


def test_markdown_financial_values_dates_units_and_disclosures(report):
    text = report_markdown(report)
    assert "| Total equity | 20,292.00 | USD |" in text
    assert "| Invested holdings value | 2,852.00 | USD |" in text
    assert "| Cash balance | 17,440.00 | USD |" in text
    assert "| Net external contributions through cutoff | 20,000.00 | USD |" in text
    assert "| Remaining average-cost basis | 2,600.00 | USD |" in text
    assert "| Cumulative return | 1.46% | 3.00% | 4 | 4 |" in text
    assert "| Excess cumulative return | -1.54 pp | 4 |" in text
    assert "| Exact endpoint cutoff | 2026-01-09 |" in text
    assert "| Historical allocation reference | 2026-01-05 |" in text
    assert "(ratio)" in text
    assert "Not applicable" in text
    assert "Each download is freshly composed" in text
    assert "Matching parameters and cutoff dates do not guarantee identity with a separately requested preview" in text
    assert "asof date is a reporting cutoff, not a snapshot identifier" in text
    assert "dividends are not reinvested" in text
    assert "Historical starting weights are not investment-policy targets" in text
    assert "not underlying ETF holdings or look-through exposures" in text
    assert "No persisted snapshot identifier" in text
    assert "Legacy Attribution is excluded" in text


def test_null_zero_sign_and_metric_unit_contracts(report):
    data = report.model_dump(mode="json")
    data["endpoint"]["cash_balance"] = {"value": 0.0, "unavailable_reasons": []}
    reason = {"code": "test_unavailable", "message": "Price is unavailable", "scope": "endpoint"}
    data["endpoint"]["invested_value"] = {"value": None, "unavailable_reasons": [reason]}
    data["risk"]["portfolio_metrics"]["historical_var"]["value"] = -.0125
    data["risk"]["portfolio_metrics"]["historical_cvar"]["value"] = -.005
    data["risk"]["portfolio_metrics"]["maximum_drawdown"]["value"] = -.025
    data["performance"]["portfolio_metrics"]["cumulative_return"]["value"] = 0
    # MetricValue explicitly carries units; even unusual valid assignments must
    # follow those units rather than hardcoding percentages from metric names.
    data["performance"]["portfolio_metrics"]["annualized_return"].update(value=.125, unit="ratio")
    data["performance"]["comparison"]["excess_annualized_return"].update(value=.125, unit="ratio")
    data["allocation"]["changes"][0]["weight_change_fraction"] = 0
    updated = ReportResponse.model_validate(data)
    text = report_markdown(updated)
    assert "| Cash balance | 0.00 | USD |" in text
    assert "| Invested holdings value | Unavailable | USD |" in text
    assert "| Cumulative return | 0.00%" in text
    assert "| Annualized return | 0.12 (ratio)" in text
    assert "| Excess annualized return | 0.12 (ratio)" in text
    assert "| Historical VaR (loss convention) | -1.25%" in text
    assert "| Historical CVaR (loss convention) | -0.50%" in text
    assert "| Maximum drawdown | -2.50%" in text
    assert "0.00 pp" in text
    assert "test\\_unavailable" in text and "Price is unavailable" in text
    assert json.loads(report_json(updated)) == updated.model_dump(mode="json")


def test_escaping_and_diagnostic_context_and_date_summaries(report):
    data = report.model_dump(mode="json")
    hostile = "A|B\r\n# Heading <script>alert(1)</script> [link](https://example.com) `code` *bold* \\"
    data["name"] = hostile
    data["holdings"]["holdings"][0]["instrument_name"] = hostile
    diagnostic = {"code": "missing_price", "message": hostile, "scope": "portfolio", "category": "coverage",
                  "metric": "portfolio.total_equity", "ticker": "QQQ", "start_date": "2026-01-07", "end_date": "2026-01-07"}
    data["warnings"] = [diagnostic, diagnostic, dict(diagnostic, start_date="2026-01-09", end_date="2026-01-09")]
    data["performance"]["warnings"] = [diagnostic]
    data["risk"]["warnings"] = [dict(diagnostic, start_date=None)]
    text = report_markdown(ReportResponse.model_validate(data))
    assert "A\\|B #" not in text
    assert "A\\|B \\# Heading &lt;script&gt;" in text
    assert "<script>" not in text and "\n# Heading" not in text
    assert "\\[link\\]" in text and "\\`code\\`" in text and "\\*bold\\*" in text
    assert "missing\\_price" in text and "QQQ" in text
    assert "portfolio.total\\_equity" in text
    assert "3 dated occurrences; 2026-01-07 to 2026-01-09 (2 distinct dates)" in text
    assert "performance.warnings" in text and "risk.warnings" in text
    assert "exact occurrences remain in JSON" in text


def test_missing_diagnostic_dates_are_not_described_as_date_independent(report):
    data = report.model_dump(mode="json")
    data["warnings"] = [{"code": "undated", "message": "Dates were not supplied", "scope": "benchmark"}]
    changed = ReportResponse.model_validate(data)
    before = report_json(changed)
    text = report_markdown(changed)
    row = next(line for line in text.splitlines() if line.startswith("| undated |"))
    assert "Date not supplied; see section cutoff" in row
    assert "context:" not in row  # no guess from the overall report period
    assert "Not date-specific" not in text
    assert report_json(changed) == before


@pytest.mark.parametrize("known", [True, False])
def test_reference_context_uses_only_explicit_reference_date(report, known):
    data = report.model_dump(mode="json")
    reference = data["allocation"]["reference"]
    reference["snapshot"] = None
    reference["unavailable_reasons"] = [{"code": "invalid_reference", "message": "Incomplete corporate action coverage", "scope": "reference"}]
    if not known:
        reference["reference_date"] = None
    changed = ReportResponse.model_validate(data)
    before = report_json(changed)
    text = report_markdown(changed)
    row = next(line for line in text.splitlines() if line.startswith("| invalid\\_reference |"))
    assert "Incomplete corporate action coverage" in row
    assert "Date not supplied; see section cutoff" in row
    assert ("reference date context: 2026-01-05" in row) is known
    assert "2026-01-09" not in row  # never replace reference with endpoint date
    assert "corporate-action coverage intervals" in text
    assert report_json(changed) == before


def test_supplied_diagnostic_boundaries_are_preserved_without_cutoff_substitution(report):
    data = report.model_dump(mode="json")
    data["allocation"]["reference"]["unavailable_reasons"] = []
    data["warnings"] = [{"code": "dated", "message": "Known boundary only", "scope": "portfolio",
                         "start_date": None, "end_date": "2026-01-07"}]
    text = report_markdown(ReportResponse.model_validate(data))
    row = next(line for line in text.splitlines() if line.startswith("| dated |"))
    assert "Unavailable to 2026-01-07" in row
    assert "2026-01-09" not in row and "context:" not in row


def test_corporate_action_policy_discloses_source_dependent_stored_coverage(report):
    before = report_json(report)
    text = report_markdown(report)
    assert "Corporate-action validation is source-dependent" in text
    assert "active holdings valued with Tiingo raw closes" in text
    assert "continuous stored Tiingo corporate-action coverage" in text
    assert "instrument's first portfolio trade through the valuation cutoff" in text
    assert "Other price sources use adjusted close and do not undergo this same Tiingo coverage check" in text
    assert "identical corporate-action coverage guarantees must not be assumed across sources" in text
    assert "Complete price coverage alone does not establish corporate-action coverage" in text
    assert "Latest price dates and recorded price sources are not evidence of certified action-coverage intervals" in text
    assert "not an independent audit of all corporate actions" in text
    assert "2026-10-23" not in text  # general policy must not use the historical DB's dates
    assert report_json(report) == before


@pytest.mark.parametrize("format,extension,content_type", [
    ("json", "json", "application/json"), ("markdown", "md", "text/markdown; charset=utf-8")])
def test_http_headers_complete_equality_and_single_composition_snapshot(api, monkeypatch, format, extension, content_type):
    captured, prepared, opened, statements = [], [], [], []
    compose, prepare, connect = main.get_report_response, performance_service.prepare_analysis, main.get_connection

    def tracked_compose(connection, *args):
        assert connection.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE portfolios SET name=name")
        result = compose(connection, *args)
        captured.append(result)
        return result

    def tracked_prepare(*args, **kwargs):
        prepared.append(args[0])
        return prepare(*args, **kwargs)

    def tracked_connection(path, *, read_only):
        assert read_only
        connection = connect(path, read_only=read_only)
        connection.set_trace_callback(statements.append)
        opened.append(connection)
        return connection

    monkeypatch.setattr(main, "get_report_response", tracked_compose)
    monkeypatch.setattr(performance_service, "prepare_analysis", tracked_prepare)
    monkeypatch.setattr(main, "get_connection", tracked_connection)
    status, headers, body = api(format, start_date="2026-01-05", end_date="2026-01-09", confidence=.625)
    assert status == 200
    assert len(captured) == len(prepared) == len(opened) == 1
    assert prepared[0] is opened[0]
    assert [sql for sql in statements if sql.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))] == ["BEGIN"]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = opened[0].in_transaction
    assert headers["content-type"] == content_type
    assert headers["content-disposition"] == (
        f'attachment; filename="portfolio-1-report-2026-01-05-to-2026-01-09-asof-2026-01-09.{extension}"')
    assert headers["cache-control"] == "no-store"
    if format == "json":
        assert json.loads(body) == captured[0].model_dump(mode="json")
        assert ReportResponse.model_validate_json(body) == captured[0]
    else:
        assert body.decode() == report_markdown(captured[0])
        assert "62.50%" in body.decode()


@pytest.mark.parametrize("query", [
    {}, {"format": "pdf"}, {"format": "JSON"}, {"format": ""},
    {"format": "json", "confidence": "nan"}, {"format": "markdown", "confidence": 1},
    {"format": "json", "start_date": "2026-02-30"}, {"format": "json", "end_date": "20260109"},
    {"format": "markdown", "benchmark": " "},
    {"format": "json", "start_date": "2026-01-09", "end_date": "2026-01-05"},
])
def test_invalid_query_errors_are_json_and_no_database_is_opened(api, monkeypatch, query):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid export request opened database")
    monkeypatch.setattr(main, "get_connection", forbidden)
    status, headers, body = request_export(**query)
    assert status == 422 and headers["content-type"] == "application/json"
    assert json.loads(body)["detail"]
    assert "content-disposition" not in headers


@pytest.mark.parametrize("pid,expected", [(0, 422), (-1, 422), ("bad", 422), (9223372036854775808, 422), (999, 404)])
def test_invalid_and_missing_ids(api, pid, expected):
    assert request_export(pid, format="markdown")[0] == expected


def test_missing_database(api, monkeypatch, tmp_path):
    monkeypatch.setenv("RISKENGINE_DB", str(tmp_path / "absent.db"))
    assert request_export(format="json")[0] == 404


@pytest.mark.parametrize("case", ["missing_benchmark", "missing_price", "currency", "invalid_reference", "non_session"])
def test_partial_export_matches_preview_values_and_reasons(api, golden_spy_db, case):
    connection, pid = golden_spy_db
    query = {}
    if case == "missing_benchmark":
        query["benchmark"] = "ABSENT"
    elif case == "missing_price":
        connection.execute("DELETE FROM prices WHERE date='2026-01-09' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    elif case == "currency":
        connection.execute("UPDATE cash_flows SET currency='EUR'")
    elif case == "invalid_reference":
        connection.execute("UPDATE prices SET source='tiingo' WHERE date='2026-01-05'")
        connection.execute("DELETE FROM corporate_action_coverage")
        query["start_date"] = "2026-01-06"
    else:
        query["end_date"] = "2026-01-10"
    connection.commit()
    preview_status, preview = preview_get(pid, **query)
    status, _, body = api("json", **query)
    assert status == preview_status == 200
    assert json.loads(body) == preview
    status, _, body = api("markdown", **query)
    text = body.decode()
    assert status == 200 and "Unavailable" in text
    assert "## Availability reasons and diagnostics" in text
    if case == "missing_benchmark":
        assert "ABSENT" in text and "20,292.00" in text
    elif case == "missing_price":
        assert "stale\\_price" in text and "17,440.00" in text
    elif case == "currency":
        assert "currency\\_conflict" in text
        assert "| Total equity | Unavailable | USD |" in text
    elif case == "invalid_reference":
        assert "invalid\\_reference" in text
        assert "Date not supplied; see section cutoff (reference date context: 2026-01-05)" in text
    else:
        assert "| Exact endpoint cutoff | 2026-01-10 |" in text
        assert "| Selected end date | 2026-01-09 |" in text
        assert "Date not supplied; see section cutoff (holdings cutoff context: 2026-01-10)" in text
        assert "current allocation cutoff context: 2026-01-10" in text
        assert "Not date-specific" not in text


@pytest.mark.parametrize("format", ["json", "markdown"])
@pytest.mark.parametrize("failure", ["oversell", "corporate_action", "unexpected"])
def test_errors_and_cleanup_are_preserved(api, golden_spy_db, monkeypatch, format, failure):
    connection, _ = golden_spy_db
    opened = []
    connect = main.get_connection

    def track(*args, **kwargs):
        result = connect(*args, **kwargs)
        opened.append(result)
        return result

    monkeypatch.setattr(main, "get_connection", track)
    if failure == "oversell":
        connection.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    elif failure == "corporate_action":
        connection.execute("UPDATE prices SET source='tiingo'")
        connection.execute("DELETE FROM corporate_action_coverage")
    else:
        def fail(*args, **kwargs):
            raise RuntimeError("unexpected export composition error")
        monkeypatch.setattr(main, "get_report_response", fail)
    if failure == "unexpected":
        with pytest.raises(RuntimeError, match="unexpected export composition error"):
            api(format)
    else:
        status, headers, body = api(format)
        assert status == 409 and headers["content-type"] == "application/json"
        assert json.loads(body)["detail"]
        assert "content-disposition" not in headers
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        _ = opened[0].in_transaction


def test_filenames_are_safe_deterministic_and_missing_dates_explicit(report):
    data = report.model_dump(mode="json")
    data["name"] = '../../bad\r\nInjected: header"é'
    data["endpoint"]["valuation_date"] = None
    changed = ReportResponse.model_validate(data)
    assert report_filename(changed, "markdown") == "portfolio-1-report-auto-to-auto-asof-undated.md"
    assert report_filename(changed, "json") == "portfolio-1-report-auto-to-auto-asof-undated.json"


def test_changed_database_does_not_claim_preview_snapshot_identity(api, golden_spy_db):
    connection, pid = golden_spy_db
    connection.commit()
    _, preview = preview_get(pid)
    connection.execute("UPDATE cash_flows SET amount=amount+100")
    status, _, body = api()
    assert status == 200
    exported = json.loads(body)
    assert exported["period"] == preview["period"]
    assert exported["endpoint"]["valuation_date"] == preview["endpoint"]["valuation_date"]
    assert exported["endpoint"]["cash_balance"]["value"] == preview["endpoint"]["cash_balance"]["value"] + 100
    assert exported != preview
    assert exported["provenance"]["persisted_snapshot_id"] is None


@pytest.mark.parametrize("explicit_dates", [False, True])
def test_empty_report_exports_keep_unavailable_and_genuine_zeros(api, golden_spy_db, explicit_dates):
    connection, _ = golden_spy_db
    pid = connection.execute("INSERT INTO portfolios(name,currency) VALUES ('Empty export','USD')").lastrowid
    connection.commit()
    query = {"start_date": "2026-01-05", "end_date": "2026-01-09"} if explicit_dates else {}
    status, _, body = request_export(pid, format="json", benchmark="ABSENT", **query)
    assert status == 200
    document = json.loads(body)
    status, _, body = request_export(pid, format="markdown", benchmark="ABSENT", **query)
    assert status == 200
    text = body.decode()
    if explicit_dates:
        assert document["status"] == "partial"
        assert document["endpoint"]["total_equity"] == {"value": 0.0, "unavailable_reasons": []}
        assert "| Total equity | 0.00 | USD |" in text
        assert "| Historical allocation reference | Unavailable |" in text
    else:
        assert document["status"] == "unavailable"
        assert document["endpoint"]["total_equity"]["value"] is None
        assert document["endpoint"]["total_equity"]["unavailable_reasons"]
        assert "| Total equity | Unavailable | USD |" in text
        assert "| Exact endpoint cutoff | Unavailable |" in text
        assert "## Holdings\n\nUnavailable; see diagnostics." in text


def test_full_history_json_is_complete_and_markdown_summarizes(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", str(path))
    status, _, body = request_export(format="json", start_date="2024-01-02", end_date="2026-11-06")
    assert status == 200
    report = ReportResponse.model_validate_json(body)
    assert len(report.performance.valuations) == len(report.risk.valuations) == 716
    assert len(report.performance.portfolio_intervals) == len(report.risk.benchmark_intervals) == 715
    assert json.loads(body) == report.model_dump(mode="json")
    text = report_markdown(report)
    assert "151,597.57" in text and "150,870.22" in text and "727.35" in text
    assert "716" in text and "715" in text
    assert "2025-06-03" not in text  # no rows for interior observations
    summary = text.split("## Time-series summary\n", 1)[1].split("\n## ", 1)[0]
    assert len([line for line in summary.splitlines() if line.startswith("| ")]) == 12  # headings + ten series
    assert "valuations (USD)" in summary
    assert "portfolio\\_intervals (fraction)" in summary
    assert "wealth\\_index.portfolio (wealth\\_index)" in summary
