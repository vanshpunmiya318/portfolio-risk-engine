"""Read-only allocation contract: historical reference, never policy targets."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, FiniteFloat


class Record(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")


class AllocationIssue(Record):
    code: str
    message: str
    scope: Literal["current", "reference", "target"]
    ticker: str | None = None


class SecurityAllocation(Record):
    ticker: str
    instrument_name: str
    classification: str
    quantity: FiniteFloat | None
    effective_price_date: date | None
    price_status: str
    market_value: FiniteFloat | None
    weight: FiniteFloat | None


class ClassificationAllocation(Record):
    classification: str
    market_value: FiniteFloat
    weight: FiniteFloat
    holdings_count: int


class Concentration(Record):
    hhi: FiniteFloat | None = None
    effective_holdings: FiniteFloat | None = None
    largest_position_weight: FiniteFloat | None = None


class AllocationSnapshot(Record):
    valuation_date: date | None
    valuation_complete: bool
    invested_value: FiniteFloat | None
    cash_balance: FiniteFloat | None
    securities: list[SecurityAllocation]
    classification_exposures: list[ClassificationAllocation]
    concentration: Concentration
    unavailable_reasons: list[AllocationIssue]


class HistoricalReference(Record):
    reference_date: date | None
    methodology: Literal["first_recorded_buy_date_end_of_day_valuation"] = "first_recorded_buy_date_end_of_day_valuation"
    provenance: Literal["recorded_transactions_and_stored_exact_date_prices"] = "recorded_transactions_and_stored_exact_date_prices"
    is_investment_policy_target: Literal[False] = False
    snapshot: AllocationSnapshot | None
    unavailable_reasons: list[AllocationIssue]


class AllocationChange(Record):
    ticker: str
    current_weight: FiniteFloat
    reference_weight: FiniteFloat
    weight_change_fraction: FiniteFloat


class AllocationResponse(Record):
    portfolio_id: int
    name: str
    currency: str
    requested_valuation_date: date | None
    effective_valuation_date: date | None
    weight_basis: Literal["invested_holdings_excluding_cash"] = "invested_holdings_excluding_cash"
    weight_unit: Literal["fraction"] = "fraction"
    classification_basis: Literal["current_instrument_metadata_not_look_through_or_historically_versioned"] = "current_instrument_metadata_not_look_through_or_historically_versioned"
    max_price_age_days: Literal[0] = 0
    current: AllocationSnapshot
    reference: HistoricalReference
    changes: list[AllocationChange] | None
    comparison_unavailable_reasons: list[AllocationIssue]
    target_allocation: None = None
    target_unavailable_reasons: list[AllocationIssue]
    warnings: list[AllocationIssue]
