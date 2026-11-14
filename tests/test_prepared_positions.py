from pathlib import Path
from contextlib import closing
import sqlite3

import pandas as pd
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from test_analysis_service import CALENDAR, prepare
from riskengine.db import portfolios
from riskengine.risk import analysis_service


def begin(connection):
    if not connection.in_transaction:
        connection.execute("BEGIN")


@pytest.mark.parametrize("day", ["2026-01-05", "2026-01-07", "2026-01-08", "2026-01-09"])
def test_prepared_positions_preserve_trade_and_split_date_values(golden_db, day, monkeypatch):
    c, _, pid = golden_db
    path = Path(c.execute("PRAGMA database_list").fetchone()[2])
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
        reader.execute("BEGIN")
        expected = portfolios.get_portfolio_value(reader, pid, day, max_age_days=0)
        prepared = portfolios.prepare_positions(reader, pid, day)

        def forbidden(*args, **kwargs):
            pytest.fail("Valuation must reuse prepared positions")

        monkeypatch.setattr(portfolios, "get_positions", forbidden)
        actual = portfolios.get_portfolio_value(reader, pid, day, max_age_days=0, prepared_positions=prepared)
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)


@pytest.mark.parametrize("day,quantity", [("2025-12-04", 118.), ("2025-12-05", 236.), ("2025-12-08", 236.)])
def test_historical_xle_split_exact_parity(day, quantity):
    path = Path(__file__).resolve().parents[1] / "data/historical_portfolio.db"
    if not path.exists():
        pytest.skip("Historical database is not available")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("BEGIN")
        expected = portfolios.get_portfolio_value(c, 1, day, max_age_days=0)
        prepared = portfolios.prepare_positions(c, 1, day)
        actual = portfolios.get_portfolio_value(c, 1, day, max_age_days=0, prepared_positions=prepared)
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)
        assert actual.set_index("ticker").loc["XLE", "quantity"] == pytest.approx(quantity)


@pytest.mark.parametrize("defect", ["date", "portfolio", "connection", "ended", "write", "raw_frame"])
def test_mismatched_or_stale_positions_are_rejected(golden_db, defect):
    c, _, pid = golden_db
    begin(c)
    day = "2026-01-07"
    prepared = portfolios.prepare_positions(c, pid, day)
    other = None
    if defect == "date":
        day = "2026-01-08"  # Intervening split.
    elif defect == "portfolio":
        pid += 1
    elif defect == "connection":
        other = sqlite3.connect(c.execute("PRAGMA database_list").fetchone()[2])
        other.execute("BEGIN")
        c = other
    elif defect == "ended":
        c.commit()
    elif defect == "write":
        c.execute("UPDATE transactions SET quantity=quantity+1 WHERE transaction_type='BUY'")
    else:
        prepared = prepared.to_frame()
    try:
        with pytest.raises(ValueError, match="Prepared positions"):
            portfolios.get_portfolio_value(c, pid, day, prepared_positions=prepared)
    finally:
        if other is not None:
            other.close()


def test_preparation_requires_transaction_but_legacy_does_not(golden_db):
    c, _, pid = golden_db
    c.commit()
    with pytest.raises(ValueError, match="active caller-owned"):
        portfolios.prepare_positions(c, pid, "2026-01-05")
    assert not portfolios.get_portfolio_value(c, pid, "2026-01-05").empty


@pytest.mark.parametrize("scenario", ["missing_price", "invalid_positions", "future_trade", "currency", "corporate_coverage"])
def test_analysis_diagnostics_match_original_positions_path(golden_spy_db, monkeypatch, scenario):
    c, pid = golden_spy_db
    if scenario == "missing_price":
        c.execute("DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    elif scenario in {"invalid_positions", "future_trade"}:
        day = "2026-01-07" if scenario == "invalid_positions" else "2026-01-12"
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),?,'SELL',999,100,'USD','test')", (pid, day))
    elif scenario == "currency":
        c.execute("UPDATE transactions SET currency='EUR' WHERE date='2026-01-07'")
    else:
        c.execute("UPDATE prices SET source='tiingo'")
        c.execute("DELETE FROM corporate_action_coverage")
    optimized = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR)
    original = analysis_service.get_portfolio_value

    def legacy(*args, **kwargs):
        kwargs.pop("prepared_positions", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis_service, "get_portfolio_value", legacy)
    assert prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR) == optimized


def test_missing_and_stale_price_errors_are_unchanged(golden_db):
    c, _, pid = golden_db
    c.execute("DELETE FROM prices WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    for extra in ({}, {"prepared_positions": portfolios.prepare_positions(c, pid, "2026-01-07")}):
        with pytest.raises(ValueError, match="No price available as of 2026-01-07 for:.*AAPL"):
            portfolios.get_portfolio_value(c, pid, "2026-01-07", max_age_days=0, **extra)
    c.execute("DELETE FROM prices WHERE date='2026-01-07'")
    # A missing instrument still takes priority over stale-price diagnostics.
    with pytest.raises(ValueError, match="No price available"):
        portfolios.get_portfolio_value(c, pid, "2026-01-07", max_age_days=0,
                                       prepared_positions=portfolios.prepare_positions(c, pid, "2026-01-07"))


def test_stale_prices_and_partial_values_preserve_legacy_behavior(golden_db):
    c, _, pid = golden_db
    c.execute("DELETE FROM prices WHERE date='2026-01-07'")
    for extra in ({}, {"prepared_positions": portfolios.prepare_positions(c, pid, "2026-01-07")}):
        with pytest.raises(ValueError, match="Price exceeds maximum permitted age on 2026-01-07"):
            portfolios.get_portfolio_value(c, pid, "2026-01-07", max_age_days=0, **extra)
    c.execute("DELETE FROM prices WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    expected = portfolios.get_portfolio_value(c, pid, "2026-01-07", allow_missing_prices=True)
    actual = portfolios.get_portfolio_value(c, pid, "2026-01-07", allow_missing_prices=True,
                                           prepared_positions=portfolios.prepare_positions(c, pid, "2026-01-07"))
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    assert pd.isna(actual.set_index("ticker").loc["AAPL", "market_value"])


def test_frame_copy_cannot_mutate_prepared_positions(golden_db):
    c, _, pid = golden_db
    begin(c)
    prepared = portfolios.prepare_positions(c, pid, "2026-01-07")
    frame = prepared.to_frame()
    frame.loc[:, "quantity"] = 999
    actual = portfolios.get_portfolio_value(c, pid, "2026-01-07", prepared_positions=prepared)
    expected = portfolios.get_portfolio_value(c, pid, "2026-01-07")
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)


def test_changed_database_snapshot_is_rejected(golden_db):
    c, _, pid = golden_db
    c.commit()
    c.execute("BEGIN")
    prepared = portfolios.prepare_positions(c, pid, "2026-01-07")
    path = c.execute("PRAGMA database_list").fetchone()[2]
    c.commit()
    with closing(sqlite3.connect(path)) as other:
        other.execute("UPDATE transactions SET quantity=quantity+1 WHERE transaction_type='BUY'")
        other.commit()
    c.execute("BEGIN")
    with pytest.raises(ValueError, match="Prepared positions"):
        portfolios.get_portfolio_value(c, pid, "2026-01-07", prepared_positions=prepared)


def test_rollback_before_new_snapshot_cannot_reuse_uncommitted_positions(golden_db):
    c, _, pid = golden_db
    c.execute("UPDATE transactions SET quantity=quantity+1 WHERE transaction_type='BUY'")
    prepared = portfolios.prepare_positions(c, pid, "2026-01-07")
    c.rollback()
    c.execute("BEGIN")
    expected = portfolios.get_portfolio_value(c, pid, "2026-01-07")
    actual = portfolios.get_portfolio_value(c, pid, "2026-01-07", prepared_positions=prepared)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)


def test_same_day_split_precedes_trade_and_future_sales_are_excluded(golden_db):
    c, instruments, pid = golden_db
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,?,'2026-01-08','BUY',2,55,'USD','test')", (pid, instruments["NVDA"]))
    c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,?,'2026-01-12','SELL',999,55,'USD','test')", (pid, instruments["NVDA"]))
    c.commit()
    path = Path(c.execute("PRAGMA database_list").fetchone()[2])
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
        reader.execute("BEGIN")
        prepared = portfolios.prepare_positions(reader, pid, "2026-01-08")
        actual = portfolios.get_portfolio_value(reader, pid, "2026-01-08", max_age_days=0, prepared_positions=prepared)
        pd.testing.assert_frame_equal(actual, portfolios.get_portfolio_value(reader, pid, "2026-01-08", max_age_days=0), check_exact=True)
        assert actual.set_index("ticker").loc["NVDA", "quantity"] == pytest.approx(22.)
        with pytest.raises(ValueError, match="Negative position detected"):
            portfolios.get_portfolio_value(reader, pid, "2026-01-12",
                                           prepared_positions=portfolios.prepare_positions(reader, pid, "2026-01-12"))
