"""Recorded ledger history; no valuation or accounting aggregation."""
from pydantic import BaseModel, FiniteFloat


class HistoryIssue(BaseModel):
    code: str
    message: str


class TradeHistoryRow(BaseModel):
    id: int
    date: str
    transaction_type: str
    instrument_id: int
    ticker: str | None
    instrument_name: str | None
    instrument_currency: str | None
    quantity: FiniteFloat
    price: FiniteFloat
    currency: str
    source: str
    issues: list[HistoryIssue]


class CashFlowHistoryRow(BaseModel):
    id: int
    date: str
    flow_type: str
    amount: FiniteFloat
    currency: str
    source: str
    issues: list[HistoryIssue]


class TransactionsResponse(BaseModel):
    portfolio_id: int
    name: str
    currency: str
    trades: list[TradeHistoryRow]
    external_cash_flows: list[CashFlowHistoryRow]
