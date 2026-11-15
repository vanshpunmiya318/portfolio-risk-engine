"""Read-only report composition; financial calculations stay in existing engines.

The connection and transaction belong to the caller. No lifecycle operations,
exports, legacy attribution, or independently reconstructed return series.
"""
from riskengine.api.report_models import EndpointSummary, ReportAmount, ReportProvenance, ReportResponse
from riskengine.api.holdings_models import HoldingsResponse
from riskengine.api.analysis_models import AnalysisMessage
from riskengine.risk.analysis_adapters import build_performance_response, build_risk_response
from riskengine.risk.performance_service import _prepare_response_analysis, validate_risk_request
from riskengine.risk.holdings_service import get_holdings_summary, finite
from riskengine.risk.allocation_service import get_allocation_response
from riskengine.db.transactions import get_net_cash_flows


def _overall_status(endpoint, performance, risk, holdings, allocation):
    """Use financial availability, not metadata, diagnostics or numeric truthiness.

    ``ok`` is the existing contract's fully available state. Policy targets and
    benchmark self-beta are intentionally inapplicable, not missing sections.
    """
    amounts = [getattr(endpoint, name) for name in (
        "total_equity", "invested_value", "cash_balance", "net_contributions",
        "remaining_average_cost_basis",
    )]
    values = [amount.value for amount in amounts]
    for response in (performance, risk):
        for group in (response.portfolio_metrics, response.benchmark_metrics):
            values.extend(getattr(group, name).value for name in type(group).model_fields)
    values.extend(getattr(performance.comparison, name).value
                  for name in type(performance.comparison).model_fields)
    if holdings is not None:
        values.extend(getattr(holdings.totals, name) for name in (
            "market_value", "remaining_average_cost_basis", "unrealized_gain_loss",
        ))
        for holding in holdings.holdings:
            values.extend((holding.market_value, holding.remaining_average_cost_basis,
                           holding.unrealized_gain_loss))
    if allocation is not None:
        for snapshot in (allocation.current, allocation.reference.snapshot):
            if snapshot is not None:
                values.extend((snapshot.invested_value, snapshot.cash_balance))
                values.extend(getattr(snapshot.concentration, name)
                              for name in type(snapshot.concentration).model_fields)
                for security in snapshot.securities:
                    values.extend((security.market_value, security.weight))
    if not any(value is not None for value in values):
        return "unavailable"
    complete = (
        all(amount.value is not None for amount in amounts)
        and performance.status == risk.status == "ok"
        and holdings is not None and holdings.valuation_complete
        and allocation is not None and allocation.current.valuation_complete
        and not allocation.current.unavailable_reasons
        and allocation.reference.snapshot is not None
        and allocation.reference.snapshot.valuation_complete
        and not allocation.reference.unavailable_reasons
        and allocation.changes is not None
        and not allocation.comparison_unavailable_reasons
    )
    return "ok" if complete else "partial"


def get_report_response(connection, portfolio_id, start_date=None, end_date=None,
                        benchmark="SPY", confidence=0.95):
    """Compose all sections within an already-active caller-owned snapshot."""
    validate_risk_request(start_date, end_date, benchmark, confidence)
    prepared = _prepare_response_analysis(connection, portfolio_id, start_date, end_date, benchmark, confidence)
    performance = build_performance_response(prepared)
    risk = build_risk_response(prepared)
    # Do not retreat to the last valid valuation when the endpoint is incomplete.
    cutoff = end_date or (prepared.period.selected_end_date.isoformat()
                          if prepared.period.selected_end_date else None)
    holdings = allocation = None
    warnings = list(performance.warnings)
    reasons = []
    if cutoff is not None:
        # Lexically request-local: retain validated rows only for this exact
        # connection/portfolio/date/price-age policy, inside the active snapshot.
        point_in_time_holdings = {}

        def load_holdings(snapshot, pid, day, max_age):
            if snapshot is not connection or pid != portfolio_id or not snapshot.in_transaction or max_age != 0:
                raise ValueError("Report holdings reuse requires the same active snapshot and exact-date policy")
            if day not in point_in_time_holdings:
                point_in_time_holdings[day] = get_holdings_summary(snapshot, pid, day, max_age)
            return point_in_time_holdings[day]

        holdings = HoldingsResponse.model_validate(load_holdings(connection, portfolio_id, cutoff, 0))
        allocation = get_allocation_response(connection, portfolio_id, cutoff, _holdings_loader=load_holdings)
        warnings.extend(AnalysisMessage(code=w.code, message=w.message, scope="holdings", ticker=w.ticker)
                        for w in holdings.warnings)
        warnings.extend(AnalysisMessage(code=w.code, message=w.message, scope=f"allocation.{w.scope}", ticker=w.ticker)
                        for w in allocation.warnings)
        reasons.extend(AnalysisMessage(code=w.code, message=w.message, scope=f"allocation.{w.scope}", ticker=w.ticker)
                       for w in allocation.current.unavailable_reasons)
    else:
        reasons.append(AnalysisMessage(code="no_reporting_cutoff", message="No portfolio reporting cutoff is available; point-in-time sections are unavailable.", scope="endpoint"))

    currency_conflict = allocation is not None and any(
        r.code == "currency_conflict" for r in allocation.current.unavailable_reasons)
    current = allocation.current if allocation else None
    invested = current.invested_value if current else None
    cash = current.cash_balance if current else None
    contributions = finite(get_net_cash_flows(connection, portfolio_id, cutoff)) if cutoff and not currency_conflict else None
    basis = holdings.totals.remaining_average_cost_basis if holdings and not currency_conflict else None
    # Prefer the identical prepared endpoint scalar, including its completeness gate.
    valuation = next((v for v in prepared.valuations if v.date.isoformat() == cutoff), None)
    equity = valuation.total_equity if valuation else finite(invested + cash) if invested is not None and cash is not None else None

    def amount(value, name):
        value = finite(value)
        unavailable = () if value is not None else tuple(reasons) or (AnalysisMessage(
            code="endpoint_value_unavailable", message=f"{name} is unavailable at the exact reporting cutoff; see section diagnostics.", scope="endpoint"),)
        return ReportAmount(value=value, unavailable_reasons=unavailable)

    endpoint = EndpointSummary(valuation_date=cutoff, currency=prepared.currency,
        total_equity=amount(equity, "Equity"), invested_value=amount(invested, "Invested value"),
        cash_balance=amount(cash, "Cash"), net_contributions=amount(contributions, "Contributions"),
        remaining_average_cost_basis=amount(basis, "Remaining average-cost basis"))
    price_rows = connection.execute(
        "SELECT MAX(p.date), p.source FROM prices p WHERE p.instrument_id IN "
        "(SELECT instrument_id FROM transactions WHERE portfolio_id=? AND date<=?) "
        "AND p.date<=? GROUP BY p.source ORDER BY p.source", (portfolio_id, cutoff, cutoff)).fetchall() if cutoff else []
    reference_reasons = tuple(AnalysisMessage(code=r.code, message=r.message, scope=f"allocation.{r.scope}", ticker=r.ticker)
                              for r in allocation.reference.unavailable_reasons) if allocation else ()
    status = _overall_status(endpoint, performance, risk, holdings, allocation)
    return ReportResponse(status=status, portfolio_id=prepared.portfolio_id, name=prepared.name,
        currency=prepared.currency, period=prepared.period, endpoint=endpoint, performance=performance,
        risk=risk, holdings=holdings, allocation=allocation, warnings=tuple(warnings),
        unavailable_reasons=tuple(reasons) + reference_reasons + performance.unavailable_reasons + risk.unavailable_reasons,
        provenance=ReportProvenance(point_in_time_cutoff=cutoff,
            latest_portfolio_price_date=max((r[0] for r in price_rows), default=None),
            price_sources=tuple(r[1] for r in price_rows), calendar_provenance=prepared.methodology.calendar_source))
