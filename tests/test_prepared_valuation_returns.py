from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from test_golden_portfolio import golden_db, cash_flow_golden_db
from test_benchmark import golden_spy_db
from test_analysis_service import CALENDAR, prepare
from riskengine.db import portfolios
from riskengine.risk import analysis_service
from riskengine.risk.analysis_models import AnalysisValuation
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns
from riskengine.risk.return_intervals import get_portfolio_return_observations


@pytest.mark.parametrize("scenario", [
    "complete", "deposit", "withdrawal", "zero_baseline", "negative_baseline",
    "ending_zero", "ending_negative", "missing_price", "historical_cutoff",
    "currency_conflict", "corporate_action_coverage", "closed_reverse_split_price_gap",
])
def test_preparation_diagnostics_match_legacy_return_path(golden_spy_db, monkeypatch, scenario):
    c, pid = golden_spy_db
    if scenario in {"deposit", "withdrawal", "zero_baseline", "negative_baseline", "ending_zero", "ending_negative"}:
        day, kind, amount = {
            "deposit": ("2026-01-07", "DEPOSIT", 5000),
            "withdrawal": ("2026-01-07", "WITHDRAWAL", 5000),
            "zero_baseline": ("2026-01-05", "WITHDRAWAL", 20000),
            "negative_baseline": ("2026-01-05", "WITHDRAWAL", 21000),
            "ending_zero": ("2026-01-09", "WITHDRAWAL", 20292),
            "ending_negative": ("2026-01-09", "WITHDRAWAL", 30000),
        }[scenario]
        c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,?,?,?, 'USD','test')", (pid, day, kind, amount))
    elif scenario == "missing_price":
        c.execute("DELETE FROM prices WHERE date='2026-01-07' AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')")
    elif scenario == "historical_cutoff":
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='AAPL'),'2026-01-12','SELL',999,100,'EUR','test')", (pid,))
    elif scenario == "currency_conflict":
        c.execute("UPDATE transactions SET currency='EUR' WHERE date='2026-01-07'")
    elif scenario == "corporate_action_coverage":
        c.execute("UPDATE instruments SET primary_source='tiingo' WHERE ticker!='SPY'")
        c.execute("UPDATE prices SET source='tiingo' WHERE instrument_id!=(SELECT id FROM instruments WHERE ticker='SPY')")
        c.execute("DELETE FROM corporate_action_coverage")
    elif scenario == "closed_reverse_split_price_gap":
        c.execute("UPDATE corporate_actions SET ratio=.5 WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='NVDA')")
        c.execute("INSERT INTO transactions(portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (?,(SELECT id FROM instruments WHERE ticker='NVDA'),'2026-01-08','SELL',5,55,'USD','test')", (pid,))
        c.execute("DELETE FROM prices WHERE date='2026-01-09' AND instrument_id=(SELECT id FROM instruments WHERE ticker='NVDA')")
    optimized = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR)
    original = analysis_service.get_portfolio_return_observations

    def legacy_path(*args, **kwargs):
        kwargs.pop("prepared_valuations", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis_service, "get_portfolio_return_observations", legacy_path)
    legacy = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR)
    assert optimized == legacy  # Every valuation, return, boundary and diagnostic.
    if scenario == "closed_reverse_split_price_gap":
        assert any(issue.code == "return_coverage" for issue in optimized.issues)
    if scenario == "corporate_action_coverage":
        assert any(issue.code == "corporate_action_conflict" for issue in optimized.issues)


def test_preparation_values_each_date_only_once(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    original = portfolios.get_portfolio_value
    dates = []

    def counted(*args, **kwargs):
        dates.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(portfolios, "get_portfolio_value", counted)
    monkeypatch.setattr(analysis_service, "get_portfolio_value", counted)
    result = prepare(c, pid, session_calendar=CALENDAR)
    assert len(dates) == len(set(dates)) == len(result.valuations) == 5
    assert len(result.portfolio_intervals) == 4


def test_prepared_producer_does_not_load_equity_again(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    result = prepare(c, pid, session_calendar=CALENDAR)
    dates = [row.date for row in result.valuations]
    legacy = get_portfolio_return_observations(c, pid, dates, calendar=CALENDAR)

    def forbidden(*args, **kwargs):
        pytest.fail("Prepared return construction must not recalculate portfolio equity")

    monkeypatch.setattr(portfolios, "get_total_portfolio_equity", forbidden)
    observations = get_portfolio_return_observations(c, pid, dates, calendar=CALENDAR, prepared_valuations=result.valuations)
    assert observations == legacy
    assert c.in_transaction


def test_cash_flow_adjustment_is_preserved_with_prepared_values(cash_flow_golden_db):
    c, pid = cash_flow_golden_db
    data = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-08")
    dates = [row.date for row in data.valuations]
    expected = get_portfolio_return_observations(c, pid, dates)
    actual = get_portfolio_return_observations(c, pid, dates, prepared_valuations=data.valuations)
    assert actual == expected
    assert [row.return_value for row in actual] == pytest.approx([.05, 0, .02])


def test_irregular_intervals_and_splits_keep_actual_boundaries(golden_spy_db):
    c, pid = golden_spy_db
    data = prepare(c, pid, session_calendar=CALENDAR)
    dates = [date(2026, 1, day) for day in (5, 7, 9)]
    expected = get_portfolio_return_observations(c, pid, dates, calendar=CALENDAR)
    actual = get_portfolio_return_observations(c, pid, dates, calendar=CALENDAR, prepared_valuations=data.valuations)
    assert actual == expected
    assert [row.trading_sessions for row in actual] == [2, 2]


def test_intermediate_cash_flow_still_requires_separate_periods(cash_flow_golden_db):
    c, pid = cash_flow_golden_db
    data = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-08")
    dates = [date(2026, 1, 5), date(2026, 1, 8)]
    for extra in ({}, {"prepared_valuations": data.valuations}):
        with pytest.raises(ValueError, match="Intermediate cash flows require separate"):
            get_portfolio_return_observations(c, pid, dates, **extra)


@pytest.mark.parametrize("defect", ["missing", "incomplete", "nonfinite", "duplicate"])
def test_invalid_prepared_values_fail_without_silent_fallback(golden_spy_db, defect):
    c, pid = golden_spy_db
    data = prepare(c, pid, session_calendar=CALENDAR)
    values = list(data.valuations)
    if defect == "missing":
        values.pop(1)
    elif defect == "incomplete":
        values[1] = replace(values[1], complete=False)
    elif defect == "nonfinite":
        values[1] = replace(values[1], total_equity=float("nan"))
    else:
        values.append(values[1])
    with pytest.raises(ValueError, match="[Pp]repared valuation"):
        get_portfolio_return_observations(c, pid, [v.date for v in data.valuations], prepared_valuations=values)


def test_prepared_values_require_caller_owned_snapshot(golden_spy_db):
    c, pid = golden_spy_db
    data = prepare(c, pid, session_calendar=CALENDAR)
    c.commit()
    with pytest.raises(ValueError, match="transaction"):
        get_portfolio_return_observations(c, pid, [v.date for v in data.valuations], prepared_valuations=data.valuations)
    # No prepared input retains the legacy no-explicit-BEGIN behavior.
    assert get_portfolio_return_observations(c, pid, [v.date for v in data.valuations])


def test_future_incomplete_prepared_value_is_not_consumed(golden_spy_db):
    c, pid = golden_spy_db
    data = prepare(c, pid, session_calendar=CALENDAR)
    extra = AnalysisValuation(date(2026, 1, 12), None, None, None, None, False)
    actual = get_portfolio_return_observations(c, pid, [v.date for v in data.valuations], calendar=CALENDAR,
                                                 prepared_valuations=(*data.valuations, extra))
    assert actual == data.portfolio_intervals


@pytest.mark.parametrize("equity", [0., -100.])
def test_nonpositive_starting_equity_uses_existing_guard(golden_spy_db, equity):
    c, pid = golden_spy_db
    data = prepare(c, pid, session_calendar=CALENDAR)
    values = (replace(data.valuations[0], total_equity=equity), data.valuations[1])
    with pytest.raises(ValueError, match="Starting capital must be positive"):
        get_portfolio_return_observations(c, pid, [v.date for v in values], prepared_valuations=values)


def test_daily_prepared_equities_preserve_public_series_structure(golden_spy_db):
    c, pid = golden_spy_db
    data = prepare(c, pid, session_calendar=CALENDAR)
    dates = [v.date for v in data.valuations]
    expected = get_portfolio_daily_returns(c, pid, dates)
    actual = get_portfolio_daily_returns(c, pid, dates,
                                         prepared_equities={v.date.isoformat(): v.total_equity for v in data.valuations})
    pd.testing.assert_series_equal(actual, expected)
