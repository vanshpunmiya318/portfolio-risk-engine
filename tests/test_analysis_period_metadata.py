from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from test_golden_portfolio import golden_db
from test_benchmark import golden_spy_db
from riskengine.risk.performance import compare_portfolio_to_benchmark
from riskengine.risk.summary import get_portfolio_risk_summary
from riskengine.risk.portfolio_report import get_portfolio_risk_report
from riskengine.risk.return_intervals import (
    ReturnObservation, compare_interval_returns, calculate_interval_performance, interval_beta,
)


def returns():
    return pd.Series([.01, -.02, .03], index=pd.to_datetime(["2026-01-06", "2026-01-07", "2026-01-08"]))


def observations():
    days = [date(2026, 1, day) for day in (5, 6, 7, 8)]
    return tuple(ReturnObservation(a, b, value, 1) for a, b, value in zip(days, days[1:], returns()))


def test_legacy_comparison_keeps_start_type_and_never_guesses_baseline():
    data = returns()
    result = compare_portfolio_to_benchmark(data, data, requested_start_date="2026-01-03")
    assert result["start_date"] == result["first_return_end_date"] == "2026-01-06"
    assert result["requested_start_date"] == "2026-01-03"
    assert result["effective_baseline_date"] is None
    assert result["end_date"] == result["effective_end_date"] == "2026-01-08"
    assert result["period_metadata_reason"]
    assert result["session_coverage_verified"] is False


def test_new_comparison_exposes_baseline_separately_from_requested_and_return_dates():
    data = observations()
    result = compare_interval_returns(data, data, requested_start_date=date(2026, 1, 3))
    assert result.status == "ok"
    assert result.start_date == result.baseline_date == result.effective_baseline_date == date(2026, 1, 5)
    assert result.requested_start_date == date(2026, 1, 3)
    assert result.first_return_end_date == date(2026, 1, 6)
    assert result.end_date == result.effective_end_date == date(2026, 1, 8)
    assert result.value["start_date"] == "2026-01-05"
    assert result.value["requested_start_date"] == "2026-01-03"
    assert result.value["first_return_end_date"] == "2026-01-06"
    assert result.session_coverage_verified is True
    assert result.period_metadata_reason is None


def test_unknown_baseline_is_null_with_reason_even_when_request_is_known():
    data = observations()
    ambiguous = (replace(data[0], start_date=None), *data[1:])
    result = compare_interval_returns(ambiguous, data, requested_start_date=date(2026, 1, 5))
    assert result.status == "insufficient_coverage"
    assert result.start_date is None
    assert result.effective_baseline_date is None
    assert result.period_metadata_reason
    assert result.first_return_end_date == date(2026, 1, 6)
    assert any(reason.code == "unknown_start" for reason in result.reasons)


def test_different_known_baselines_do_not_become_a_fabricated_common_baseline():
    data = observations()
    other = (replace(data[0], start_date=date(2026, 1, 2)), *data[1:])
    result = compare_interval_returns(data, other)
    assert result.status == "insufficient_coverage"
    assert result.start_date is None
    assert result.period_metadata_reason


def test_empty_interval_results_have_no_fabricated_dates():
    for result in (compare_interval_returns((), ()), calculate_interval_performance(()), interval_beta((), ())):
        assert result.status == "insufficient_coverage"
        assert result.start_date is None
        assert result.first_return_end_date is None
        assert result.effective_end_date is None
        assert result.period_metadata_reason


def test_empty_legacy_comparison_preserves_explicit_error():
    empty = pd.Series([], index=pd.DatetimeIndex([]), dtype=float)
    with pytest.raises(ValueError, match="Insufficient overlapping"):
        compare_portfolio_to_benchmark(empty, empty)


def test_nonconsecutive_legacy_end_dates_do_not_imply_an_earlier_baseline():
    data = returns().iloc[[0, 2]]
    result = compare_portfolio_to_benchmark(data, data)
    assert result["start_date"] == "2026-01-06"
    assert result["effective_baseline_date"] is None
    assert result["effective_end_date"] == "2026-01-08"
    assert result["session_coverage_verified"] is False


def test_gapped_intervals_keep_known_boundaries_without_claiming_valid_coverage():
    data = observations()
    gap = (data[0], data[2])
    result = compare_interval_returns(gap, gap)
    assert result.status == "insufficient_coverage"
    assert result.start_date == date(2026, 1, 5)
    assert result.first_return_end_date == date(2026, 1, 6)
    assert result.effective_end_date == date(2026, 1, 8)
    assert result.session_coverage_verified is False
    assert any(reason.code == "coverage_gap" for reason in result.reasons)


def test_summary_uses_first_valuation_even_when_dates_are_unsorted(golden_db):
    connection, _, pid = golden_db
    dates = pd.to_datetime(["2026-01-09", "2026-01-05", "2026-01-07", "2026-01-06", "2026-01-08"])
    result = get_portfolio_risk_summary(connection, pid, dates, requested_start_date="2026-01-03")
    assert result["start_date"] == result["effective_baseline_date"] == "2026-01-05"
    assert result["first_return_end_date"] == "2026-01-06"
    assert result["requested_start_date"] == "2026-01-03"
    assert result["effective_end_date"] == "2026-01-09"


def test_summary_nonconsecutive_boundaries_are_actual_not_inferred(golden_db):
    connection, _, pid = golden_db
    result = get_portfolio_risk_summary(connection, pid, pd.to_datetime(["2026-01-05", "2026-01-07", "2026-01-09"]))
    assert result["start_date"] == "2026-01-05"
    assert result["first_return_end_date"] == "2026-01-07"
    assert result["end_date"] == "2026-01-09"
    assert result["session_coverage_verified"] is False
    assert result["period_metadata_reason"]


@pytest.mark.parametrize("dates", [[], ["2026-01-05"], [None, "2026-01-06", "2026-01-07"]])
def test_summary_missing_valuation_boundaries_remain_explicit_errors(golden_db, dates):
    connection, _, pid = golden_db
    with pytest.raises(ValueError):
        get_portfolio_risk_summary(connection, pid, dates)


def test_unexpected_empty_calculated_returns_do_not_invent_period(golden_db, monkeypatch):
    from riskengine.risk import summary
    connection, _, pid = golden_db
    monkeypatch.setattr(summary, "get_portfolio_daily_returns", lambda **kwargs: pd.Series([], dtype=float))
    with pytest.raises(ValueError, match="analysis period is undefined"):
        summary.get_portfolio_risk_summary(connection, pid, pd.date_range("2026-01-05", "2026-01-09"))


def test_report_passes_requested_start_and_preserves_financial_values(golden_spy_db):
    connection, pid = golden_spy_db
    result = get_portfolio_risk_report(connection, pid, "2026-01-05", "2026-01-09")
    assert result["start_date"] == result["requested_start_date"] == "2026-01-05"
    assert result["first_return_end_date"] == "2026-01-06"
    assert result["observations"] == 4
