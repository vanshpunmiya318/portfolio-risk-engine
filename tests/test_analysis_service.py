from datetime import date
import sqlite3

import pandas as pd
import pytest

from test_golden_portfolio import golden_db, cash_flow_golden_db, EXPECTED_EQUITY
from test_benchmark import golden_spy_db
from riskengine.db.portfolios import get_total_portfolio_equity
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns
from riskengine.risk.analysis_service import (
    prepare_analysis, AnalysisInputError, AnalysisPortfolioNotFound,
)
from riskengine.risk.return_intervals import VerifiedSessionCalendar


CALENDAR = VerifiedSessionCalendar(tuple(date(2026, 1, day) for day in range(5, 10)), "Explicit test sessions")


def prepare(connection, pid, **kwargs):
    if not connection.in_transaction:
        connection.execute("BEGIN")
    return prepare_analysis(connection, pid, **kwargs)


def codes(result, scope=None):
    return {item.code for item in result.issues if scope is None or item.scope == scope}


def test_complete_valuation_and_return_parity(golden_spy_db):
    c, pid = golden_spy_db
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert [v.total_equity for v in result.valuations] == list(EXPECTED_EQUITY.values())
    dates = pd.to_datetime(list(EXPECTED_EQUITY))
    old = get_portfolio_daily_returns(c, pid, dates)
    assert [r.return_value for r in result.portfolio_intervals] == old.tolist()
    assert result.portfolio_coverage.complete is True
    assert result.portfolio_coverage.expected_sessions == 5
    assert result.portfolio_coverage.expected_intervals == 4
    assert result.comparison_eligible
    assert result.period.effective_baseline_date == date(2026, 1, 5)
    assert result.period.first_return_end_date == date(2026, 1, 6)
    assert result.period.effective_end_date == date(2026, 1, 9)
    assert result.methodology.price_age_days == 0


def test_unknown_calendar_does_not_certify_spy_dates(golden_spy_db):
    c, pid = golden_spy_db
    result = prepare(c, pid)
    assert result.portfolio_coverage.complete is None
    assert result.portfolio_coverage.expected_sessions is None
    assert not result.comparison_eligible
    assert "unknown_calendar" in codes(result)
    assert all(r.trading_sessions is None for r in result.portfolio_intervals)


def test_missing_benchmark_retains_portfolio_and_long_interval(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices WHERE date='2026-01-06' AND instrument_id=(SELECT id FROM instruments WHERE ticker='SPY')")
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert result.portfolio_coverage.complete
    assert len(result.portfolio_intervals) == 4
    assert result.benchmark_coverage.missing_sessions == (date(2026, 1, 6),)
    assert result.benchmark_intervals[0].trading_sessions == 2
    assert {"missing_session", "not_one_session"} <= codes(result, "benchmark")
    assert "unmatched_interval" in codes(result, "comparison")
    assert not result.comparison_eligible


def test_unavailable_benchmark_does_not_change_portfolio_period(golden_db):
    c, _, pid = golden_db
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert result.portfolio_coverage.daily_eligible
    assert not result.benchmark_intervals
    assert "benchmark_unavailable" in codes(result)
    assert result.period.selected_end_date == date(2026, 1, 9)


@pytest.mark.parametrize("missing", ["2026-01-05", "2026-01-07", "2026-01-09"])
def test_missing_expected_prices_never_shift_boundaries_or_bridge_gaps(golden_spy_db, missing):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices WHERE date=? AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (missing,))
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert result.period.selected_start_date == date(2026, 1, 5)
    assert result.period.selected_end_date == date(2026, 1, 9)
    assert not result.portfolio_coverage.complete
    assert date.fromisoformat(missing) in result.portfolio_coverage.missing_sessions
    assert all(r.start_date != date.fromisoformat(missing) and r.end_date != date.fromisoformat(missing) for r in result.portfolio_intervals)
    assert all(r.trading_sessions == 1 for r in result.portfolio_intervals)
    if missing.endswith("05"):
        assert result.period.effective_baseline_date is None
    if missing.endswith("09"):
        assert result.period.effective_end_date is None
    assert not any(i.category == "integrity" for i in result.issues)


def test_cash_flow_adjusted_returns_not_raw_equity(cash_flow_golden_db):
    c, pid = cash_flow_golden_db
    result = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-08", session_calendar=CALENDAR)
    assert [r.return_value for r in result.portfolio_intervals] == pytest.approx([.05, 0, .02])
    assert result.valuations[-1].total_equity == pytest.approx(16065)


@pytest.mark.parametrize("table", ["transactions", "instruments", "cash_flows"])
def test_currency_conflict_suppresses_affected_values(golden_spy_db, table):
    c, pid = golden_spy_db
    if table == "instruments":
        c.execute("UPDATE instruments SET currency='EUR' WHERE ticker='AAPL'")
    else:
        c.execute(f"UPDATE {table} SET currency='EUR' WHERE portfolio_id=?", (pid,))
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert not result.portfolio_intervals
    assert all(v.total_equity is None for v in result.valuations)
    assert "currency_conflict" in codes(result)
    assert result.benchmark_intervals


def test_future_invalid_sale_and_currency_do_not_affect_historical_cutoff(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("INSERT INTO transactions (portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-10','SELL',999,100,'EUR','test')", (pid,))
    result = prepare(c, pid, start_date="2026-01-07", end_date="2026-01-09", session_calendar=CALENDAR)
    assert result.valuations[0].total_equity == EXPECTED_EQUITY["2026-01-07"]
    assert result.comparison_eligible
    assert "currency_conflict" not in codes(result)
    assert "invalid_positions" not in codes(result)


def test_in_period_negative_position_is_integrity_issue(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("UPDATE transactions SET quantity=999 WHERE transaction_type='SELL'")
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert "invalid_positions" in codes(result)
    assert any(i.category == "integrity" for i in result.issues)


def test_explicit_range_preserves_earlier_positions_and_same_day_split(golden_spy_db):
    c, pid = golden_spy_db
    result = prepare(c, pid, start_date="2026-01-08", end_date="2026-01-09", session_calendar=CALENDAR)
    assert result.valuations[0].total_equity == get_total_portfolio_equity(c, pid, "2026-01-08", max_age_days=0)
    assert result.valuations[0].total_equity == 20238
    assert result.period.effective_baseline_date == date(2026, 1, 8)


def test_empty_portfolio_returns_no_fabricated_returns(golden_spy_db):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Empty','USD')").lastrowid
    result = prepare(c, pid)
    assert result.valuations == result.portfolio_intervals == ()
    assert result.period.effective_baseline_date is None
    assert "empty_portfolio" in codes(result)
    explicit = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR)
    assert not explicit.portfolio_intervals
    assert all(v.total_equity == 0 for v in explicit.valuations)


@pytest.mark.parametrize("kwargs", [
    {"portfolio_id": 0}, {"confidence": float("nan")}, {"confidence": 1},
    {"start_date": "2026-02-30"}, {"start_date": "2026-01-09", "end_date": "2026-01-05"},
    {"benchmark_ticker": " "},
])
def test_input_validation(golden_spy_db, kwargs):
    c, pid = golden_spy_db
    args = {"portfolio_id": pid, **kwargs}
    if not c.in_transaction:
        c.execute("BEGIN")
    with pytest.raises(AnalysisInputError):
        prepare_analysis(c, **args)


def test_unknown_portfolio(golden_spy_db):
    c, _ = golden_spy_db
    with pytest.raises(AnalysisPortfolioNotFound):
        prepare(c, 999)


def test_connection_and_snapshot_are_caller_owned(golden_spy_db):
    c, pid = golden_spy_db
    c.commit()
    with pytest.raises(AnalysisInputError, match="transaction"):
        prepare_analysis(c, pid)
    c.execute("BEGIN")
    statements = []
    c.set_trace_callback(statements.append)
    try:
        prepare_analysis(c, pid, session_calendar=CALENDAR)
    finally:
        c.set_trace_callback(None)
    assert c.in_transaction
    assert c.execute("SELECT 1").fetchone() == (1,)
    assert not any(s.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "COMMIT", "ROLLBACK", "BEGIN"} for s in statements)


def test_calendar_outside_verified_range_does_not_certify_coverage(golden_spy_db):
    c, pid = golden_spy_db
    result = prepare(c, pid, start_date="2026-01-04", end_date="2026-01-09", session_calendar=CALENDAR)
    assert not result.portfolio_coverage.calendar_verified
    assert "calendar_out_of_range" in codes(result)
    assert not result.comparison_eligible


def test_cash_flow_between_verified_sessions_is_not_shifted(cash_flow_golden_db):
    c, pid = cash_flow_golden_db
    calendar = VerifiedSessionCalendar(tuple(date(2026, 1, day) for day in (5, 6, 8, 9)), "Test with non-session flow date")
    result = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-08", session_calendar=calendar)
    assert date(2026, 1, 7) in [v.date for v in result.valuations]
    assert [r.return_value for r in result.portfolio_intervals] == pytest.approx([.05, 0, .02])
    assert not result.portfolio_coverage.daily_eligible
    assert "unverified_sessions" in codes(result)


def test_missing_off_session_cash_flow_valuation_is_not_bridged(cash_flow_golden_db):
    c, pid = cash_flow_golden_db
    c.execute("DELETE FROM prices WHERE date='2026-01-07'")
    calendar = VerifiedSessionCalendar(tuple(date(2026, 1, day) for day in (5, 6, 8, 9)), "Test sessions")
    result = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-08", session_calendar=calendar)
    assert not result.portfolio_coverage.complete
    assert [(r.start_date, r.end_date) for r in result.portfolio_intervals] == [(date(2026, 1, 5), date(2026, 1, 6))]


def test_weekend_range_selects_sessions_without_calendar_subtraction(golden_spy_db):
    c, pid = golden_spy_db
    calendar = VerifiedSessionCalendar((date(2026, 1, 2), *CALENDAR.session_dates, date(2026, 1, 12)), "Explicit weekend coverage")
    result = prepare(c, pid, start_date="2026-01-04", end_date="2026-01-10", session_calendar=calendar)
    assert result.period.requested_start_date == date(2026, 1, 4)
    assert result.period.selected_start_date == date(2026, 1, 5)
    assert result.period.selected_end_date == date(2026, 1, 9)
    assert result.comparison_eligible


def test_future_split_does_not_affect_earlier_cutoff(golden_spy_db):
    c, pid = golden_spy_db
    before = prepare(c, pid, end_date="2026-01-09", session_calendar=CALENDAR)
    c.execute("INSERT INTO corporate_actions(instrument_id,ex_date,action_type,ratio,source,fetched_at) VALUES ((SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-10','SPLIT',3,'test','2026-01-10')")
    after = prepare(c, pid, end_date="2026-01-09", session_calendar=CALENDAR)
    assert before.valuations == after.valuations
    assert before.portfolio_intervals == after.portfolio_intervals


def test_withdrawal_is_removed_from_investment_return(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,'2026-01-07','WITHDRAWAL',1000,'USD','test')", (pid,))
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert result.portfolio_intervals[1].return_value == pytest.approx(20205 / 20140 - 1)
    assert result.valuations[2].total_equity == 19205


def test_later_currency_conflict_does_not_contaminate_earlier_valuations(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("UPDATE transactions SET currency='EUR' WHERE date='2026-01-07'")
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert result.valuations[0].complete and result.valuations[1].complete
    assert result.valuations[2].total_equity is None
    assert len(result.portfolio_intervals) == 1


def test_all_prices_missing_does_not_shrink_verified_window(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices")
    result = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR)
    assert len(result.valuations) == 5
    assert result.period.selected_start_date == date(2026, 1, 5)
    assert result.period.selected_end_date == date(2026, 1, 9)
    assert result.period.effective_baseline_date is None
    assert result.portfolio_coverage.missing_sessions == CALENDAR.session_dates
    assert not result.portfolio_intervals
    assert not any(i.category == "integrity" for i in result.issues)


def test_foreign_benchmark_is_explicit_local_currency_not_fx(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("UPDATE instruments SET currency='EUR' WHERE ticker='SPY'")
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert result.portfolio_coverage.daily_eligible
    assert result.benchmark_currency == "EUR"
    assert result.benchmark_intervals
    assert not result.comparison_eligible
    assert "benchmark_currency_difference" in codes(result)


def test_valuation_aggregation_preserves_engine_floating_point_order(golden_spy_db, monkeypatch):
    from riskengine.risk import analysis_service
    c, pid = golden_spy_db
    frame = pd.DataFrame({"market_value": [1e16, *([1.] * 7)]})
    assert frame.market_value.sum() != sum(frame.market_value)
    monkeypatch.setattr(analysis_service, "get_portfolio_value", lambda *args, **kwargs: frame)
    result = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-05")
    assert result.valuations[0].holdings_value == frame.market_value.sum()


@pytest.mark.parametrize("invalid", [float("nan"), float("inf")])
def test_nonfinite_component_is_not_skipped_or_turned_into_zero(golden_spy_db, monkeypatch, invalid):
    from riskengine.risk import analysis_service
    c, pid = golden_spy_db
    monkeypatch.setattr(analysis_service, "get_portfolio_value", lambda *args, **kwargs: pd.DataFrame({"market_value": [100., invalid]}))
    result = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-05")
    assert result.valuations[0].holdings_value is None
    assert result.valuations[0].total_equity is None
    assert "invalid_valuation" in codes(result)


def test_invalid_return_observation_is_a_structured_outcome(golden_spy_db, monkeypatch):
    from riskengine.risk import analysis_service
    c, pid = golden_spy_db

    def invalid_observation(*args, **kwargs):
        raise ValueError("Simple returns must be finite and at least -100%")

    monkeypatch.setattr(analysis_service, "get_portfolio_return_observations", invalid_observation)
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert not result.portfolio_intervals
    assert "invalid_return" in codes(result)
    assert not result.portfolio_coverage.daily_eligible


def test_concurrent_write_cannot_mix_snapshot_values(golden_spy_db, monkeypatch):
    from riskengine.risk import analysis_service
    c, pid = golden_spy_db
    c.commit()
    path = c.execute("PRAGMA database_list").fetchone()[2]
    c.execute("PRAGMA journal_mode=WAL")
    original = analysis_service.get_portfolio_value
    changed = False

    def concurrent_write(*args, **kwargs):
        nonlocal changed
        if not changed:
            writer = sqlite3.connect(path)
            try:
                writer.execute("UPDATE prices SET close=close*2, adjusted_close=adjusted_close*2 WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
                writer.commit()
            finally:
                writer.close()
            changed = True
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis_service, "get_portfolio_value", concurrent_write)
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert changed
    assert [v.total_equity for v in result.valuations] == list(EXPECTED_EQUITY.values())
    assert c.in_transaction
    c.rollback()
    assert c.execute("SELECT close FROM prices WHERE date='2026-01-05' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')").fetchone()[0] == 200
