from dataclasses import replace
from datetime import date
import json
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from test_golden_portfolio import golden_db, cash_flow_golden_db
from test_benchmark import golden_spy_db
from riskengine.api.analysis_models import MetricValue, PerformanceResponse, RiskResponse
from riskengine.risk.analysis_adapters import build_performance_response, build_risk_response
from riskengine.risk.analysis_service import prepare_analysis
from riskengine.risk.metrics import historical_var, historical_cvar, portfolio_beta
from riskengine.risk.performance import calculate_performance_metrics, compare_portfolio_to_benchmark
from riskengine.risk.return_intervals import (
    VerifiedSessionCalendar, calculate_interval_performance, compare_interval_returns,
)


CALENDAR = VerifiedSessionCalendar(tuple(date(2026, 1, day) for day in range(5, 10)), "Explicit synthetic test sessions")


def prepare(c, pid, **kwargs):
    if not c.in_transaction:
        c.execute("BEGIN")
    return prepare_analysis(c, pid, **kwargs)


@pytest.fixture
def verified(golden_spy_db):
    c, pid = golden_spy_db
    return prepare(c, pid, session_calendar=CALENDAR)


def series(intervals):
    return pd.Series([item.return_value for item in intervals], index=pd.DatetimeIndex([item.end_date for item in intervals]))


def check_json(response):
    dumped = response.model_dump(mode="json")
    encoded = json.dumps(dumped, allow_nan=False)
    assert "NaN" not in encoded and "Infinity" not in encoded
    assert type(response).model_validate_json(encoded) == response


def test_complete_metric_and_chart_parity(verified):
    performance = build_performance_response(verified)
    risk = build_risk_response(verified)
    assert isinstance(performance, PerformanceResponse)
    assert isinstance(risk, RiskResponse)
    assert performance.status == risk.status == "ok"
    p = series(verified.portfolio_intervals)
    b = series(verified.benchmark_intervals)
    for scope, returns in (("portfolio", p), ("benchmark", b)):
        expected = calculate_performance_metrics(returns, initial_capital=1.)
        actual = getattr(performance, scope + "_metrics")
        assert actual.cumulative_return.value == expected["cumulative_return"]
        assert actual.annualized_return.value == expected["annualized_return"]
        assert actual.annualized_volatility.value == expected["annualized_volatility"]
        assert actual.sharpe_ratio.value == expected["sharpe_ratio"]
        assert actual.maximum_drawdown.value == expected["max_drawdown"]
        actual_risk = getattr(risk, scope + "_metrics")
        assert actual_risk.historical_var.value == historical_var(returns)
        assert actual_risk.historical_cvar.value == historical_cvar(returns)
        points = getattr(performance.wealth_index, scope).points
        assert points[0].date == verified.period.effective_baseline_date
        assert points[0].value == 1
        assert [point.value for point in points[1:]] == expected["equity_curve"].tolist()
        drawdown = getattr(risk.drawdown, scope).points
        assert drawdown[0].value == 0
        assert max(point.value for point in drawdown) == actual.maximum_drawdown.value
        assert [point.date for point in drawdown] == [point.date for point in points]
    assert risk.portfolio_metrics.beta.value == portfolio_beta(p, b)
    assert risk.portfolio_metrics.beta.unit == "ratio"
    assert performance.portfolio_metrics.cumulative_return.unit == "fraction"
    assert performance.valuations[0].total_equity == verified.valuations[0].total_equity
    assert performance.comparison.excess_cumulative_return.value == calculate_performance_metrics(p)["cumulative_return"] - calculate_performance_metrics(b)["cumulative_return"]
    check_json(performance)
    check_json(risk)


def test_unknown_calendar_suppresses_all_metrics_and_charts(golden_spy_db):
    c, pid = golden_spy_db
    data = prepare(c, pid)
    for response in (build_performance_response(data), build_risk_response(data)):
        assert response.status == "unavailable"
        assert response.portfolio_metrics.annualized_volatility.value is None
        assert response.portfolio_metrics.maximum_drawdown.value is None
        assert response.portfolio_metrics.sharpe_ratio.unavailable_reasons
        assert "unknown_calendar" in {r.code for r in response.unavailable_reasons}
        assert response.portfolio_intervals  # Observations remain visible, not certified.
        check_json(response)
    assert not build_performance_response(data).wealth_index.portfolio.points
    assert not build_risk_response(data).drawdown.portfolio.points


def test_missing_benchmark_retains_portfolio_metrics(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices WHERE instrument_id=(SELECT id FROM instruments WHERE ticker='SPY')")
    data = prepare(c, pid, session_calendar=CALENDAR)
    performance, risk = build_performance_response(data), build_risk_response(data)
    assert performance.status == risk.status == "partial"
    assert performance.portfolio_metrics.cumulative_return.value is not None
    assert risk.portfolio_metrics.historical_cvar.value is not None
    assert performance.benchmark_metrics.cumulative_return.value is None
    assert performance.comparison.excess_cumulative_return.value is None
    assert risk.portfolio_metrics.beta.value is None
    assert risk.portfolio_metrics.beta.unavailable_reasons
    assert len(performance.wealth_index.portfolio.points) == 5
    assert not performance.wealth_index.benchmark.points
    assert "benchmark_unavailable" in {w.code for w in risk.warnings}
    check_json(risk)


def test_missing_benchmark_date_cannot_compare_end_date_only(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices WHERE date='2026-01-06' AND instrument_id=(SELECT id FROM instruments WHERE ticker='SPY')")
    data = prepare(c, pid, session_calendar=CALENDAR)
    response = build_performance_response(data)
    assert response.portfolio_metrics.cumulative_return.value is not None
    assert response.benchmark_metrics.cumulative_return.value is None
    assert response.comparison.excess_annualized_return.value is None
    assert not response.wealth_index.benchmark.points
    assert "unmatched_interval" in {w.code for w in response.warnings}


def test_zero_volatility_and_undefined_beta(verified):
    zero = tuple(replace(item, return_value=0.) for item in verified.portfolio_intervals)
    data = replace(verified, portfolio_intervals=zero, benchmark_intervals=zero)
    p, r = build_performance_response(data), build_risk_response(data)
    assert p.portfolio_metrics.cumulative_return.value == 0
    assert p.portfolio_metrics.annualized_volatility.value == 0
    assert p.portfolio_metrics.sharpe_ratio.value is None
    assert p.portfolio_metrics.sharpe_ratio.unavailable_reasons[0].code == "zero_volatility"
    assert r.portfolio_metrics.beta.value is None
    assert r.portfolio_metrics.beta.unavailable_reasons[0].code == "zero_benchmark_variance"
    assert r.portfolio_metrics.historical_var.value == 0
    assert all(point.value == 1 for point in p.wealth_index.portfolio.points)
    assert all(point.value == 0 for point in r.drawdown.portfolio.points)
    check_json(p)
    check_json(r)


def test_fractional_tail_cvar_uses_existing_method_not_threshold_subset(verified):
    returns = [-.10, -.02, .01, .03]
    observations = tuple(replace(item, return_value=value) for item, value in zip(verified.portfolio_intervals, returns))
    data = replace(verified, confidence=.625, portfolio_intervals=observations)
    r = build_risk_response(data)
    assert r.portfolio_metrics.historical_cvar.value == historical_cvar(pd.Series(returns), .625)
    assert r.portfolio_metrics.historical_cvar.value == pytest.approx((.10 + .5 * .02) / 1.5)
    assert r.portfolio_metrics.historical_var.value == historical_var(pd.Series(returns), .625)


def test_deposit_only_increases_are_not_investment_returns(golden_spy_db):
    c, pid = golden_spy_db
    c.execute("DELETE FROM transactions WHERE portfolio_id=?", (pid,))
    for day in (6, 7, 8, 9):
        c.execute("INSERT INTO cash_flows(portfolio_id,date,flow_type,amount,currency,source) VALUES (?,?,'DEPOSIT',1000,'USD','test')", (pid, f"2026-01-{day:02d}"))
    data = prepare(c, pid, start_date="2026-01-05", end_date="2026-01-09", session_calendar=CALENDAR)
    p, r = build_performance_response(data), build_risk_response(data)
    assert p.valuations[-1].total_equity > p.valuations[0].total_equity
    assert p.portfolio_metrics.cumulative_return.value == 0
    assert r.portfolio_metrics.annualized_volatility.value == 0
    assert r.portfolio_metrics.maximum_drawdown.value == 0
    assert r.portfolio_metrics.sharpe_ratio.value is None


@pytest.mark.parametrize("day", ["2026-01-05", "2026-01-07", "2026-01-09"])
def test_missing_valuations_never_bridge_chart_gaps(golden_spy_db, day):
    c, pid = golden_spy_db
    c.execute("DELETE FROM prices WHERE date=? AND instrument_id=(SELECT id FROM instruments WHERE ticker='AAPL')", (day,))
    data = prepare(c, pid, session_calendar=CALENDAR)
    p, r = build_performance_response(data), build_risk_response(data)
    assert not p.wealth_index.portfolio.points
    assert not r.drawdown.portfolio.points
    assert p.wealth_index.portfolio.unavailable_reasons
    assert p.portfolio_metrics.cumulative_return.value is None
    assert any(v.total_equity is None and not v.complete for v in p.valuations)
    assert p.benchmark_metrics.cumulative_return.value is not None
    assert r.portfolio_metrics.beta.value is None
    check_json(p)


def test_empty_portfolio_has_null_metrics_and_no_fabricated_baseline(golden_spy_db):
    c, _ = golden_spy_db
    pid = c.execute("INSERT INTO portfolios(name,currency) VALUES ('Empty Stage 2','USD')").lastrowid
    data = prepare(c, pid)
    p, r = build_performance_response(data), build_risk_response(data)
    assert p.status == r.status == "unavailable"
    assert p.period.effective_baseline_date is None
    assert not p.wealth_index.portfolio.points
    assert not p.valuations and not p.portfolio_intervals
    assert r.portfolio_metrics.historical_cvar.value is None
    check_json(p)
    check_json(r)


def test_interval_validator_defends_against_inconsistent_eligibility_flags(verified):
    ambiguous = tuple(replace(item, trading_sessions=None) for item in verified.portfolio_intervals)
    data = replace(verified, portfolio_intervals=ambiguous)
    result = build_risk_response(data)
    assert result.portfolio_metrics.annualized_volatility.value is None
    assert "unverified_sessions" in {reason.code for reason in result.unavailable_reasons}
    assert result.portfolio_metrics.beta.value is None


def test_adapter_has_no_database_dependency_or_reconstruction(golden_spy_db, monkeypatch):
    c, pid = golden_spy_db
    data = prepare(c, pid, session_calendar=CALENDAR)
    from riskengine.risk import analysis_service
    monkeypatch.setattr(analysis_service, "prepare_analysis", lambda *a, **k: pytest.fail("Preparation must not be repeated"))
    c.close()
    assert build_performance_response(data).portfolio_metrics.cumulative_return.value is not None
    assert build_risk_response(data).portfolio_metrics.beta.value is not None


def test_drawdown_includes_baseline_loss_with_actual_dates(verified):
    observations = tuple(replace(item, return_value=value) for item, value in zip(verified.portfolio_intervals, [-.1, .02, -.2, .01]))
    data = replace(verified, portfolio_intervals=observations)
    response = build_risk_response(data)
    assert response.drawdown.portfolio.points[0].date == date(2026, 1, 5)
    assert response.drawdown.portfolio.points[1].date == date(2026, 1, 6)
    assert response.drawdown.portfolio.points[1].value == pytest.approx(.1)
    assert response.portfolio_metrics.maximum_drawdown.value == max(p.value for p in response.drawdown.portfolio.points)


def test_nonfinite_metric_and_chart_are_null_with_reasons(verified):
    extreme = tuple(replace(item, return_value=1e200) for item in verified.portfolio_intervals)
    response = build_performance_response(replace(verified, portfolio_intervals=extreme))
    assert response.portfolio_metrics.cumulative_return.value is None
    assert response.portfolio_metrics.annualized_return.value is None
    assert response.portfolio_metrics.sharpe_ratio.value is None
    assert not response.wealth_index.portfolio.points
    assert response.wealth_index.portfolio.unavailable_reasons[0].code == "nonfinite_chart"
    check_json(response)
    check_json(build_risk_response(replace(verified, portfolio_intervals=extreme)))


def test_nonfinite_prepared_valuation_cannot_reach_json(verified):
    points = (replace(verified.valuations[0], total_equity=float("nan")), *verified.valuations[1:])
    response = build_performance_response(replace(verified, valuations=points))
    assert response.valuations[0].total_equity is None
    assert not response.valuations[0].complete
    assert response.portfolio_metrics.cumulative_return.value is None
    check_json(response)


def test_response_models_enforce_explicit_null_reasons_and_finite_values():
    with pytest.raises(ValidationError):
        MetricValue(value=None, observations=0)
    with pytest.raises(ValidationError):
        MetricValue(value=float("inf"), observations=2)


def test_known_calendar_flag_cannot_infer_missing_baseline(verified):
    observations = (replace(verified.portfolio_intervals[0], start_date=None), *verified.portfolio_intervals[1:])
    result = build_performance_response(replace(verified, portfolio_intervals=observations))
    assert result.portfolio_metrics.cumulative_return.value is None
    assert not result.wealth_index.portfolio.points
    assert "unknown_baseline" in {reason.code for reason in result.unavailable_reasons}


def test_removed_intervals_cannot_be_connected_even_if_daily_flag_is_stale(verified):
    observations = (verified.portfolio_intervals[0], verified.portfolio_intervals[2], verified.portfolio_intervals[3])
    coverage = replace(verified.portfolio_coverage, interval_count=3, expected_intervals=3)
    data = replace(verified, portfolio_intervals=observations, portfolio_coverage=coverage)
    result = build_risk_response(data)
    assert result.portfolio_metrics.annualized_volatility.value is None
    assert not result.drawdown.portfolio.points
    assert "coverage_gap" in {reason.code for reason in result.unavailable_reasons}


def test_historical_window_metric_parity_with_explicit_test_sessions():
    from riskengine.db.connection import get_connection
    path = Path(__file__).resolve().parents[1] / "data" / "historical_portfolio.db"
    if not path.is_file():
        pytest.skip("Historical database is not present")
    # Independently enumerated test sessions, never built from SPY observations.
    # This small fixture does not install or supply a production calendar.
    calendar = VerifiedSessionCalendar(tuple(date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9, 10)),
                                       "Explicit January 2024 test-session fixture; no production calendar provider")
    with closing(get_connection(path, read_only=True)) as c:
        c.execute("BEGIN")
        prepared = prepare_analysis(c, 1, start_date="2024-01-02", end_date="2024-01-10", session_calendar=calendar)
    p, r = build_performance_response(prepared), build_risk_response(prepared)
    assert p.comparison_eligible
    for scope, intervals in (("portfolio", prepared.portfolio_intervals), ("benchmark", prepared.benchmark_intervals)):
        returns = series(intervals)
        old = calculate_performance_metrics(returns, initial_capital=1.)
        metrics = getattr(p, scope + "_metrics")
        for target, source in (("cumulative_return", "cumulative_return"), ("annualized_return", "annualized_return"),
                               ("annualized_volatility", "annualized_volatility"), ("sharpe_ratio", "sharpe_ratio"),
                               ("maximum_drawdown", "max_drawdown")):
            assert getattr(metrics, target).value == old[source]
        risk = getattr(r, scope + "_metrics")
        assert risk.historical_var.value == historical_var(returns, prepared.confidence)
        assert risk.historical_cvar.value == historical_cvar(returns, prepared.confidence)
    assert r.portfolio_metrics.beta.value == portfolio_beta(series(prepared.portfolio_intervals), series(prepared.benchmark_intervals))
    check_json(p)
    check_json(r)


def sampled_window(prepared, count):
    """A complete PreparedAnalysis fixture with real baseline/interval boundaries."""
    valuations = prepared.valuations[:count + 1]
    end = valuations[-1].date
    period = replace(prepared.period, selected_end_date=end, effective_end_date=end,
                     first_return_end_date=valuations[1].date if count else None)

    def coverage(value):
        return replace(value, expected_sessions=count + 1, expected_intervals=count,
                       valuation_count=count + 1, valid_valuation_count=count + 1,
                       interval_count=count, complete=True, daily_eligible=count >= 2)

    return replace(prepared, period=period, valuations=valuations,
                   portfolio_intervals=prepared.portfolio_intervals[:count],
                   benchmark_intervals=prepared.benchmark_intervals[:count],
                   portfolio_coverage=coverage(prepared.portfolio_coverage),
                   benchmark_coverage=coverage(prepared.benchmark_coverage), comparison_eligible=count >= 2)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_metric_specific_zero_one_two_observation_minimums(verified, count):
    data = sampled_window(verified, count)
    p, r = build_performance_response(data), build_risk_response(data)
    if count == 0:
        assert p.portfolio_metrics.cumulative_return.value is None
        assert p.portfolio_metrics.annualized_return.value is None
        assert r.portfolio_metrics.maximum_drawdown.value is None
        assert r.portfolio_metrics.historical_var.value is None
        assert r.portfolio_metrics.historical_cvar.value is None
        assert not p.wealth_index.portfolio.points
        assert "insufficient_observations" in {reason.code for reason in p.portfolio_metrics.cumulative_return.unavailable_reasons}
    else:
        returns = series(data.portfolio_intervals)
        expected = calculate_performance_metrics(returns, initial_capital=1., minimum_observations=1)
        assert p.portfolio_metrics.cumulative_return.value == expected["cumulative_return"]
        assert p.portfolio_metrics.annualized_return.value == expected["annualized_return"]
        assert r.portfolio_metrics.maximum_drawdown.value == expected["max_drawdown"]
        assert r.portfolio_metrics.historical_var.value == historical_var(returns)
        assert r.portfolio_metrics.historical_cvar.value == historical_cvar(returns)
        assert len(p.wealth_index.portfolio.points) == count + 1
        assert p.wealth_index.portfolio.points[0].date == data.period.effective_baseline_date
        assert p.wealth_index.portfolio.points[0].value == 1
        assert p.comparison.excess_cumulative_return.value is not None
    if count < 2:
        for metric in (r.portfolio_metrics.annualized_volatility, r.portfolio_metrics.sharpe_ratio, r.portfolio_metrics.beta):
            assert metric.value is None
            assert "insufficient_observations" in {reason.code for reason in metric.unavailable_reasons}
    else:
        old = calculate_performance_metrics(series(data.portfolio_intervals), initial_capital=1.)
        assert r.portfolio_metrics.annualized_volatility.value == old["annualized_volatility"]
        assert r.portfolio_metrics.sharpe_ratio.value == old["sharpe_ratio"]
        assert r.portfolio_metrics.beta.value == portfolio_beta(series(data.portfolio_intervals), series(data.benchmark_intervals))
    check_json(p)
    check_json(r)


@pytest.mark.parametrize("value", [-.0077069, 0., .01, -1.])
def test_single_interval_growth_drawdown_and_tail_behavior(verified, value):
    data = sampled_window(verified, 1)
    data = replace(data, portfolio_intervals=(replace(data.portfolio_intervals[0], return_value=value),))
    p, r = build_performance_response(data), build_risk_response(data)
    assert p.portfolio_metrics.cumulative_return.value == pytest.approx(value)
    assert p.portfolio_metrics.annualized_return.value == (1 + p.portfolio_metrics.cumulative_return.value) ** 252 - 1
    assert r.portfolio_metrics.maximum_drawdown.value == pytest.approx(max(0, -value))
    assert r.portfolio_metrics.historical_var.value == -value
    assert r.portfolio_metrics.historical_cvar.value == -value
    assert r.drawdown.portfolio.points[0].value == 0
    assert r.drawdown.portfolio.points[-1].value == r.portfolio_metrics.maximum_drawdown.value
    assert r.portfolio_metrics.sharpe_ratio.unavailable_reasons[0].code == "insufficient_observations"
    check_json(p)
    check_json(r)


@pytest.mark.parametrize("defect", ["calendar", "sessions", "complete", "baseline", "multi_session"])
def test_single_interval_path_never_relaxes_coverage(verified, defect):
    data = sampled_window(verified, 1)
    if defect == "calendar":
        data = replace(data, portfolio_coverage=replace(data.portfolio_coverage, calendar_verified=False))
    elif defect == "sessions":
        data = replace(data, portfolio_intervals=(replace(data.portfolio_intervals[0], trading_sessions=None),))
    elif defect == "complete":
        data = replace(data, portfolio_coverage=replace(data.portfolio_coverage, complete=False))
    elif defect == "baseline":
        data = replace(data, portfolio_intervals=(replace(data.portfolio_intervals[0], start_date=None),))
    else:
        data = replace(data, portfolio_intervals=(replace(data.portfolio_intervals[0], trading_sessions=2),))
    response = build_performance_response(data)
    assert response.portfolio_metrics.cumulative_return.value is None
    assert response.portfolio_metrics.annualized_return.value is None
    assert not response.wealth_index.portfolio.points
    assert response.comparison.excess_cumulative_return.value is None


def test_legacy_primitive_minimums_remain_two_and_opt_in_does_not_emit_nan(verified):
    data = sampled_window(verified, 1)
    returns = series(data.portfolio_intervals)
    with pytest.raises(ValueError, match="At least two"):
        calculate_performance_metrics(returns)
    with pytest.raises(ValueError, match="Insufficient overlapping"):
        compare_portfolio_to_benchmark(returns, returns)
    assert calculate_interval_performance(data.portfolio_intervals).status == "insufficient_coverage"
    assert compare_interval_returns(data.portfolio_intervals, data.benchmark_intervals).status == "insufficient_coverage"
    result = calculate_interval_performance(data.portfolio_intervals, minimum_observations=1)
    assert result.status == "ok"
    assert result.value["annualized_volatility"] is None
    assert result.value["sharpe_ratio"] is None
    assert compare_interval_returns(data.portfolio_intervals, data.benchmark_intervals, minimum_observations=1).status == "ok"


def test_historical_single_interval_minus_point_77069_percent_case():
    from riskengine.db.connection import get_connection
    path = Path(__file__).resolve().parents[1] / "data" / "historical_portfolio.db"
    if not path.is_file():
        pytest.skip("Historical database is not present")
    calendar = VerifiedSessionCalendar((date(2024, 1, 2), date(2024, 1, 3)), "Explicit two-session historical test fixture")
    with closing(get_connection(path, read_only=True)) as connection:
        connection.execute("BEGIN")
        data = prepare_analysis(connection, 1, start_date="2024-01-02", end_date="2024-01-03", session_calendar=calendar)
    response = build_performance_response(data)
    assert data.portfolio_coverage.complete and not data.portfolio_coverage.daily_eligible
    assert response.portfolio_metrics.cumulative_return.value == pytest.approx(-.0077069)
    assert response.portfolio_metrics.annualized_return.value == (1 + data.portfolio_intervals[0].return_value) ** 252 - 1
    assert response.portfolio_metrics.annualized_volatility.value is None
    assert response.portfolio_metrics.sharpe_ratio.value is None
    assert response.period.effective_baseline_date == date(2024, 1, 2)
    assert [p.date for p in response.wealth_index.portfolio.points] == [date(2024, 1, 2), date(2024, 1, 3)]
    # The existing benchmark producer still requires three price observations.
    assert response.benchmark_metrics.cumulative_return.value is None
    check_json(response)
