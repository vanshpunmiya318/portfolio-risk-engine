"""Framework-independent preparation records; dates and numbers remain native Python."""
from dataclasses import dataclass
from datetime import date
from typing import Literal

from riskengine.risk.return_intervals import ReturnObservation


@dataclass(frozen=True)
class AnalysisIssue:
    code: str
    message: str
    scope: Literal["portfolio", "benchmark", "comparison", "calendar"]
    category: Literal["coverage", "integrity", "methodology"] = "coverage"
    start_date: date | None = None
    end_date: date | None = None
    ticker: str | None = None


@dataclass(frozen=True)
class AnalysisPeriod:
    requested_start_date: date | None
    requested_end_date: date | None
    selected_start_date: date | None
    selected_end_date: date | None
    effective_baseline_date: date | None
    first_return_end_date: date | None
    effective_end_date: date | None


@dataclass(frozen=True)
class AnalysisValuation:
    date: date
    holdings_value: float | None
    cash_balance: float | None
    total_equity: float | None
    net_contributions: float | None
    complete: bool


@dataclass(frozen=True)
class AnalysisCoverage:
    calendar_verified: bool
    expected_sessions: int | None
    expected_intervals: int | None
    valuation_count: int
    valid_valuation_count: int
    interval_count: int
    missing_sessions: tuple[date, ...]
    complete: bool | None
    daily_eligible: bool


@dataclass(frozen=True)
class AnalysisMethodology:
    calendar_source: str | None
    price_age_days: int = 0
    external_flow_timing: str = "end_of_period; flows in (start, end]"
    portfolio_price_basis: str = "existing_engine: raw_close_for_tiingo; adjusted_close_otherwise"
    benchmark_return_basis: str = "raw_close_price_return; dividends_not_reinvested"
    return_unit: str = "fraction"
    monetary_unit: str = "portfolio_currency; no_FX_conversion"
    annualization_sessions: int = 252


@dataclass(frozen=True)
class PreparedAnalysis:
    portfolio_id: int
    name: str
    currency: str
    benchmark_ticker: str
    benchmark_currency: str | None
    confidence: float
    period: AnalysisPeriod
    valuations: tuple[AnalysisValuation, ...]
    portfolio_intervals: tuple[ReturnObservation, ...]
    benchmark_intervals: tuple[ReturnObservation, ...]
    portfolio_coverage: AnalysisCoverage
    benchmark_coverage: AnalysisCoverage
    comparison_eligible: bool
    issues: tuple[AnalysisIssue, ...]
    methodology: AnalysisMethodology
