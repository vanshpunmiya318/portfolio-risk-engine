from contextlib import closing
from pathlib import Path
import sqlite3

import numpy as np
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from test_analysis_service import CALENDAR
from test_batched_valuation_join import DAYS
from test_valuation_context import reader
from riskengine.risk import analysis_service
from riskengine.risk.valuation_context import PreparedValuationContext, ValuationContextError


@pytest.mark.parametrize("scenario", [
    "complete", "duplicates", "mixed_providers", "null", "adjusted_inf", "raw_inf",
    "unused_adjusted_inf", "unused_raw_inf", "coverage_before_nonfinite", "missing", "stale",
    "transaction_currency", "instrument_currency", "cash_currency", "same_day_buy",
    "same_day_sale", "fractional_splits", "future_trade", "historical_start", "empty",
    "cash_nonfinite", "product_overflow", "reduction_overflow",
])
def test_array_totals_and_full_preparation_match_dataframe(golden_spy_db, monkeypatch, scenario):
    c, pid = golden_spy_db
    if scenario in {"duplicates", "null", "reduction_overflow"}:
        c.execute("CREATE TEMP TABLE copy_prices AS SELECT * FROM prices")
        c.execute("DROP TABLE prices")
        c.execute("CREATE TABLE prices AS SELECT * FROM copy_prices")
        if scenario == "null":
            c.execute("UPDATE prices SET adjusted_close=NULL WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
        elif scenario == "reduction_overflow":
            c.execute("INSERT INTO prices SELECT * FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
            c.execute("INSERT INTO prices SELECT * FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
            c.execute("UPDATE prices SET adjusted_close=1.1e307 WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
        else:
            c.execute("INSERT INTO prices SELECT * FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
            c.execute("UPDATE prices SET source='yfinance'")
            c.execute("UPDATE prices SET adjusted_close=adjusted_close-17,close=close+12,source='tiingo' WHERE rowid=(SELECT MAX(rowid) FROM prices)")
    elif scenario == "mixed_providers":
        c.execute("UPDATE prices SET adjusted_close=close*.9,source=CASE WHEN date='2026-01-07' THEN 'tiingo' ELSE 'yfinance' END")
    elif scenario in {"adjusted_inf", "raw_inf", "unused_adjusted_inf", "unused_raw_inf", "coverage_before_nonfinite"}:
        column = "close" if scenario in {"raw_inf", "unused_raw_inf", "coverage_before_nonfinite"} else "adjusted_close"
        source = "tiingo" if scenario in {"raw_inf", "unused_adjusted_inf", "coverage_before_nonfinite"} else "yfinance"
        c.execute(f"UPDATE prices SET {column}=?,source=? WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (float("inf"), source))
    elif scenario in {"missing", "stale"}:
        c.execute("DELETE FROM prices WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')" + (" AND date='2026-01-07'" if scenario == "stale" else ""))
    elif scenario == "transaction_currency":
        c.execute("UPDATE transactions SET currency='EUR' WHERE date='2026-01-07'")
    elif scenario == "instrument_currency":
        c.execute("UPDATE instruments SET currency='EUR' WHERE ticker='AAPL'")
    elif scenario == "cash_currency":
        c.execute("UPDATE cash_flows SET currency='EUR'")
    elif scenario in {"same_day_buy", "same_day_sale"}:
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='NVDA'),'2026-01-08',?,?,55,'USD','test')", (pid, "BUY" if scenario == "same_day_buy" else "SELL", 2 if scenario == "same_day_buy" else 20))
    elif scenario == "fractional_splits":
        for day, ratio in [("2026-01-06", 1.1), ("2026-01-07", 1.3)]:
            c.execute("INSERT INTO corporate_actions(instrument_id,ex_date,action_type,ratio,source,fetched_at) VALUES ((SELECT id FROM instruments WHERE ticker='NVDA'),?,'SPLIT',?,'test','test')", (day, ratio))
    elif scenario == "future_trade":
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    elif scenario == "empty":
        c.execute("DELETE FROM transactions")
    elif scenario == "cash_nonfinite":
        c.execute("UPDATE cash_flows SET amount=?", (float("inf"),))
    elif scenario == "product_overflow":
        c.execute("UPDATE prices SET adjusted_close=1e308 WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    if scenario != "coverage_before_nonfinite":
        c.execute("INSERT OR IGNORE INTO corporate_action_coverage(instrument_id,start_date,end_date,source,fetched_at) SELECT id,'2026-01-01','2026-01-31','tiingo','test' FROM instruments")
    else:
        c.execute("DELETE FROM corporate_action_coverage")
    days = DAYS[2:] if scenario == "historical_start" else DAYS
    with reader(c) as r:
        with PreparedValuationContext(r, pid, days) as context:
            for day in days:
                try:
                    expected = context.holdings_total(r, pid, day, use_arrays=False)
                except ValueError as error:
                    with pytest.raises(ValueError) as actual:
                        context.holdings_total(r, pid, day)
                    assert str(actual.value) == str(error)
                else:
                    actual = context.holdings_total(r, pid, day)
                    assert np.float64(actual).tobytes() == np.float64(expected).tobytes()
            if scenario == "coverage_before_nonfinite":
                with pytest.raises(ValueError, match="Incomplete corporate action coverage"):
                    context.holdings_total(r, pid, "2026-01-07")
        kwargs = dict(start_date=days[0], end_date=days[-1], session_calendar=CALENDAR)
        actual = analysis_service.prepare_analysis(r, pid, **kwargs)
        original = PreparedValuationContext.holdings_total

        def dataframe_path(self, connection, portfolio_id, day):
            return original(self, connection, portfolio_id, day, use_arrays=False)

        monkeypatch.setattr(PreparedValuationContext, "holdings_total", dataframe_path)
        assert analysis_service.prepare_analysis(r, pid, **kwargs) == actual


@pytest.mark.parametrize("count", [1, 7, 8, 17, 64, 129])
def test_duplicate_order_and_pandas_reduction_are_bit_identical(golden_spy_db, count):
    c, pid = golden_spy_db
    c.execute("CREATE TEMP TABLE copy_prices AS SELECT * FROM prices")
    c.execute("DROP TABLE prices")
    c.execute("CREATE TABLE prices AS SELECT * FROM copy_prices")
    row = c.execute("SELECT rowid FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')").fetchone()[0]
    prices = [1e16, .1, -1e16, 3.333, 123.4, 1e-10]
    for index in range(count):
        if index:
            c.execute("INSERT INTO prices SELECT * FROM prices WHERE rowid=?", (row,))
            target = c.execute("SELECT MAX(rowid) FROM prices").fetchone()[0]
        else:
            target = row
        c.execute("UPDATE prices SET adjusted_close=? WHERE rowid=?", (prices[index % len(prices)], target))
    with reader(c) as r:
        with PreparedValuationContext(r, pid, ["2026-01-07"]) as context:
            expected = context.holdings_total(r, pid, "2026-01-07", use_arrays=False)
            actual = context.holdings_total(r, pid, "2026-01-07")
            assert np.float64(actual).tobytes() == np.float64(expected).tobytes()
            inputs = context._holdings_inputs["2026-01-07"]
            frame = context.value(r, pid, "2026-01-07")
            np.testing.assert_array_equal(np.multiply(inputs.quantity, inputs.price), frame.market_value.to_numpy())


def test_complete_dates_avoid_output_frames_and_arrays_are_scope_local(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    with reader(c) as r:
        with PreparedValuationContext(r, pid, DAYS) as context:
            def forbidden(*args, **kwargs):
                pytest.fail("Complete Performance totals must not materialize output DataFrames")

            monkeypatch.setattr(context, "value", forbidden)
            for day in DAYS:
                assert np.isfinite(context.holdings_total(r, pid, day))
            inputs = context._holdings_inputs[DAYS[0]]
            assert inputs.quantity.dtype == inputs.price.dtype == np.dtype("float64")
            with pytest.raises(ValueError):
                inputs.price[0] = 999
            with pytest.raises(ValuationContextError):
                context.holdings_total(c, pid, DAYS[0])
            with pytest.raises(ValuationContextError):
                context.holdings_total(r, pid + 1, DAYS[0])
            with pytest.raises(ValuationContextError):
                context.holdings_total(r, pid, "2026-01-12")
        assert context._holdings_inputs == {}
        with pytest.raises(ValuationContextError):
            context.holdings_total(r, pid, DAYS[0])


def test_xle_split_array_totals_are_exact():
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    if not path.exists():
        pytest.skip("Historical database is unavailable")
    days = ["2025-12-04", "2025-12-05", "2025-12-08"]
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        with PreparedValuationContext(c, 1, days) as context:
            for day in days:
                assert context.holdings_total(c, 1, day) == context.holdings_total(c, 1, day, use_arrays=False)
