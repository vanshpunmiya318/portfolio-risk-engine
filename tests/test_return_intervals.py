from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from test_benchmark import golden_spy_db
from test_golden_portfolio import golden_db
from riskengine.risk.benchmark import get_benchmark_price_returns
from riskengine.risk.metrics import portfolio_beta
from riskengine.risk.performance import calculate_performance_metrics, compare_portfolio_to_benchmark
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns
from riskengine.risk.return_intervals import (
    ReturnObservation, VerifiedSessionCalendar,
    observations_from_series, get_benchmark_return_observations,
    get_portfolio_return_observations, calculate_interval_performance,
    compare_interval_returns, interval_beta,
)


def calendar(*days):
    return VerifiedSessionCalendar(tuple(date.fromisoformat(day) for day in days), "Explicit test session calendar")


DAILY = calendar("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09")


def observations(values=(.01, -.02, .03, .01), sessions=DAILY):
    returns = pd.Series(values, index=pd.DatetimeIndex(sessions.session_dates[1:]))
    return observations_from_series(returns, start_dates=sessions.session_dates[:-1], calendar=sessions)


def codes(result):
    return {issue.code for issue in result.reasons}


def test_complete_daily_metrics_and_success_dictionary_are_unchanged():
    p, b = observations(), observations((.005, -.01, .015, -.005))
    ps = pd.Series([r.return_value for r in p], index=pd.DatetimeIndex([r.end_date for r in p]))
    bs = pd.Series([r.return_value for r in b], index=ps.index)
    result = compare_interval_returns(p, b)
    old = compare_portfolio_to_benchmark(ps, bs)
    assert result.status == "ok"
    assert result.baseline_date == date(2026, 1, 5)
    assert result.end_date == date(2026, 1, 9)
    assert result.observations == 4
    assert result.value.keys() == old.keys()
    for side in ("portfolio", "benchmark"):
        for key, value in old[side].items():
            if key == "equity_curve":
                pd.testing.assert_series_equal(result.value[side][key], value)
            else:
                assert result.value[side][key] == value
    metadata_keys = {"start_date", "requested_start_date", "effective_baseline_date", "first_return_end_date",
                     "effective_end_date", "period_metadata_reason", "session_coverage_verified"}
    for key in set(old) - {"portfolio", "benchmark"} - metadata_keys:
        assert result.value[key] == old[key]
    assert result.value["start_date"] == "2026-01-05"
    assert old["start_date"] == "2026-01-06"  # Compatibility convention stays explicit.
    assert interval_beta(p, b).value == portfolio_beta(ps, bs)
    standalone = calculate_interval_performance(p)
    assert standalone.status == "ok"
    expected = calculate_performance_metrics(ps)
    for key in expected:
        if key == "equity_curve":
            pd.testing.assert_series_equal(standalone.value[key], expected[key])
        else:
            assert standalone.value[key] == expected[key]


def test_missing_benchmark_observation_cannot_compare_21_with_33_1_percent(golden_spy_db):
    connection, _ = golden_spy_db
    for day, close in [("2026-01-05", 100), ("2026-01-06", 110), ("2026-01-07", 121), ("2026-01-08", 133.1)]:
        connection.execute("UPDATE prices SET close = ? WHERE date = ? AND instrument_id = (SELECT id FROM instruments WHERE ticker = 'SPY')", (close, day))
    connection.execute("DELETE FROM prices WHERE date = '2026-01-06' AND instrument_id = (SELECT id FROM instruments WHERE ticker = 'SPY')")
    p_series = pd.Series([.1, .1, .1], index=pd.to_datetime(["2026-01-06", "2026-01-07", "2026-01-08"]))
    old_b = get_benchmark_price_returns(connection, "SPY", "2026-01-05", "2026-01-08")
    # Legacy behavior is deliberately retained, exposing why opt-in is needed.
    legacy = compare_portfolio_to_benchmark(p_series, old_b)
    assert legacy["portfolio"]["cumulative_return"] == pytest.approx(.21)
    assert legacy["benchmark"]["cumulative_return"] == pytest.approx(.331)
    p = observations_from_series(p_series, start_dates=DAILY.session_dates[:3], calendar=DAILY)
    b = get_benchmark_return_observations(connection, "SPY", "2026-01-05", "2026-01-08", calendar=DAILY)
    assert b[0].start_date == date(2026, 1, 5)
    assert b[0].end_date == date(2026, 1, 7)
    assert b[0].trading_sessions == 2
    for result in (compare_interval_returns(p, b), interval_beta(p, b)):
        assert result.status == "insufficient_coverage"
        assert result.value is None
        assert {"not_one_session", "unmatched_interval"} <= codes(result)
        assert any(issue.start_date == date(2026, 1, 5) and issue.end_date == date(2026, 1, 7) for issue in result.reasons)


@pytest.mark.parametrize("side", ["portfolio", "benchmark", "both"])
def test_removed_interval_is_not_silently_dropped(side):
    p, b = observations(), observations()
    if side in ("portfolio", "both"):
        p = p[:1] + p[2:]
    if side in ("benchmark", "both"):
        b = b[:1] + b[2:]
    for result in (compare_interval_returns(p, b), interval_beta(p, b)):
        assert result.status == "insufficient_coverage"
        assert "coverage_gap" in codes(result)
        assert result.value is None


def test_matching_multisession_intervals_are_not_daily_observations():
    p = (ReturnObservation(date(2026, 1, 5), date(2026, 1, 7), .21, 2),
         ReturnObservation(date(2026, 1, 7), date(2026, 1, 9), .21, 2))
    for result in (calculate_interval_performance(p), compare_interval_returns(p, p), interval_beta(p, p)):
        assert result.status == "insufficient_coverage"
        assert codes(result) == {"not_one_session"}
        assert result.value is None


def test_end_dates_and_counts_do_not_establish_unknown_starts():
    values = pd.Series([.1, .2, -.1], index=pd.to_datetime(["2026-01-06", "2026-01-08", "2026-01-09"]))
    p = observations_from_series(values, calendar=DAILY)
    assert all(item.start_date is None and item.trading_sessions is None for item in p)
    result = compare_interval_returns(p, p)
    assert result.status == "insufficient_coverage"
    assert "unknown_start" in codes(result)
    # Even later starts are not guessed from a possibly filtered Series.
    assert len([i for i in result.reasons if i.code == "unknown_start"]) == 6


def test_known_boundaries_without_verified_sessions_cannot_be_annualized():
    p = tuple(replace(item, trading_sessions=None) for item in observations())
    for result in (calculate_interval_performance(p), compare_interval_returns(p, p), interval_beta(p, p)):
        assert result.status == "insufficient_coverage"
        assert "unverified_sessions" in codes(result)


def test_a_claimed_session_count_cannot_replace_an_unknown_baseline():
    p = observations()
    ambiguous = (replace(p[0], start_date=None), *p[1:])
    assert ambiguous[0].trading_sessions == 1
    assert "unknown_start" in codes(compare_interval_returns(ambiguous, p))
    assert calculate_interval_performance(ambiguous).value is None


def test_weekend_and_holiday_span_can_be_one_verified_session():
    # Friday -> Tuesday spans the weekend and the Monday exchange holiday.
    sessions = calendar("2026-01-16", "2026-01-20", "2026-01-21")
    p = observations((.02, -.01), sessions)
    b = observations((.01, -.005), sessions)
    assert p[0].trading_sessions == 1
    assert compare_interval_returns(p, b).status == "ok"
    assert interval_beta(p, b).value == pytest.approx(2)
    assert calculate_interval_performance(p).status == "ok"


def test_weekend_span_is_not_automatically_verified():
    values = pd.Series([.02, -.01], index=pd.to_datetime(["2026-01-20", "2026-01-21"]))
    p = observations_from_series(values, start_dates=[date(2026, 1, 16), date(2026, 1, 20)])
    assert p[0].trading_sessions is None
    assert calculate_interval_performance(p).status == "insufficient_coverage"


def test_calendar_boundary_outside_verified_range_stays_unknown():
    values = pd.Series([.02, -.01], index=pd.to_datetime(["2026-01-05", "2026-01-06"]))
    p = observations_from_series(values, start_dates=[date(2026, 1, 2), date(2026, 1, 5)], calendar=DAILY)
    assert p[0].trading_sessions is None
    assert "unverified_sessions" in codes(calculate_interval_performance(p))


@pytest.mark.parametrize("variant", ["duplicate", "overlap", "different_baseline"])
def test_ambiguous_or_different_boundaries_return_reasons(variant):
    p, b = observations(), observations()
    if variant == "duplicate":
        b += (b[0],)
        expected = "duplicate_interval"
    elif variant == "overlap":
        b = (b[0], replace(b[1], start_date=b[0].start_date), *b[2:])
        expected = "overlapping_intervals"
    else:
        b = (replace(b[0], start_date=date(2026, 1, 2)), *b[1:])
        expected = "unmatched_interval"
    result = compare_interval_returns(p, b)
    assert result.status == "insufficient_coverage"
    assert expected in codes(result)


def test_matching_inputs_can_be_sorted_without_changing_interval_identity():
    p = observations()
    a = compare_interval_returns(reversed(p), p)
    b = compare_interval_returns(p, p)
    assert a.status == "ok"
    pd.testing.assert_series_equal(a.value["portfolio"]["equity_curve"], b.value["portfolio"]["equity_curve"])


def test_one_matching_interval_is_insufficient():
    p = observations()[:1]
    assert "insufficient_observations" in codes(compare_interval_returns(p, p))
    assert interval_beta(p, p).value is None
    assert calculate_interval_performance(()).status == "insufficient_coverage"


def test_zero_variance_beta_is_explicitly_undefined():
    result = interval_beta(observations(), observations((.01, .01, .01, .01)))
    assert result.status == "undefined_metric"
    assert result.value is None
    assert codes(result) == {"zero_benchmark_variance"}


def test_portfolio_producer_preserves_legacy_series_and_actual_cutoffs(golden_db):
    connection, _, pid = golden_db
    dates = pd.date_range("2026-01-05", "2026-01-09")
    legacy = get_portfolio_daily_returns(connection, pid, dates)
    result = get_portfolio_return_observations(connection, pid, dates, calendar=DAILY)
    assert isinstance(legacy, pd.Series)
    assert [r.return_value for r in result] == list(legacy)
    assert [r.start_date for r in result] == list(DAILY.session_dates[:-1])
    assert all(r.trading_sessions == 1 for r in result)
    unverified = get_portfolio_return_observations(connection, pid, dates)
    assert all(r.trading_sessions is None for r in unverified)


def test_portfolio_producer_retains_multisession_gap_instead_of_splitting_it(golden_db):
    connection, _, pid = golden_db
    dates = pd.to_datetime(["2026-01-05", "2026-01-07", "2026-01-08"])
    result = get_portfolio_return_observations(connection, pid, dates, calendar=DAILY)
    assert len(result) == 2
    assert result[0].start_date == date(2026, 1, 5)
    assert result[0].end_date == date(2026, 1, 7)
    assert result[0].trading_sessions == 2
    assert calculate_interval_performance(result).status == "insufficient_coverage"


def test_benchmark_producer_preserves_legacy_values_without_inventing_calendar(golden_spy_db):
    connection, _ = golden_spy_db
    legacy = get_benchmark_price_returns(connection, "SPY", "2026-01-05", "2026-01-09")
    result = get_benchmark_return_observations(connection, "SPY", "2026-01-05", "2026-01-09")
    assert isinstance(legacy, pd.Series)
    assert [r.return_value for r in result] == list(legacy)
    assert result[0].start_date == date(2026, 1, 5)
    assert all(r.trading_sessions is None for r in result)


@pytest.mark.parametrize("kwargs", [
    {"return_value": float("nan")}, {"return_value": float("inf")},
    {"return_value": -1.01}, {"trading_sessions": True}, {"trading_sessions": -1},
    {"start_date": date(2026, 1, 7)}, {"end_date": 1},
])
def test_invalid_observation_values_rejected(kwargs):
    args = dict(start_date=date(2026, 1, 5), end_date=date(2026, 1, 6), return_value=.01, trading_sessions=1)
    args.update(kwargs)
    with pytest.raises(ValueError):
        ReturnObservation(**args)


def test_calendar_rejects_ambiguous_session_lists_and_missing_source():
    with pytest.raises(ValueError):
        VerifiedSessionCalendar((date(2026, 1, 6), date(2026, 1, 5)), "source")
    with pytest.raises(ValueError):
        VerifiedSessionCalendar((date(2026, 1, 5), date(2026, 1, 5)), "source")
    with pytest.raises(ValueError):
        VerifiedSessionCalendar(DAILY.session_dates, " ")
