"""Connection-owned orchestration for the read-only Performance response.

The caller supplies an active snapshot. Date queries only size calendar coverage;
prepare_analysis retains ownership of reporting/baseline selection. No benchmark
prices, unrelated instruments or current-clock values establish portfolio bounds.
"""
from datetime import date

from riskengine.api.analysis_models import PerformanceResponse
from riskengine.risk.analysis_adapters import build_performance_response
from riskengine.risk.analysis_models import AnalysisIssue
from riskengine.risk.analysis_service import (
    AnalysisInputError, AnalysisPortfolioNotFound, prepare_analysis,
)
from riskengine.risk.trading_calendar import verified_nyse_calendar


class PerformanceDataConflict(ValueError):
    def __init__(self, issues):
        self.issues = tuple(issues)
        super().__init__("Portfolio analysis contains proven integrity conflicts")


def _requested_date(value):
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except (ValueError, TypeError):
        pass
    raise AnalysisInputError("Analysis boundaries must be valid YYYY-MM-DD dates")


def validate_performance_request(start_date, end_date, benchmark):
    start, end = _requested_date(start_date), _requested_date(end_date)
    if start is not None and end is not None and end < start:
        raise AnalysisInputError("end_date cannot precede start_date")
    if not isinstance(benchmark, str) or not benchmark.strip():
        raise AnalysisInputError("benchmark must be a nonempty ticker")
    return start, end


def get_performance_response(connection, portfolio_id, start_date=None, end_date=None,
                             benchmark="SPY") -> PerformanceResponse:
    start, end = validate_performance_request(start_date, end_date, benchmark)
    if not connection.in_transaction:
        raise AnalysisInputError("Caller must begin a SQLite read transaction before analysis")
    if connection.execute("SELECT 1 FROM portfolios WHERE id=?", (portfolio_id,)).fetchone() is None:
        raise AnalysisPortfolioNotFound("Portfolio not found")
    cutoff = end.isoformat() if end else None
    event_bounds = connection.execute(
        "SELECT MIN(date), MAX(date) FROM ("
        "SELECT date FROM transactions WHERE portfolio_id=? "
        "UNION ALL SELECT date FROM cash_flows WHERE portfolio_id=?) "
        "WHERE (? IS NULL OR date<=?)", (portfolio_id, portfolio_id, cutoff, cutoff),
    ).fetchone()
    latest_price = connection.execute(
        "SELECT MAX(date) FROM prices WHERE instrument_id IN ("
        "SELECT instrument_id FROM transactions WHERE portfolio_id=? AND (? IS NULL OR date<=?)) "
        "AND (? IS NULL OR date<=?)", (portfolio_id, cutoff, cutoff, cutoff, cutoff),
    ).fetchone()[0]
    bounds = [day for day in (start, end) if day is not None]
    for value in (*event_bounds, latest_price):
        if value is None:
            continue
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except (ValueError, TypeError) as error:
            raise PerformanceDataConflict((AnalysisIssue(
                "invalid_stored_date", "Portfolio reporting history contains a malformed date.",
                "portfolio", "integrity",
            ),)) from error
        bounds.append(parsed)
    calendar = None
    if bounds:
        try:
            calendar = verified_nyse_calendar(min(bounds), max(bounds))
        except ValueError as error:
            # Unsupported request/calendar bounds are input failures, never
            # blanket financial ValueError-to-integrity translations.
            raise AnalysisInputError(f"Unsupported calendar coverage: {error}") from error
    prepared = prepare_analysis(connection, portfolio_id, start_date=start_date, end_date=end_date,
                                benchmark_ticker=benchmark, session_calendar=calendar)
    conflicts = tuple(issue for issue in prepared.issues if issue.category == "integrity")
    if conflicts:
        raise PerformanceDataConflict(conflicts)
    return build_performance_response(prepared)
