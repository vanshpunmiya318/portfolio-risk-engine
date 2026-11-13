"""Fixed NYSE expectations from exchange publications, not market observations.

Compatibility at integration: all declared project dependencies resolved for
Python 3.11/Windows, and adapter syntax was checked for Python 3.11. The runtime
suite ran on Python 3.14.6; Python 3.11 was not runtime-tested locally because
that interpreter was unavailable. The declared Python >=3.11 floor is unchanged.

Sources: NYSE 2024/2025/2026 holiday and early-closing announcement (2023-11-10),
and NYSE's Carter National Day of Mourning announcement (2024-12-30).
https://s2.q4cdn.com/154085107/files/doc_news/NYSE-Group-Announces-2024-2025-and-2026-Holiday-and-Early-Closings-Calendar-2023.pdf
https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx
"""
import ast
from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import socket
import sqlite3

import exchange_calendars
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from riskengine.db.connection import get_connection
from riskengine.risk.analysis_service import prepare_analysis
from riskengine.risk.return_intervals import (
    ReturnObservation, VerifiedSessionCalendar, calculate_interval_performance,
)
from riskengine.risk.trading_calendar import verified_nyse_calendar


def within(calendar, start, end):
    return tuple(day for day in calendar.session_dates if start <= day <= end)


@pytest.mark.parametrize("year,end,count", [(2024, "2024-12-31", 252), (2025, "2025-12-31", 250), (2026, "2026-11-30", 229)])
def test_independent_historical_session_counts(year, end, count):
    start = date(year, 1, 1)
    end = date.fromisoformat(end)
    calendar = verified_nyse_calendar(start, end)
    assert len(within(calendar, start, end)) == count
    assert all(type(day) is date for day in calendar.session_dates)


@pytest.mark.parametrize("closed", [
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
])
def test_nyse_holidays_and_carter_exception_are_not_sessions(closed):
    day = date.fromisoformat(closed)
    calendar = verified_nyse_calendar(day, day)
    assert day not in calendar.session_dates
    assert calendar.session_dates[0] < day < calendar.session_dates[-1]


@pytest.mark.parametrize("early", ["2024-07-03", "2024-11-29", "2024-12-24", "2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24"])
def test_early_close_is_still_one_session(early):
    day = date.fromisoformat(early)
    calendar = verified_nyse_calendar(day, day)
    assert calendar.session_dates == (day,)
    provider = exchange_calendars.get_calendar("XNYS", start=f"{day.year - 1}-01-01", end=f"{day.year + 1}-12-31")
    assert provider.session_close(early).tz_convert("America/New_York").hour == 13


def test_weekend_padding_uses_actual_surrounding_sessions():
    calendar = verified_nyse_calendar("2026-01-17", "2026-01-19")
    # Weekend followed by MLK holiday; do not invent Monday or shift labels.
    assert calendar.session_dates == (date(2026, 1, 16), date(2026, 1, 20))
    assert calendar.count(date(2026, 1, 16), date(2026, 1, 20)) == 1


@pytest.mark.parametrize("start,end,old_hour,new_hour", [
    ("2024-03-08", "2024-03-11", 21, 20),
    ("2024-11-01", "2024-11-04", 20, 21),
])
def test_dst_changes_instants_but_not_session_dates(start, end, old_hour, new_hour):
    calendar = verified_nyse_calendar(start, end)
    assert calendar.session_dates == (date.fromisoformat(start), date.fromisoformat(end))
    assert calendar.count(date.fromisoformat(start), date.fromisoformat(end)) == 1
    provider = exchange_calendars.get_calendar("XNYS", start="2023-01-01", end="2025-12-31")
    assert provider.session_close(start).hour == old_hour
    assert provider.session_close(end).hour == new_hour


def test_multi_session_gap_is_not_one_day_or_one_session():
    calendar = verified_nyse_calendar("2026-01-05", "2026-01-08")
    assert calendar.count(date(2026, 1, 5), date(2026, 1, 7)) == 2
    observation = ReturnObservation(date(2026, 1, 5), date(2026, 1, 7), .02,
                                    calendar.count(date(2026, 1, 5), date(2026, 1, 7)))
    result = calculate_interval_performance((observation,), minimum_observations=1)
    assert result.status == "insufficient_coverage"
    assert "not_one_session" in {reason.code for reason in result.reasons}
    assert calendar.count(date(2026, 1, 4), date(2026, 1, 7)) is None


def test_provenance_checksum_and_call_order_stability():
    first = verified_nyse_calendar("2025-01-08", "2025-01-13")
    # Jan 9 was a full closure; Jan 11/12 were a weekend.
    expected = (date(2025, 1, 8), date(2025, 1, 10), date(2025, 1, 13))
    assert first.session_dates == expected
    provenance = json.loads(first.source)
    assert provenance["provider"] == "exchange_calendars"
    assert provenance["version"] == "4.13.2"
    assert provenance["calendar"] == "XNYS"
    assert provenance["coverage_start"] == "2025-01-08"
    assert provenance["coverage_end"] == "2025-01-13"
    assert provenance["session_count"] == 3
    assert provenance["session_checksum"] == "sha256:" + sha256(b"2025-01-08\n2025-01-10\n2025-01-13\n").hexdigest()
    verified_nyse_calendar("2024-01-01", "2026-11-30")
    assert first == verified_nyse_calendar("2025-01-08", "2025-01-13")


def test_constructor_bounds_are_explicit_and_padded(monkeypatch):
    from riskengine.risk import trading_calendar
    original = trading_calendar.exchange_calendars.get_calendar
    calls = []

    def capture(name, **kwargs):
        calls.append((name, kwargs))
        return original(name, **kwargs)

    monkeypatch.setattr(trading_calendar.exchange_calendars, "get_calendar", capture)
    verified_nyse_calendar("2024-01-01", "2026-11-30")
    assert calls == [("XNYS", {"start": "2023-01-01", "end": "2027-12-31"})]


def test_provider_version_mismatch_fails_closed(monkeypatch):
    from riskengine.risk import trading_calendar
    monkeypatch.setattr(trading_calendar, "version", lambda name: "0.0")
    with pytest.raises(RuntimeError, match="Expected exchange_calendars==4.13.2"):
        verified_nyse_calendar("2024-01-02", "2024-01-03")


def test_offline_adapter_performs_no_network_or_database_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Calendar generation must not use network or databases")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert verified_nyse_calendar("2025-01-08", "2025-01-10").session_dates == (date(2025, 1, 8), date(2025, 1, 10))


@pytest.mark.parametrize("start,end", [(None, "2024-01-02"), ("2024-02-30", "2024-03-01"), ("20240102", "2024-01-03"), ("2024-01-03", "2024-01-02"), (datetime(2024, 1, 2), "2024-01-03"), (date(1, 1, 1), date(1, 1, 2)), (date(9999, 1, 1), date(9999, 1, 2))])
def test_invalid_boundaries_fail_explicitly(start, end):
    with pytest.raises(ValueError):
        verified_nyse_calendar(start, end)


def test_existing_preparation_interface_accepts_real_calendar(golden_spy_db):
    c, pid = golden_spy_db
    calendar = verified_nyse_calendar("2026-01-04", "2026-01-10")
    assert isinstance(calendar, VerifiedSessionCalendar)
    if not c.in_transaction:
        c.execute("BEGIN")
    result = prepare_analysis(c, pid, start_date="2026-01-04", end_date="2026-01-10", session_calendar=calendar)
    assert result.portfolio_coverage.calendar_verified
    assert result.portfolio_coverage.expected_sessions == 5
    assert result.period.selected_start_date == date(2026, 1, 5)
    assert result.period.selected_end_date == date(2026, 1, 9)
    assert all(item.trading_sessions == 1 for item in result.portfolio_intervals)
    assert result.comparison_eligible
    assert c.in_transaction


def test_preparation_weekend_only_window_has_no_invented_sessions(golden_spy_db):
    c, pid = golden_spy_db
    calendar = verified_nyse_calendar("2026-01-17", "2026-01-19")
    if not c.in_transaction:
        c.execute("BEGIN")
    result = prepare_analysis(c, pid, start_date="2026-01-17", end_date="2026-01-19", session_calendar=calendar)
    assert result.portfolio_coverage.calendar_verified
    assert result.portfolio_coverage.expected_sessions == 0
    assert not result.portfolio_intervals
    assert any(issue.code == "no_sessions" for issue in result.issues)


def test_historical_six_etf_session_coverage_read_only():
    path = Path(__file__).resolve().parents[1] / "data" / "historical_portfolio.db"
    if not path.is_file():
        pytest.skip("Historical database is not present")
    calendar = verified_nyse_calendar("2024-01-02", "2026-11-06")
    expected = {day.isoformat() for day in calendar.session_dates}
    assert len(expected) == 716
    with closing(get_connection(path, read_only=True)) as c:
        c.execute("BEGIN")
        tickers = {row[0] for row in c.execute("SELECT DISTINCT i.ticker FROM prices p JOIN instruments i ON i.id=p.instrument_id")}
        assert tickers == {"GLD", "IWM", "QQQ", "SPY", "TLT", "XLE"}
        for ticker in sorted(tickers):
            actual = {row[0] for row in c.execute("SELECT p.date FROM prices p JOIN instruments i ON i.id=p.instrument_id WHERE i.ticker=?", (ticker,))}
            assert expected - actual == set(), f"Missing sessions: {ticker}"
            assert actual - expected == set(), f"Unexpected dates: {ticker}"


def test_adapter_source_supports_python_311_syntax():
    from riskengine.risk import trading_calendar
    ast.parse(Path(trading_calendar.__file__).read_text(encoding="utf-8"), feature_version=(3, 11))
