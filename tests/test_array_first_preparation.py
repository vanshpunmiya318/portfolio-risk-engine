from contextlib import closing
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from test_batched_valuation_join import DAYS
from test_valuation_context import reader
from riskengine.db.portfolios import get_portfolio_value
from riskengine.risk import analysis_service
from riskengine.risk.valuation_context import PreparedValuationContext


def compare_frame_and_total(context, connection, pid, day):
    try:
        frame = get_portfolio_value(connection, pid, day, max_age_days=0)
        expected = context._frame_total(frame)
    except (ValueError, TypeError) as error:
        with pytest.raises(type(error)) as actual:
            context.holdings_total(connection, pid, day)
        assert str(actual.value) == str(error)
    else:
        actual = context.holdings_total(connection, pid, day)
        assert np.float64(actual).tobytes() == np.float64(expected).tobytes()
        pd.testing.assert_frame_equal(context.value(connection, pid, day), frame, check_exact=True)


def test_bulk_buffers_normalize_once_and_legacy_frames_are_lazy(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    conversions = []
    original = pd.to_datetime

    def record(*args, **kwargs):
        conversions.append(True)
        return original(*args, **kwargs)

    with reader(c) as r:
        with PreparedValuationContext(r, pid, DAYS) as context:
            monkeypatch.setattr(pd, "to_datetime", record)
            context._build_exact_join()
            assert len(conversions) == 1
            assert context._exact_join._frames == {}
            assert DAYS[0] in context._exact_join
            assert context._exact_join._frames == {}
            for day in DAYS:
                assert np.isfinite(context.holdings_total(r, pid, day))
            assert len(conversions) == 1
            assert context._exact_join._frames == {}
            first = context._ordered_holdings_inputs(DAYS[0])
            last = context._ordered_holdings_inputs(DAYS[-1])
            assert first.quantity.base is last.quantity.base
            assert first.price.base is last.price.base
            assert not first.quantity.flags.writeable and not last.price.flags.writeable
            with pytest.raises(ValueError):
                first.price[0] = 999
            frame = context.value(r, pid, DAYS[0])
            assert set(context._exact_join._frames) == {DAYS[0]}
            pd.testing.assert_frame_equal(frame, get_portfolio_value(r, pid, DAYS[0], max_age_days=0), check_exact=True)
        assert context._exact_join is None and context._holdings_inputs == {}


@pytest.mark.parametrize("scenario", [
    "duplicates", "mixed_sources", "adjusted_inf", "raw_inf", "unused_inf",
    "nullable", "nullable_source", "integers", "numeric_text", "compact_date", "intraday_date",
    "same_day_trade", "fractional_splits", "future_trade", "historical_cutoff",
])
def test_bulk_arrays_preserve_rows_dtypes_and_fallbacks(golden_spy_db, scenario):
    c, pid = golden_spy_db
    if scenario in {"duplicates", "nullable", "nullable_source", "integers", "numeric_text"}:
        c.execute("CREATE TEMP TABLE saved_prices AS SELECT * FROM prices")
        c.execute("DROP TABLE prices")
        if scenario in {"integers", "numeric_text"}:
            dtype = "INTEGER" if scenario == "integers" else "TEXT"
            c.execute(f"CREATE TABLE prices AS SELECT id,instrument_id,date,open,high,low,CAST(close AS {dtype}) AS close,CAST(adjusted_close AS {dtype}) AS adjusted_close,volume,source,fetched_at FROM saved_prices")
        else:
            c.execute("CREATE TABLE prices AS SELECT * FROM saved_prices")
            if scenario == "duplicates":
                c.execute("INSERT INTO prices SELECT * FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
                c.execute("UPDATE prices SET source='yfinance'")
                c.execute("UPDATE prices SET close=close+13,adjusted_close=adjusted_close-19,source='tiingo' WHERE rowid=(SELECT MAX(rowid) FROM prices)")
            else:
                column = "source" if scenario == "nullable_source" else "adjusted_close"
                c.execute(f"UPDATE prices SET {column}=NULL WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    elif scenario == "mixed_sources":
        c.execute("UPDATE prices SET adjusted_close=close*.9,source=CASE WHEN date='2026-01-07' THEN 'tiingo' ELSE 'yfinance' END")
    elif scenario in {"adjusted_inf", "raw_inf", "unused_inf"}:
        column = "close" if scenario == "raw_inf" else "adjusted_close"
        source = "tiingo" if scenario in {"raw_inf", "unused_inf"} else "yfinance"
        c.execute(f"UPDATE prices SET {column}=?,source=? WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (float("inf"), source))
    elif scenario in {"compact_date", "intraday_date"}:
        c.execute("UPDATE prices SET date=? WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", ("20260107" if scenario == "compact_date" else "2026-01-07T12:00:00",))
    elif scenario == "same_day_trade":
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='NVDA'),'2026-01-08','BUY',2,55,'USD','test')", (pid,))
    elif scenario == "fractional_splits":
        for day, ratio in [("2026-01-06", 1.1), ("2026-01-07", 1.3)]:
            c.execute("INSERT INTO corporate_actions(instrument_id,ex_date,action_type,ratio,source,fetched_at) VALUES ((SELECT id FROM instruments WHERE ticker='NVDA'),?,'SPLIT',?,'test','test')", (day, ratio))
    elif scenario in {"future_trade", "historical_cutoff"}:
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    c.execute("INSERT OR IGNORE INTO corporate_action_coverage(instrument_id,start_date,end_date,source,fetched_at) SELECT id,'2026-01-01','2026-01-31','tiingo','test' FROM instruments")
    days = DAYS[2:] if scenario == "historical_cutoff" else DAYS
    with reader(c) as r:
        with PreparedValuationContext(r, pid, days) as context:
            for day in days:
                compare_frame_and_total(context, r, pid, day)
            if scenario in {"integers", "numeric_text"}:
                assert context._exact_join == {}
            if scenario in {"nullable", "nullable_source", "compact_date", "intraday_date"}:
                assert "2026-01-07" not in context._exact_join
            if scenario == "duplicates":
                assert len(context._exact_join["2026-01-07"]) == 4


def test_extreme_canonical_dates_retain_per_date_parser_eligibility(golden_spy_db):
    c, pid = golden_spy_db
    days = ["0001-01-01", "1660-01-01", "1678-01-01", "2024-02-29", "2261-12-31", "2500-01-01", "9999-12-31"]
    c.execute("UPDATE transactions SET date='0001-01-01'")
    for day in days:
        c.execute("INSERT INTO prices(instrument_id,date,open,high,low,close,adjusted_close,volume,source,fetched_at) SELECT id,?,100,100,100,100,100,1000,'test','test' FROM instruments WHERE ticker!='SPY'", (day,))
    with reader(c) as r:
        with PreparedValuationContext(r, pid, days) as context:
            for day in days:
                compare_frame_and_total(context, r, pid, day)


def test_invalid_requested_calendar_date_keeps_original_error(golden_spy_db):
    c, pid = golden_spy_db
    with reader(c) as r:
        with PreparedValuationContext(r, pid, [DAYS[0], "2026-02-30"]) as context:
            compare_frame_and_total(context, r, pid, "2026-02-30")


def test_no_financial_reduction_changes_on_full_preparation(golden_spy_db, monkeypatch):
    from test_analysis_service import CALENDAR

    c, pid = golden_spy_db
    with reader(c) as r:
        kwargs = dict(start_date=DAYS[0], end_date=DAYS[-1], session_calendar=CALENDAR)
        actual = analysis_service.prepare_analysis(r, pid, **kwargs)
        original = PreparedValuationContext.holdings_total

        def frames(self, connection, portfolio_id, day):
            return original(self, connection, portfolio_id, day, use_arrays=False)

        monkeypatch.setattr(PreparedValuationContext, "holdings_total", frames)
        assert analysis_service.prepare_analysis(r, pid, **kwargs) == actual
