from datetime import date
from typing import Literal

from pydantic import BaseModel, FiniteFloat


class Holding(BaseModel):
    ticker: str
    instrument_name: str
    quantity: FiniteFloat | None
    effective_price_date: date | None
    price_age_days: int | None
    price_status: Literal["current", "carried_forward", "stale", "missing", "invalid"]
    market_price: FiniteFloat | None
    market_price_currency: str
    currency_status: Literal["consistent", "conflict"]
    market_value: FiniteFloat | None
    remaining_average_cost_basis: FiniteFloat | None
    unrealized_gain_loss: FiniteFloat | None
    invested_weight: FiniteFloat | None


class HoldingsTotals(BaseModel):
    holding_count: int
    valued_holding_count: int
    market_value: FiniteFloat | None
    remaining_average_cost_basis: FiniteFloat | None
    unrealized_gain_loss: FiniteFloat | None


class DataWarning(BaseModel):
    code: str
    ticker: str | None = None
    message: str


class HoldingsResponse(BaseModel):
    portfolio_id: int
    name: str
    currency: str
    requested_valuation_date: date | None
    effective_valuation_date: date | None
    max_price_age_days: int
    valuation_complete: bool
    weight_basis: Literal["invested_market_value_excluding_cash"] = "invested_market_value_excluding_cash"
    weight_unit: Literal["fraction"] = "fraction"
    cost_basis_method: Literal["remaining_average_cost_not_net_transaction_investment"] = "remaining_average_cost_not_net_transaction_investment"
    holdings: list[Holding]
    totals: HoldingsTotals
    warnings: list[DataWarning]
