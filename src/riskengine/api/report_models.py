"""Versioned JSON report composed exclusively from validated service contracts."""
from datetime import date
from typing import Literal

from pydantic import FiniteFloat, model_validator

from riskengine.api.analysis_models import AnalysisMessage, PerformanceResponse, ResponseModel, RiskResponse
from riskengine.api.allocation_models import AllocationResponse
from riskengine.api.holdings_models import HoldingsResponse
from riskengine.risk.analysis_models import AnalysisPeriod


class ReportAmount(ResponseModel):
    value: FiniteFloat | None
    unavailable_reasons: tuple[AnalysisMessage, ...] = ()

    @model_validator(mode="after")
    def explicit_availability(self):
        if (self.value is None) != bool(self.unavailable_reasons):
            raise ValueError("Unavailable amounts require reasons; available amounts cannot have reasons")
        return self


class EndpointSummary(ResponseModel):
    valuation_date: date | None
    currency: str
    total_equity: ReportAmount
    invested_value: ReportAmount
    cash_balance: ReportAmount
    net_contributions: ReportAmount
    remaining_average_cost_basis: ReportAmount


class ReportProvenance(ResponseModel):
    report_methodology: Literal["validated_portfolio_report_v1"] = "validated_portfolio_report_v1"
    schema_version: Literal["1.0"] = "1.0"
    snapshot_policy: Literal["one_caller_owned_read_transaction"] = "one_caller_owned_read_transaction"
    persisted_snapshot_id: None = None
    reproducibility: str = "No persisted snapshot identifier; regeneration depends on unchanged stored data and engine/calendar versions."
    point_in_time_cutoff: date | None
    latest_portfolio_price_date: date | None
    price_sources: tuple[str, ...]
    calendar_provenance: str | None
    classification_basis: str = "current_instrument_metadata_not_look_through_or_historically_versioned"
    section_dates: str = "Performance/Risk describe the requested period; Holdings/Allocation use the explicit end_date or selected period end, never a last-complete valuation fallback."


class ReportResponse(ResponseModel):
    status: Literal["ok", "partial", "unavailable"]
    portfolio_id: int
    name: str
    currency: str
    period: AnalysisPeriod
    endpoint: EndpointSummary
    performance: PerformanceResponse
    risk: RiskResponse
    holdings: HoldingsResponse | None
    allocation: AllocationResponse | None
    warnings: tuple[AnalysisMessage, ...]
    unavailable_reasons: tuple[AnalysisMessage, ...]
    provenance: ReportProvenance
