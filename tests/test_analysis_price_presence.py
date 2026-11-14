"""Presence-only batching must retain existing valuation and diagnostic behavior."""
import sqlite3

import pytest

from test_golden_portfolio import golden_db, EXPECTED_EQUITY
from test_benchmark import golden_spy_db
from test_analysis_service import CALENDAR, prepare
from riskengine.risk import analysis_service


def missing_prices(result):
    return {(item.ticker, item.start_date.isoformat()) for item in result.issues if item.code == "missing_price"}


def test_missing_prices_are_matched_by_instrument_and_exact_date(golden_spy_db):
    c, pid = golden_spy_db
    for ticker, day in (("AAPL", "2026-01-07"), ("NVDA", "2026-01-08")):
        c.execute("DELETE FROM prices WHERE date=? AND instrument_id=(SELECT id FROM instruments WHERE ticker=?)", (day, ticker))
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert missing_prices(result) == {("AAPL", "2026-01-07"), ("NVDA", "2026-01-08")}
    assert not result.portfolio_coverage.complete
    assert [v.complete for v in result.valuations] == [True, True, False, False, True]
    assert not any(item.category == "integrity" for item in result.issues)


@pytest.mark.parametrize("day", ["2026-01-06", "2026-01-07", "2026-01-08"])
def test_inclusive_window_never_substitutes_prices_outside_boundaries(golden_spy_db, day):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices WHERE date=? AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (day,))
    result = prepare(c, pid, start_date="2026-01-06", end_date="2026-01-08", session_calendar=CALENDAR)
    assert missing_prices(result) == {("AAPL", day)}
    assert result.period.selected_start_date.isoformat() == "2026-01-06"
    assert result.period.selected_end_date.isoformat() == "2026-01-08"
    if day == "2026-01-06":
        assert result.period.effective_baseline_date is None
    if day == "2026-01-08":
        assert result.period.effective_end_date is None


def test_duplicate_price_constraint_is_not_relaxed(golden_spy_db):
    c, pid = golden_spy_db
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        c.execute(
            "INSERT INTO prices(instrument_id,date,open,high,low,close,adjusted_close,volume,source,fetched_at) "
            "SELECT instrument_id,date,open,high,low,close,adjusted_close,volume,source,fetched_at FROM prices "
            "WHERE date='2026-01-05' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')"
        )
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert [v.total_equity for v in result.valuations] == list(EXPECTED_EQUITY.values())
    assert not missing_prices(result)


@pytest.mark.parametrize("assignment", ["close=0", "adjusted_close=-1", "adjusted_close=NULL"])
def test_invalid_price_constraints_are_not_relaxed(golden_spy_db, assignment):
    c, pid = golden_spy_db
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(f"UPDATE prices SET {assignment} WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert result.portfolio_coverage.complete
    assert not missing_prices(result)


def test_nonfinite_existing_price_remains_invalid_valuation_not_missing_price(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("UPDATE prices SET adjusted_close=? WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (float("inf"),))
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert not missing_prices(result)
    assert result.valuations[2].total_equity is None
    assert any(item.code == "invalid_valuation" and item.start_date.isoformat() == "2026-01-07" for item in result.issues)
    assert not any(item.category == "integrity" for item in result.issues)


def test_noncanonical_stored_date_is_not_normalized_into_exact_price(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("UPDATE prices SET date='20260107' WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    result = prepare(c, pid, start_date="2026-01-06", end_date="2026-01-08", session_calendar=CALENDAR)
    assert missing_prices(result) == {("AAPL", "2026-01-07")}
    assert not result.portfolio_coverage.complete


def test_all_missing_prices_leave_complete_expected_date_grid(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices")
    result = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR)
    assert len(result.valuations) == 5
    assert len(missing_prices(result)) == 15
    assert all(v.total_equity is None for v in result.valuations)
    assert not result.portfolio_intervals
    assert result.period.effective_baseline_date is None


def test_future_instrument_and_prices_do_not_change_historical_cutoff(golden_spy_db):
    c, pid = golden_spy_db
    instrument = c.execute("INSERT INTO instruments(ticker,name,asset_type,currency,primary_source) VALUES ('FUT','Future','stock','EUR','test')").lastrowid
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,?,'2026-01-12','BUY',1,10,'EUR','test')", (pid, instrument))
    c.execute("INSERT INTO prices(instrument_id,date,open,high,low,close,adjusted_close,source,fetched_at) VALUES (?,'2030-01-02',10,10,10,10,10,'test','2030-01-02')", (instrument,))
    result = prepare(c, pid, end_date="2026-01-09", session_calendar=CALENDAR)
    assert result.portfolio_coverage.complete
    assert [v.total_equity for v in result.valuations] == list(EXPECTED_EQUITY.values())
    assert not any(item.code == "currency_conflict" for item in result.issues)


def test_presence_is_refreshed_for_each_caller_transaction(golden_spy_db):
    c, pid = golden_spy_db
    assert prepare(c, pid, session_calendar=CALENDAR).portfolio_coverage.complete
    c.execute("DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    c.commit()
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert missing_prices(result) == {("AAPL", "2026-01-07")}
    assert c.in_transaction  # Preparation still leaves lifecycle to its caller.


def test_batch_presence_never_uses_full_dataframe_reads_for_holdings(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    original = analysis_service.get_prices

    def benchmark_only(connection, ticker, *args, **kwargs):
        assert ticker == "SPY", "Holding existence checks must not construct price DataFrames"
        return original(connection, ticker, *args, **kwargs)

    statements = []
    monkeypatch.setattr(analysis_service, "get_prices", benchmark_only)
    c.set_trace_callback(statements.append)
    try:
        result = prepare(c, pid, session_calendar=CALENDAR)
    finally:
        c.set_trace_callback(None)
    assert result.portfolio_coverage.complete
    batch = [sql for sql in statements if "SELECT DISTINCT i.ticker, p.date" in sql]
    assert len(batch) == 1
    assert not any(sql.lstrip().upper().startswith(("COMMIT", "ROLLBACK", "INSERT", "UPDATE", "DELETE")) for sql in statements)
