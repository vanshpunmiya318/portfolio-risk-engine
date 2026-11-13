"""Opt-in interval-aware returns. Legacy Series functions are unchanged.

Session counts are verified facts supplied by the caller, never estimates from
calendar-day differences. A calendar's source must attest that its session list
is complete between its endpoints; this module cannot independently certify it.
End-date-only Series cannot establish actual starts, even for later observations
after filtering. Use explicit starts or one of the connection-based producers.
"""
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from typing import Generic, Iterable, Literal, Sequence, TypeVar

import pandas as pd

from riskengine.db.prices import get_prices
from riskengine.risk.benchmark import get_benchmark_price_returns
from riskengine.risk.metrics import portfolio_beta
from riskengine.risk.performance import (
    calculate_performance_metrics,
    compare_portfolio_to_benchmark,
    analysis_period_metadata,
)
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns


def _date(value) -> date:
    if isinstance(value, str):
        return date.fromisoformat(value)
    if not isinstance(value, date):
        raise ValueError("An interval boundary must be a calendar date")
    if value is None or pd.isna(value):
        raise ValueError("An interval boundary must be a calendar date")
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None or stamp != stamp.normalize():
        raise ValueError("Interval boundaries must be timezone-free calendar dates")
    return stamp.date()


@dataclass(frozen=True)
class ReturnObservation:
    start_date: date | None
    end_date: date
    return_value: float
    trading_sessions: int | None = None

    def __post_init__(self):
        if self.start_date is not None:
            object.__setattr__(self, "start_date", _date(self.start_date))
        object.__setattr__(self, "end_date", _date(self.end_date))
        if self.start_date is not None and self.start_date >= self.end_date:
            raise ValueError("A return interval must have start_date < end_date")
        if isinstance(self.return_value, bool) or not isfinite(self.return_value) or self.return_value < -1:
            raise ValueError("Simple returns must be finite and at least -100%")
        if self.trading_sessions is not None and (
            isinstance(self.trading_sessions, bool)
            or not isinstance(self.trading_sessions, int)
            or self.trading_sessions < 0
        ):
            raise ValueError("Verified trading-session count must be a nonnegative integer")


@dataclass(frozen=True)
class VerifiedSessionCalendar:
    session_dates: tuple[date, ...]
    source: str

    def __post_init__(self):
        sessions = tuple(_date(day) for day in self.session_dates)
        if not sessions or tuple(sorted(set(sessions))) != sessions:
            raise ValueError("Verified sessions must be nonempty, unique and sorted")
        if not self.source.strip():
            raise ValueError("A verified calendar requires explicit source/provenance")
        object.__setattr__(self, "session_dates", sessions)

    def count(self, start: date | None, end: date) -> int | None:
        if start is None:
            return None
        left = bisect_left(self.session_dates, start)
        right = bisect_left(self.session_dates, end)
        if (left >= len(self.session_dates) or right >= len(self.session_dates)
                or self.session_dates[left] != start or self.session_dates[right] != end):
            return None  # Unknown boundary or outside the verified calendar.
        return right - left


@dataclass(frozen=True)
class CoverageIssue:
    code: str
    message: str
    series: str
    start_date: date | None = None
    end_date: date | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class IntervalResult(Generic[T]):
    status: Literal["ok", "insufficient_coverage", "undefined_metric"]
    value: T | None = None
    reasons: tuple[CoverageIssue, ...] = ()
    baseline_date: date | None = None
    end_date: date | None = None
    observations: int = 0
    requested_start_date: date | None = None
    first_return_end_date: date | None = None
    period_metadata_reason: str | None = None
    session_coverage_verified: bool = False
    start_date: date | None = field(init=False)
    effective_baseline_date: date | None = field(init=False)
    effective_end_date: date | None = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "start_date", self.baseline_date)
        object.__setattr__(self, "effective_baseline_date", self.baseline_date)
        object.__setattr__(self, "effective_end_date", self.end_date)


def observations_from_series(
    returns: pd.Series,
    *,
    start_dates: Sequence[date | None] | None = None,
    calendar: VerifiedSessionCalendar | None = None,
) -> tuple[ReturnObservation, ...]:
    """Attach explicit starts in original Series order; never guess a baseline.

    Without starts, every interval remains unknown and daily consumers reject
    it. Supplying only dates does not verify session counts without a calendar.
    """
    if start_dates is not None and len(start_dates) != len(returns):
        raise ValueError("Provide one explicit start for every return observation")
    result = []
    for index, (ending, value) in enumerate(returns.items()):
        start = None if start_dates is None or start_dates[index] is None else _date(start_dates[index])
        end = _date(ending)
        result.append(ReturnObservation(start, end, float(value), calendar.count(start, end) if calendar else None))
    return tuple(result)


def _from_boundaries(returns, boundaries, calendar):
    dates = pd.DatetimeIndex(pd.to_datetime(boundaries)).sort_values()
    if dates.hasnans or dates.has_duplicates or len(dates) != len(returns) + 1:
        raise ValueError("Valuation boundaries must uniquely describe every return interval")
    if not pd.DatetimeIndex(returns.index).equals(dates[1:]):
        raise ValueError("Return ending dates do not match valuation boundaries")
    return observations_from_series(returns, start_dates=list(dates[:-1]), calendar=calendar)


def get_portfolio_return_observations(
    connection, portfolio_id: int, dates, max_age_days: int = 0,
    *, calendar: VerifiedSessionCalendar | None = None,
) -> tuple[ReturnObservation, ...]:
    """Reuse cash-flow-aware calculations and retain their actual boundaries.

    Caller owns the connection and consistent read snapshot.
    """
    boundaries = pd.DatetimeIndex(pd.to_datetime(dates)).sort_values()
    returns = get_portfolio_daily_returns(connection, portfolio_id, boundaries, max_age_days)
    return _from_boundaries(returns, boundaries, calendar)


def get_benchmark_return_observations(
    connection, ticker: str, start_date: str, end_date: str,
    *, calendar: VerifiedSessionCalendar | None = None,
) -> tuple[ReturnObservation, ...]:
    """Reuse raw-close benchmark returns, preserving gaps as longer intervals.

    Stored prices are observations, not a verified trading calendar. Caller
    owns the read snapshot across these queries.
    """
    returns = get_benchmark_price_returns(connection, ticker, start_date, end_date)
    boundaries = get_prices(connection, ticker, start_date, end_date).index
    return _from_boundaries(returns, boundaries, calendar)


def _validate(observations, side, minimum_observations=2):
    if isinstance(minimum_observations, bool) or minimum_observations not in (1, 2):
        raise ValueError("minimum_observations must be 1 or 2")
    ordered = tuple(sorted(observations, key=lambda item: (item.end_date, item.start_date or date.min)))
    issues = []
    seen = set()
    previous = None
    for item in ordered:
        interval = (item.start_date, item.end_date)
        if interval in seen:
            issues.append(CoverageIssue("duplicate_interval", "Duplicate interval is ambiguous.", side, *interval))
        seen.add(interval)
        if item.start_date is None:
            issues.append(CoverageIssue("unknown_start", "Actual return start/baseline was not supplied.", side, *interval))
        elif previous is not None:
            if item.start_date < previous.end_date:
                issues.append(CoverageIssue("overlapping_intervals", "Return intervals overlap.", side, *interval))
            elif item.start_date > previous.end_date:
                issues.append(CoverageIssue("coverage_gap", "A return interval is missing between observations.", side, previous.end_date, item.start_date))
        if item.trading_sessions is None:
            issues.append(CoverageIssue("unverified_sessions", "No verified trading-session count is available.", side, *interval))
        elif item.trading_sessions != 1:
            issues.append(CoverageIssue("not_one_session", "Daily metrics require exactly one verified trading session per return.", side, *interval))
        previous = item
    if len(ordered) < minimum_observations:
        message = "At least two daily return intervals are required." if minimum_observations == 2 else "At least one daily return interval is required."
        issues.append(CoverageIssue("insufficient_observations", message, side))
    return ordered, issues


def _align(portfolio, benchmark, minimum_observations=2):
    portfolio, p_issues = _validate(portfolio, "portfolio", minimum_observations)
    benchmark, b_issues = _validate(benchmark, "benchmark", minimum_observations)
    issues = p_issues + b_issues
    p_intervals = {(item.start_date, item.end_date) for item in portfolio}
    b_intervals = {(item.start_date, item.end_date) for item in benchmark}
    # Fail closed for the entire supplied window; do not compound a silently
    # selected subset that omits intervening returns or changes the baseline.
    for start, end in sorted(p_intervals - b_intervals, key=lambda x: (x[1], x[0] or date.min)):
        issues.append(CoverageIssue("unmatched_interval", "Benchmark has no return with these exact boundaries.", "benchmark", start, end))
    for start, end in sorted(b_intervals - p_intervals, key=lambda x: (x[1], x[0] or date.min)):
        issues.append(CoverageIssue("unmatched_interval", "Portfolio has no return with these exact boundaries.", "portfolio", start, end))
    return portfolio, benchmark, tuple(issues)


def _series(observations):
    return pd.Series([item.return_value for item in observations],
                     index=pd.DatetimeIndex([item.end_date for item in observations]), dtype=float)


def _period(observations, benchmark=None, requested_start_date=None, verified=False):
    baseline = observations[0].start_date if observations else None
    first_end = observations[0].end_date if observations else None
    end = observations[-1].end_date if observations else None
    if benchmark is not None:
        if not benchmark or baseline != benchmark[0].start_date:
            baseline = None
        if not benchmark or first_end != benchmark[0].end_date:
            first_end = None
        if not benchmark or end != benchmark[-1].end_date:
            end = None
    requested = _date(requested_start_date) if requested_start_date is not None else None
    metadata = analysis_period_metadata(
        requested_start_date=requested, baseline_date=baseline, first_return_end_date=first_end,
        effective_end_date=end, session_coverage_verified=verified,
    )
    return dict(baseline_date=baseline, end_date=end, requested_start_date=requested,
                first_return_end_date=first_end, period_metadata_reason=metadata["period_metadata_reason"],
                session_coverage_verified=verified)


def calculate_interval_performance(
    observations: Iterable[ReturnObservation], initial_capital: float = 100_000.0,
    risk_free_rate: float = 0.0,
    *, requested_start_date: date | None = None,
    minimum_observations: int = 2,
) -> IntervalResult[dict]:
    """Opt into one observation for growth, retaining strict interval validation."""
    ordered, issues = _validate(observations, "portfolio", minimum_observations)
    period = _period(ordered, requested_start_date=requested_start_date, verified=not issues)
    if issues:
        return IntervalResult("insufficient_coverage", reasons=tuple(issues), **period)
    return IntervalResult("ok", calculate_performance_metrics(_series(ordered), initial_capital, risk_free_rate,
                          minimum_observations=minimum_observations),
                          observations=len(ordered), **period)


def compare_interval_returns(
    portfolio: Iterable[ReturnObservation], benchmark: Iterable[ReturnObservation],
    initial_capital: float = 100_000.0, risk_free_rate: float = 0.0,
    *, requested_start_date: date | None = None,
    minimum_observations: int = 2,
) -> IntervalResult[dict]:
    """Compare exact intervals; one-observation growth comparisons are opt-in."""
    portfolio, benchmark, issues = _align(portfolio, benchmark, minimum_observations)
    period = _period(portfolio, benchmark, requested_start_date, verified=not issues)
    if issues:
        return IntervalResult("insufficient_coverage", reasons=issues, **period)
    # Financial values are unchanged; this new comparison reports the baseline
    # while the legacy function retains its ending-date start_date convention.
    value = compare_portfolio_to_benchmark(_series(portfolio), _series(benchmark), initial_capital, risk_free_rate,
                                         minimum_observations=minimum_observations)
    value.update(analysis_period_metadata(
        requested_start_date=period["requested_start_date"], baseline_date=period["baseline_date"],
        first_return_end_date=period["first_return_end_date"], effective_end_date=period["end_date"],
        session_coverage_verified=True,
    ))
    value["start_date"] = value["effective_baseline_date"]
    return IntervalResult("ok", value, observations=len(portfolio), **period)


def interval_beta(
    portfolio: Iterable[ReturnObservation], benchmark: Iterable[ReturnObservation],
    *, requested_start_date: date | None = None,
) -> IntervalResult[float]:
    portfolio, benchmark, issues = _align(portfolio, benchmark)
    period = _period(portfolio, benchmark, requested_start_date, verified=not issues)
    if issues:
        return IntervalResult("insufficient_coverage", reasons=issues, **period)
    try:
        value = portfolio_beta(_series(portfolio), _series(benchmark))
    except ValueError as error:
        if str(error) != "Benchmark variance must be positive":
            raise
        return IntervalResult("undefined_metric", reasons=(CoverageIssue("zero_benchmark_variance", str(error), "benchmark"),), **period)
    if not isfinite(value):
        return IntervalResult("undefined_metric", reasons=(CoverageIssue("nonfinite_beta", "Beta is not finite.", "both"),), **period)
    return IntervalResult("ok", value, observations=len(portfolio), **period)
