from contextlib import closing
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from test_analysis_service import CALENDAR
from test_valuation_context import reader
from riskengine.db.portfolios import value_positions
from riskengine.risk import analysis_service
from riskengine.risk import valuation_context
from riskengine.risk.valuation_context import PreparedValuationContext


DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


def previous_value(context, connection, pid, day):
    """The pre-batch context path, including the same snapshot checks."""
    context.validate(connection, pid, day)
    return value_positions(context.positions(connection, pid, day), day, 0, False,
                           context._latest_prices, context._validate_coverage)


@pytest.mark.parametrize("scenario", [
    "complete", "duplicate", "conflicting", "mixed_sources", "missing", "stale",
    "first_missing", "nullable", "noncanonical_ticker", "same_day_buy", "same_day_sale",
    "fractional_splits", "transaction_currency", "instrument_currency", "flow_currency",
    "future_trade", "future_split", "historical_start", "closed_positions", "empty",
])
def test_batched_frames_and_diagnostics_match_previous_path(golden_spy_db, monkeypatch, scenario):
    c, pid = golden_spy_db
    if scenario in {"duplicate", "conflicting", "nullable"}:
        c.execute("CREATE TEMP TABLE copy_prices AS SELECT * FROM prices")
        c.execute("DROP TABLE prices")
        c.execute("CREATE TABLE prices AS SELECT * FROM copy_prices")
        if scenario == "nullable":
            c.execute("UPDATE prices SET adjusted_close=NULL WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
        else:
            c.execute("INSERT INTO prices SELECT * FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
            if scenario == "conflicting":
                c.execute("UPDATE prices SET source='yfinance'")
                c.execute("UPDATE prices SET close=close+12,adjusted_close=adjusted_close-17,source='tiingo' WHERE rowid=(SELECT MAX(rowid) FROM prices)")
    elif scenario == "mixed_sources":
        c.execute("UPDATE prices SET adjusted_close=close*.9,source=CASE WHEN date='2026-01-07' THEN 'tiingo' ELSE 'yfinance' END")
    elif scenario in {"missing", "stale", "first_missing"}:
        query = "DELETE FROM prices WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')"
        if scenario != "missing":
            query += " AND date='2026-01-07'" if scenario == "stale" else " AND date='2026-01-05'"
        c.execute(query)
    elif scenario == "noncanonical_ticker":
        c.execute("UPDATE instruments SET ticker='aapl' WHERE ticker='AAPL'")
    elif scenario in {"same_day_buy", "same_day_sale"}:
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='NVDA'),'2026-01-08',?,?,55,'USD','test')", (pid, "BUY" if scenario == "same_day_buy" else "SELL", 2 if scenario == "same_day_buy" else 20))
    elif scenario == "fractional_splits":
        for day, ratio in [("2026-01-06", 1.1), ("2026-01-07", 1.3)]:
            c.execute("INSERT INTO corporate_actions(instrument_id,ex_date,action_type,ratio,source,fetched_at) VALUES ((SELECT id FROM instruments WHERE ticker='NVDA'),?,'SPLIT',?,'test','test')", (day, ratio))
    elif scenario == "transaction_currency":
        c.execute("UPDATE transactions SET currency='EUR' WHERE date='2026-01-07'")
    elif scenario == "instrument_currency":
        c.execute("UPDATE instruments SET currency='EUR' WHERE ticker='AAPL'")
    elif scenario == "flow_currency":
        c.execute("UPDATE cash_flows SET currency='EUR'")
    elif scenario == "future_trade":
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    elif scenario == "future_split":
        c.execute("INSERT INTO corporate_actions(instrument_id,ex_date,action_type,ratio,source,fetched_at) VALUES ((SELECT id FROM instruments WHERE ticker='NVDA'),'2026-01-12','SPLIT',.1,'test','test')")
    elif scenario == "closed_positions":
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-08','SELL',6,100,'USD','test')", (pid,))
    elif scenario == "empty":
        c.execute("DELETE FROM transactions")
    if scenario in {"mixed_sources", "conflicting"}:
        c.execute("INSERT OR IGNORE INTO corporate_action_coverage(instrument_id,start_date,end_date,source,fetched_at) SELECT id,'2026-01-01','2026-01-31','tiingo','test' FROM instruments")
    days = DAYS[2:] if scenario == "historical_start" else DAYS
    with reader(c) as r:
        with PreparedValuationContext(r, pid, days) as context:
            for day in days:
                try:
                    expected = previous_value(context, r, pid, day)
                except ValueError as error:
                    with pytest.raises(ValueError) as actual:
                        context.value(r, pid, day)
                    assert str(actual.value) == str(error)
                else:
                    actual = context.value(r, pid, day)
                    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
                    repeated = context.value(r, pid, day)
                    pd.testing.assert_frame_equal(repeated, expected, check_exact=True)
                    if not actual.empty:
                        actual.loc[:, "market_value"] = 999
                        pd.testing.assert_frame_equal(context.value(r, pid, day), expected, check_exact=True)
            if scenario in {"stale", "first_missing", "nullable"}:
                assert ("2026-01-07" if scenario != "first_missing" else DAYS[0]) not in context._exact_join
            if scenario in {"duplicate", "conflicting"}:
                assert len(context._exact_join["2026-01-07"]) == 4
        kwargs = dict(start_date=days[0], end_date=days[-1], session_calendar=CALENDAR)
        actual = analysis_service.prepare_analysis(r, pid, **kwargs)

        def disable_batch(self):
            self._exact_join = {}

        monkeypatch.setattr(PreparedValuationContext, "_build_exact_join", disable_batch)
        assert analysis_service.prepare_analysis(r, pid, **kwargs) == actual


def test_builds_one_join_and_two_record_frames_per_request(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    merges, constructions = [], []
    original_merge = pd.DataFrame.merge
    original_records = pd.DataFrame.from_records

    def merge(self, *args, **kwargs):
        merges.append(True)
        return original_merge(self, *args, **kwargs)

    def records(cls, *args, **kwargs):
        constructions.append(True)
        return original_records(*args, **kwargs)

    with reader(c) as r:
        with PreparedValuationContext(r, pid, DAYS) as context:
            monkeypatch.setattr(pd.DataFrame, "merge", merge)
            monkeypatch.setattr(pd.DataFrame, "from_records", classmethod(records))
            for day in DAYS:
                context.value(r, pid, day)
            context.value(r, pid, DAYS[0])
            assert len(merges) == 1
            assert len(constructions) == 2
            assert len(context._exact_join) == len(DAYS)
        assert context._exact_join is None


def test_xle_split_joined_rows_match_previous_path_exactly():
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    if not path.exists():
        pytest.skip("Historical database is unavailable")
    days = ["2025-12-04", "2025-12-05", "2025-12-08"]
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        with PreparedValuationContext(c, 1, days) as context:
            for day in days:
                actual = context.value(c, 1, day)
                pd.testing.assert_frame_equal(actual, previous_value(context, c, 1, day), check_exact=True)
            assert set(context._exact_join) == set(days)


def test_batch_memory_limit_preserves_previous_path(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    monkeypatch.setattr(valuation_context, "_MAX_EXACT_JOIN_ROWS", 1)
    with reader(c) as r:
        with PreparedValuationContext(r, pid, DAYS) as context:
            for day in DAYS:
                pd.testing.assert_frame_equal(context.value(r, pid, day), previous_value(context, r, pid, day), check_exact=True)
            assert context._exact_join == {}


def test_unsupported_future_position_does_not_break_earlier_value(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("UPDATE transactions SET quantity='unsupported' WHERE date='2026-01-07'")
    with reader(c) as r:
        with PreparedValuationContext(r, pid, DAYS) as context:
            expected = previous_value(context, r, pid, DAYS[0])
            pd.testing.assert_frame_equal(context.value(r, pid, DAYS[0]), expected, check_exact=True)
            assert context._exact_join == {}
