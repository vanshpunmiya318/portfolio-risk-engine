"""Typed transport records for analysis adapters; no routes or database access."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, FiniteFloat, model_validator

from riskengine.risk.analysis_models import AnalysisCoverage, AnalysisMethodology, AnalysisPeriod


class ResponseModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AnalysisMessage(ResponseModel):
    code: str
    message: str
    scope: str
    category: str = "coverage"
    metric: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    ticker: str | None = None


class MetricValue(ResponseModel):
    value: FiniteFloat | None
    unit: Literal["fraction", "ratio"] = "fraction"
    observations: int
    unavailable_reasons: tuple[AnalysisMessage, ...] = ()

    @model_validator(mode="after")
    def explicit_availability(self):
        if (self.value is None) != bool(self.unavailable_reasons):
            raise ValueError("Null metrics require reasons; available metrics cannot have unavailable reasons")
        return self


class PerformanceMetrics(ResponseModel):
    cumulative_return: MetricValue
    annualized_return: MetricValue
    annualized_volatility: MetricValue
    sharpe_ratio: MetricValue
    maximum_drawdown: MetricValue


class ComparisonMetrics(ResponseModel):
    excess_cumulative_return: MetricValue
    excess_annualized_return: MetricValue


class RiskMetrics(ResponseModel):
    annualized_volatility: MetricValue
    sharpe_ratio: MetricValue
    maximum_drawdown: MetricValue
    beta: MetricValue
    historical_var: MetricValue
    historical_cvar: MetricValue


class ValuationPoint(ResponseModel):
    date: date
    holdings_value: FiniteFloat | None
    cash_balance: FiniteFloat | None
    total_equity: FiniteFloat | None
    net_contributions: FiniteFloat | None
    complete: bool


class ReturnInterval(ResponseModel):
    start_date: date | None
    end_date: date
    return_value: FiniteFloat
    trading_sessions: int | None


class ChartPoint(ResponseModel):
    date: date
    value: FiniteFloat


class ChartSeries(ResponseModel):
    unit: Literal["wealth_index", "fraction"]
    points: tuple[ChartPoint, ...] = ()
    unavailable_reasons: tuple[AnalysisMessage, ...] = ()


class PairedChartSeries(ResponseModel):
    portfolio: ChartSeries
    benchmark: ChartSeries


class MetricConventions(ResponseModel):
    risk_free_rate: FiniteFloat = 0.0
    annualization_sessions: int = 252
    volatility_ddof: int = 1
    maximum_drawdown_sign: str = "positive_loss_magnitude"
    drawdown_series_sign: str = "positive_loss_magnitude"
    var_method: str = "negative_numpy_linear_quantile_at_1_minus_confidence; not_clamped"
    cvar_method: str = "negative_fractional_tail_weighted_mean; worst_observation_if_tail_size_below_one"
    tail_horizon: str = "one_verified_trading_session"
    chart_policy: str = "baseline_inclusive; unavailable_for_incomplete_windows; no_gap_bridging"


class AnalysisResponse(ResponseModel):
    status: Literal["ok", "partial", "unavailable"]
    portfolio_id: int
    name: str
    currency: str
    benchmark_ticker: str
    benchmark_currency: str | None
    confidence: FiniteFloat
    period: AnalysisPeriod
    portfolio_coverage: AnalysisCoverage
    benchmark_coverage: AnalysisCoverage
    comparison_eligible: bool
    methodology: AnalysisMethodology
    metric_conventions: MetricConventions
    valuations: tuple[ValuationPoint, ...]
    portfolio_intervals: tuple[ReturnInterval, ...]
    benchmark_intervals: tuple[ReturnInterval, ...]
    warnings: tuple[AnalysisMessage, ...]
    unavailable_reasons: tuple[AnalysisMessage, ...]


class PerformanceResponse(AnalysisResponse):
    portfolio_metrics: PerformanceMetrics
    benchmark_metrics: PerformanceMetrics
    comparison: ComparisonMetrics
    wealth_index: PairedChartSeries


class RiskResponse(AnalysisResponse):
    portfolio_metrics: RiskMetrics
    benchmark_metrics: RiskMetrics
    drawdown: PairedChartSeries
