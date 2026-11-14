from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from test_golden_portfolio import golden_db, cash_flow_golden_db
from test_benchmark import golden_spy_db
from test_analysis_service import CALENDAR
from riskengine.db import portfolios, transactions
from riskengine.risk import analysis_service
from riskengine.risk.valuation_context import PreparedValuationContext, ValuationContextError


def reader(connection):
    connection.commit()
    path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    result = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    result.execute("BEGIN")
    return closing(result)


@pytest.mark.parametrize("scenario", [
    "complete", "deposit", "withdrawal", "empty", "funded_empty", "missing", "all_missing",
    "nonfinite", "currency", "flow_currency", "instrument_currency", "coverage", "source",
    "future_trade", "invalid_trade", "same_day_split_trade", "same_day_sale", "fractional_splits",
    "duplicate_price", "divergent_duplicates", "backdated_cash", "split_before_first_trade",
    "zero_equity", "negative_equity", "missing_benchmark", "noncanonical_price", "future_split",
    "unknown_calendar", "historical_start", "nonfinite_cash",
])
def test_full_preparation_matches_legacy_readers(golden_spy_db, monkeypatch, scenario):
    c, pid = golden_spy_db
    if scenario in {"deposit", "withdrawal", "flow_currency"}:
        c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,'2026-01-07',?,5000,?,'test')", (pid, "WITHDRAWAL" if scenario == "withdrawal" else "DEPOSIT", "EUR" if scenario == "flow_currency" else "USD"))
    elif scenario == "empty":
        c.execute("DELETE FROM transactions")
        c.execute("DELETE FROM cash_flows")
        c.execute("DELETE FROM prices")
    elif scenario == "funded_empty":
        c.execute("DELETE FROM transactions")
    elif scenario == "missing":
        c.execute("DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    elif scenario == "all_missing":
        c.execute("DELETE FROM prices")
    elif scenario == "nonfinite":
        c.execute("UPDATE prices SET adjusted_close=? WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (float("inf"),))
    elif scenario == "nonfinite_cash":
        c.execute("UPDATE cash_flows SET amount=?", (float("inf"),))
    elif scenario == "currency":
        c.execute("UPDATE transactions SET currency='EUR' WHERE date='2026-01-07'")
    elif scenario == "instrument_currency":
        c.execute("UPDATE instruments SET currency='EUR' WHERE ticker='AAPL'")
    elif scenario == "coverage":
        c.execute("UPDATE prices SET source='tiingo'")
        c.execute("DELETE FROM corporate_action_coverage")
    elif scenario == "source":
        c.execute("UPDATE prices SET adjusted_close=close*.9")
    elif scenario in {"future_trade", "invalid_trade"}:
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),?,'SELL',999,100,'USD','test')", (pid, "2026-01-12" if scenario == "future_trade" else "2026-01-07"))
    elif scenario in {"same_day_split_trade", "same_day_sale"}:
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='NVDA'),'2026-01-08',?,?,55,'USD','test')", (pid, "BUY" if scenario == "same_day_split_trade" else "SELL", 2 if scenario == "same_day_split_trade" else 20))
    elif scenario in {"fractional_splits", "split_before_first_trade", "future_split"}:
        days = ["2026-01-06", "2026-01-07"] if scenario == "fractional_splits" else ["2026-01-02" if scenario == "split_before_first_trade" else "2026-01-12"]
        for day, ratio in zip(days, [1.1, 1.3]):
            c.execute("INSERT INTO corporate_actions(instrument_id,ex_date,action_type,ratio,source,fetched_at) VALUES ((SELECT id FROM instruments WHERE ticker='NVDA'),?,'SPLIT',?,'test','test')", (day, ratio))
    elif scenario in {"duplicate_price", "divergent_duplicates"}:
        # Simulate a legacy/corrupt database without its normal UNIQUE constraint.
        c.execute("CREATE TEMP TABLE copy_prices AS SELECT * FROM prices")
        c.execute("DROP TABLE prices")
        c.execute("CREATE TABLE prices AS SELECT * FROM copy_prices")
        c.execute("INSERT INTO prices SELECT * FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
        if scenario == "divergent_duplicates":
            c.execute("UPDATE prices SET adjusted_close=adjusted_close+1 WHERE rowid=(SELECT MAX(rowid) FROM prices)")
    elif scenario == "backdated_cash":
        for day, amount, kind in [("2026-01-08", 1e16, "DEPOSIT"), ("2026-01-06", .1, "DEPOSIT"), ("2026-01-09", 1e16, "WITHDRAWAL")]:
            c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,?,?,?,'USD','test')", (pid, day, kind, amount))
    elif scenario in {"zero_equity", "negative_equity"}:
        c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,'2026-01-05','WITHDRAWAL',?,'USD','test')", (pid, 20000 if scenario == "zero_equity" else 21000))
    elif scenario == "missing_benchmark":
        c.execute("DELETE FROM prices WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='SPY')")
    elif scenario == "noncanonical_price":
        c.execute("UPDATE prices SET date='20260107' WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    kwargs = dict(start_date="2026-01-07" if scenario == "historical_start" else "2026-01-05", end_date="2026-01-09", session_calendar=None if scenario == "unknown_calendar" else CALENDAR)
    with reader(c) as r:
        actual = analysis_service.prepare_analysis(r, pid, **kwargs)
        # Force only the pre-context reader path; formulas/series stay shared.
        monkeypatch.setattr(analysis_service, "_prepare_valuations", lambda *args: analysis_service._valuation_rows(*args, None))
        expected = analysis_service.prepare_analysis(r, pid, **kwargs)
        assert actual == expected
        assert r.in_transaction


def test_cash_flow_fixture_preserves_adjustment(cash_flow_golden_db, monkeypatch):
    c, pid = cash_flow_golden_db
    with reader(c) as r:
        kwargs = dict(start_date="2026-01-05", end_date="2026-01-08", session_calendar=CALENDAR)
        actual = analysis_service.prepare_analysis(r, pid, **kwargs)
        monkeypatch.setattr(analysis_service, "_prepare_valuations", lambda *args: analysis_service._valuation_rows(*args, None))
        assert actual == analysis_service.prepare_analysis(r, pid, **kwargs)
        assert [v.return_value for v in actual.portfolio_intervals] == pytest.approx([.05, 0, .02])


@pytest.mark.parametrize("day,quantity", [("2025-12-04", 118.), ("2025-12-05", 236.), ("2025-12-08", 236.)])
def test_historical_xle_split(day, quantity):
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    if not path.exists():
        pytest.skip("Historical database is unavailable")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        with PreparedValuationContext(c, 1, [day]) as context:
            frame = context.value(c, 1, day)
            pd.testing.assert_frame_equal(frame, portfolios.get_portfolio_value(c, 1, day, max_age_days=0), check_exact=True)
            assert frame.set_index("ticker").loc["XLE", "quantity"] == pytest.approx(quantity)


def test_context_scope_and_event_reuse(golden_spy_db):
    c, pid = golden_spy_db
    boundaries = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    with reader(c) as r:
        with PreparedValuationContext(r, pid, boundaries) as context:
            for day in boundaries:
                frame = context.value(r, pid, day)
                pd.testing.assert_frame_equal(frame, portfolios.get_portfolio_value(r, pid, day, max_age_days=0), check_exact=True)
            assert len(context._positions) == len(context._validation) == 3
            for wrong_pid, day in [(pid + 1, boundaries[0]), (pid, "2026-01-12"), (pid, "2026-01-04")]:
                with pytest.raises(ValuationContextError):
                    context.positions(r, wrong_pid, day)
            with pytest.raises(ValuationContextError):
                context.positions(c, pid, boundaries[0])
            copied = context.positions(r, pid, boundaries[0])
            copied.loc[:, "quantity"] = 999
            assert context.positions(r, pid, boundaries[0]).quantity.max() != 999
        assert r.in_transaction
        with pytest.raises(ValuationContextError):
            context.positions(r, pid, boundaries[0])
        with pytest.raises(ValuationContextError):
            context.__enter__()


def test_ended_transaction_and_writes_invalidate_context(golden_spy_db):
    c, pid = golden_spy_db
    with pytest.raises(ValuationContextError):
        PreparedValuationContext(c, pid, ["2026-01-05"])
    with reader(c) as r:
        context = PreparedValuationContext(r, pid, ["2026-01-05"])
        context.__enter__()
        r.commit()
        try:
            with pytest.raises(ValuationContextError):
                context.positions(r, pid, "2026-01-05")
        finally:
            context.__exit__(RuntimeError, RuntimeError(), None)


def test_changed_snapshot_cannot_reuse_context(golden_spy_db):
    c, pid = golden_spy_db
    with reader(c) as r:
        context = PreparedValuationContext(r, pid, ["2026-01-05"])
        context.__enter__()
        r.commit()
        c.execute("UPDATE transactions SET quantity=quantity+1 WHERE transaction_type='BUY'")
        c.commit()
        r.execute("BEGIN")
        try:
            with pytest.raises(ValuationContextError):
                context.value(r, pid, "2026-01-05")
        finally:
            context.__exit__(RuntimeError, RuntimeError(), None)


def test_sqlite_cash_aggregation_and_cost_basis_are_unchanged(golden_spy_db):
    c, pid = golden_spy_db
    for kind, amount in [("DEPOSIT", 1e16), ("DEPOSIT", .1), ("WITHDRAWAL", 1e16)]:
        c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,'2026-01-09',?,?,'USD','test')", (pid, kind, amount))
    with reader(c) as r:
        before = transactions.get_cost_basis(r, pid, "2026-01-09")
        with PreparedValuationContext(r, pid, ["2026-01-09"]) as context:
            external, cash = context.cash(r, pid, "2026-01-09")
            assert external == transactions.get_net_cash_flows(r, pid, "2026-01-09") == 20000.1
            assert cash == transactions.get_cash_balance(r, pid, "2026-01-09")
        pd.testing.assert_frame_equal(before, transactions.get_cost_basis(r, pid, "2026-01-09"), check_exact=True)


def test_fractional_split_positions_are_bit_exact(golden_spy_db):
    c, pid = golden_spy_db
    for day, ratio in [("2026-01-06", 1.1), ("2026-01-07", 1.3)]:
        c.execute("INSERT INTO corporate_actions(instrument_id,ex_date,action_type,ratio,source,fetched_at) VALUES ((SELECT id FROM instruments WHERE ticker='NVDA'),?,'SPLIT',?,'test','test')", (day, ratio))
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    with reader(c) as r:
        with PreparedValuationContext(r, pid, days) as context:
            for day in days:
                pd.testing.assert_frame_equal(context.positions(r, pid, day), transactions.get_positions(r, pid, day), check_exact=True)


@pytest.mark.parametrize("missing_all", [False, True])
def test_direct_missing_price_diagnostics(golden_spy_db, missing_all):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')" if missing_all else "DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    with reader(c) as r:
        with PreparedValuationContext(r, pid, ["2026-01-07"]) as context:
            with pytest.raises(ValueError) as expected:
                portfolios.get_portfolio_value(r, pid, "2026-01-07", max_age_days=0)
            with pytest.raises(ValueError) as actual:
                context.value(r, pid, "2026-01-07")
            assert str(actual.value) == str(expected.value)


def test_context_failure_does_not_close_or_end_transaction(golden_spy_db):
    c, pid = golden_spy_db
    with reader(c) as r:
        with pytest.raises(LookupError):
            with PreparedValuationContext(r, pid, ["2026-01-05"]) as context:
                raise LookupError("caller failure")
        assert r.in_transaction and r.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(ValuationContextError):
            context.positions(r, pid, "2026-01-05")


def test_writes_during_scope_are_rejected(golden_spy_db):
    c, pid = golden_spy_db
    c.commit()
    path = c.execute("PRAGMA database_list").fetchone()[2]
    with closing(sqlite3.connect(path)) as fresh:
        fresh.execute("BEGIN")
        context = PreparedValuationContext(fresh, pid, ["2026-01-05"])
        context.__enter__()
        fresh.execute("UPDATE transactions SET quantity=quantity+1 WHERE transaction_type='BUY'")
        try:
            with pytest.raises(ValuationContextError):
                context.cash(fresh, pid, "2026-01-05")
        finally:
            context.__exit__(RuntimeError, RuntimeError(), None)
            fresh.rollback()


def test_full_historical_valuation_and_return_parity(monkeypatch):
    from riskengine.risk.trading_calendar import verified_nyse_calendar

    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    if not path.exists():
        pytest.skip("Historical database is unavailable")
    calendar = verified_nyse_calendar(date(2024, 1, 2), date(2026, 11, 6))
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        kwargs = dict(start_date="2024-01-02", end_date="2026-11-06", session_calendar=calendar)
        actual = analysis_service.prepare_analysis(c, 1, **kwargs)
        monkeypatch.setattr(analysis_service, "_prepare_valuations", lambda *args: analysis_service._valuation_rows(*args, None))
        expected = analysis_service.prepare_analysis(c, 1, **kwargs)
        assert actual == expected
        assert len(actual.valuations) == 716 and len(actual.portfolio_intervals) == 715
