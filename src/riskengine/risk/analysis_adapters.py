"""Pure PreparedAnalysis -> typed response adapters; no I/O or valuation fallback.

All metric formulas delegate to existing financial functions. Chart drawdowns
are a dated presentation of the existing baseline-inclusive wealth/peak ratio.
Legacy functions, preparation records and their callers remain unchanged.
"""
from dataclasses import asdict
from math import isfinite

import numpy as np
import pandas as pd

from riskengine.api.analysis_models import (
    AnalysisMessage, ChartPoint, ChartSeries, ComparisonMetrics, MetricConventions,
    MetricValue, PairedChartSeries, PerformanceMetrics, PerformanceResponse,
    ReturnInterval, RiskMetrics, RiskResponse, ValuationPoint,
)
from riskengine.risk.analysis_models import PreparedAnalysis
from riskengine.risk.metrics import historical_cvar, historical_var
from riskengine.risk.return_intervals import (
    calculate_interval_performance, compare_interval_returns, interval_beta,
)


def _finite(value):
    return value is not None and isfinite(float(value))


def _reason(code, message, scope, metric=None, start=None, end=None):
    return AnalysisMessage(code=code, message=message, scope=scope, metric=metric,
                           start_date=start, end_date=end)


class _Adapter:
    def __init__(self, prepared):
        self.prepared = prepared
        self.reasons = []
        self.warnings = [AnalysisMessage(**asdict(issue)) for issue in prepared.issues]
        self.data = {}
        self.metrics = []
        self.comparison_valid = False
        self.valuations = []
        self.invalid_valuations = False
        for point in prepared.valuations:
            values = asdict(point)
            for field in ("holdings_value", "cash_balance", "total_equity", "net_contributions"):
                if values[field] is not None and not _finite(values[field]):
                    values[field] = None
                    values["complete"] = False
                    self.invalid_valuations = True
                    self.warnings.append(_reason("nonfinite_valuation", "Non-finite prepared valuation suppressed.", "portfolio", start=point.date, end=point.date))
            self.valuations.append(ValuationPoint(**values))
        self._prepare_side("portfolio", prepared.portfolio_intervals, prepared.portfolio_coverage)
        self._prepare_side("benchmark", prepared.benchmark_intervals, prepared.benchmark_coverage)

    def _prepare_side(self, scope, intervals, coverage):
        p = self.prepared
        reasons = []
        if not coverage.calendar_verified:
            reasons.append(_reason("unknown_calendar", "Daily metrics require verified trading-session coverage.", scope))
        if coverage.complete is not True or not coverage.daily_eligible:
            reasons.append(_reason("insufficient_coverage", "The complete requested window is not eligible for daily analytics.", scope))
        if not intervals:
            reasons.append(_reason("empty_returns", "No adjusted return observations are available.", scope))
        if scope == "portfolio" and (self.invalid_valuations or not p.valuations or not all(v.complete for v in p.valuations)):
            reasons.append(_reason("incomplete_valuations", "Portfolio valuations are incomplete.", scope))
        if intervals:
            first, last = intervals[0], intervals[-1]
            if first.start_date != p.period.selected_start_date or last.end_date != p.period.selected_end_date:
                reasons.append(_reason("boundary_mismatch", "Return boundaries do not cover the selected window.", scope))
            if scope == "portfolio" and first.start_date != p.period.effective_baseline_date:
                reasons.append(_reason("unknown_baseline", "The actual portfolio baseline is unavailable or inconsistent.", scope))
        if coverage.interval_count != len(intervals) or coverage.expected_intervals != len(intervals):
            reasons.append(_reason("interval_count_mismatch", "Prepared interval counts do not establish complete coverage.", scope))
        if p.methodology.annualization_sessions != 252:
            reasons.append(_reason("unsupported_annualization", "Existing performance formulas use 252 sessions.", scope))
        metrics = None
        if not reasons:
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                result = calculate_interval_performance(intervals, initial_capital=1.0, risk_free_rate=0.0)
            if result.status == "ok":
                metrics = result.value
            else:
                reasons.extend(_reason(item.code, item.message, scope, start=item.start_date, end=item.end_date) for item in result.reasons)
        self.data[scope] = (intervals, metrics, tuple(reasons))

    def metric(self, scope, name, value=None, reasons=(), unit="fraction"):
        intervals, _, unavailable = self.data[scope if scope != "comparison" else "portfolio"]
        if reasons:
            unavailable = reasons
        if name == "sharpe_ratio" and _finite(value):
            values = self.data[scope][1]
            if values is not None and not _finite(values["annualized_volatility"]):
                unavailable = (_reason("nonfinite_metric", "Sharpe ratio cannot use a non-finite volatility estimate.", scope),)
        if unavailable:
            value = None
        elif not _finite(value):
            code = "zero_volatility" if name == "sharpe_ratio" and value is None else "nonfinite_metric"
            message = "Sharpe ratio is undefined for zero volatility." if code == "zero_volatility" else "Metric is non-finite or unavailable."
            unavailable = (_reason(code, message, scope),)
            value = None
        messages = tuple(item.model_copy(update={"metric": f"{scope}.{name}"}) for item in unavailable)
        self.reasons.extend(messages)
        metric = MetricValue(value=float(value) if value is not None else None, unit=unit,
                             observations=len(intervals), unavailable_reasons=messages)
        self.metrics.append(metric)
        return metric

    def performance(self, scope):
        _, values, _ = self.data[scope]
        values = values or {}
        return PerformanceMetrics(**{
            name: self.metric(scope, name, values.get(key), unit="ratio" if name == "sharpe_ratio" else "fraction")
            for name, key in (("cumulative_return", "cumulative_return"), ("annualized_return", "annualized_return"),
                              ("annualized_volatility", "annualized_volatility"), ("sharpe_ratio", "sharpe_ratio"),
                              ("maximum_drawdown", "max_drawdown"))
        })

    def comparison(self):
        p = self.prepared
        reasons = ()
        value = None
        if not p.comparison_eligible or any(self.data[side][2] for side in ("portfolio", "benchmark")):
            reasons = (_reason("comparison_unavailable", "Both complete series must cover equivalent verified one-session intervals in the same currency.", "comparison"),)
        else:
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                result = compare_interval_returns(p.portfolio_intervals, p.benchmark_intervals, initial_capital=1.0)
            if result.status == "ok":
                value = result.value
                self.comparison_valid = True
            else:
                reasons = tuple(_reason(item.code, item.message, "comparison", start=item.start_date, end=item.end_date) for item in result.reasons)
        return value or {}, reasons

    def risk(self, scope, beta_value=None, beta_reasons=()):
        intervals, values, reasons = self.data[scope]
        values = values or {}
        var = cvar = None
        if not reasons:
            returns = pd.Series([item.return_value for item in intervals], dtype=float)
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                var = historical_var(returns, self.prepared.confidence)
                cvar = historical_cvar(returns, self.prepared.confidence)
        if scope == "benchmark":
            beta_reasons = (_reason("not_applicable", "Beta is reported for the portfolio relative to the selected benchmark, not for the benchmark against itself.", scope),)
        return RiskMetrics(
            annualized_volatility=self.metric(scope, "annualized_volatility", values.get("annualized_volatility")),
            sharpe_ratio=self.metric(scope, "sharpe_ratio", values.get("sharpe_ratio"), unit="ratio"),
            maximum_drawdown=self.metric(scope, "maximum_drawdown", values.get("max_drawdown")),
            beta=self.metric(scope, "beta", beta_value, reasons=beta_reasons, unit="ratio"),
            historical_var=self.metric(scope, "historical_var", var),
            historical_cvar=self.metric(scope, "historical_cvar", cvar),
        )

    def chart(self, scope, drawdown=False):
        intervals, values, reasons = self.data[scope]
        unit = "fraction" if drawdown else "wealth_index"
        if reasons:
            return ChartSeries(unit=unit, unavailable_reasons=reasons)
        # The existing performance engine supplies normalized wealth. This is
        # only its dated presentation, including the known baseline of 1.
        wealth = pd.concat([pd.Series([1.0], index=pd.DatetimeIndex([intervals[0].start_date])), values["equity_curve"]])
        if not all(_finite(value) for value in wealth):
            reason = _reason("nonfinite_chart", "Wealth index is non-finite; the chart is unavailable rather than connecting partial values.", scope)
            self.reasons.append(reason)
            return ChartSeries(unit=unit, unavailable_reasons=(reason,))
        series = -(wealth / wealth.cummax() - 1) if drawdown else wealth
        return ChartSeries(unit=unit, points=tuple(ChartPoint(date=stamp.date(), value=float(value)) for stamp, value in series.items()))

    def common(self):
        p = self.prepared
        count = sum(metric.value is not None for metric in self.metrics)
        # Benchmark self-beta is intentionally not an applicable metric.
        unavailable = any(reason.code != "not_applicable" for reason in self.reasons)
        return dict(
            status="unavailable" if not count else "partial" if unavailable else "ok",
            portfolio_id=p.portfolio_id, name=p.name, currency=p.currency,
            benchmark_ticker=p.benchmark_ticker, benchmark_currency=p.benchmark_currency,
            confidence=p.confidence, period=p.period, portfolio_coverage=p.portfolio_coverage,
            benchmark_coverage=p.benchmark_coverage, comparison_eligible=self.comparison_valid,
            methodology=p.methodology, metric_conventions=MetricConventions(),
            valuations=tuple(self.valuations),
            portfolio_intervals=tuple(ReturnInterval(**asdict(item)) for item in p.portfolio_intervals),
            benchmark_intervals=tuple(ReturnInterval(**asdict(item)) for item in p.benchmark_intervals),
            warnings=tuple(self.warnings), unavailable_reasons=tuple(self.reasons),
        )


def build_performance_response(prepared: PreparedAnalysis) -> PerformanceResponse:
    adapter = _Adapter(prepared)
    portfolio, benchmark = adapter.performance("portfolio"), adapter.performance("benchmark")
    comparison, reasons = adapter.comparison()
    metrics = ComparisonMetrics(**{name: adapter.metric("comparison", name, comparison.get(name), reasons=reasons)
                                  for name in ("excess_cumulative_return", "excess_annualized_return")})
    charts = PairedChartSeries(portfolio=adapter.chart("portfolio"), benchmark=adapter.chart("benchmark"))
    return PerformanceResponse(**adapter.common(), portfolio_metrics=portfolio, benchmark_metrics=benchmark,
                               comparison=metrics, wealth_index=charts)


def build_risk_response(prepared: PreparedAnalysis) -> RiskResponse:
    adapter = _Adapter(prepared)
    _, reasons = adapter.comparison()
    beta = None
    if not reasons:
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            result = interval_beta(prepared.portfolio_intervals, prepared.benchmark_intervals)
        beta = result.value
        reasons = tuple(_reason(item.code, item.message, "comparison", start=item.start_date, end=item.end_date) for item in result.reasons)
    portfolio = adapter.risk("portfolio", beta, reasons)
    benchmark = adapter.risk("benchmark")
    charts = PairedChartSeries(portfolio=adapter.chart("portfolio", True), benchmark=adapter.chart("benchmark", True))
    return RiskResponse(**adapter.common(), portfolio_metrics=portfolio, benchmark_metrics=benchmark, drawdown=charts)
