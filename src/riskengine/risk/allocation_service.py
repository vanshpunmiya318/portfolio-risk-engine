"""Snapshot-owned allocation orchestration; existing engines own valuations."""
import math
import pandas as pd

from riskengine.api.allocation_models import AllocationResponse
from riskengine.risk.holdings_service import get_holdings_summary, HoldingsDataConflict, HoldingsInputError
from riskengine.risk.exposure import get_portfolio_exposure, _exposure_from_valued_holdings
from riskengine.risk.metrics import portfolio_hhi
from riskengine.db.transactions import get_cash_balance, has_portfolio_currency_conflict


def _issue(code, message, scope, ticker=None):
    return dict(code=code, message=message, scope=scope, ticker=ticker)


def _snapshot(connection, portfolio_id, day, scope, warnings, holdings_loader=None):
    load_holdings = holdings_loader or get_holdings_summary
    holdings = load_holdings(connection, portfolio_id, day, 0)
    cutoff = holdings["effective_valuation_date"]
    warnings.extend(_issue(w["code"], w["message"], scope, w.get("ticker")) for w in holdings["warnings"])
    reasons = []
    currency = holdings["currency"]
    currency_conflict = cutoff is not None and has_portfolio_currency_conflict(connection, portfolio_id, cutoff, currency)
    if currency_conflict:
        reasons.append(_issue("currency_conflict", "Historical transaction, instrument or cash-flow currencies conflict; no FX conversion is performed.", scope))
    complete = holdings["valuation_complete"] and not reasons
    if not holdings["valuation_complete"]:
        reasons.append(_issue("incomplete_valuation", "Exact-date holdings valuation is incomplete; aggregate allocations are unavailable.", scope))
    rows = holdings["holdings"]
    if len({row["ticker"] for row in rows}) != len(rows):
        raise HoldingsDataConflict("Duplicate ticker in allocation valuation")
    if complete and any(row["market_value"] <= 0 for row in rows):
        complete = False
        reasons.append(_issue("nonpositive_invested_value", "Exposure requires positive market values for every active invested holding.", scope))
    exposure = None
    if complete and rows:
        if holdings_loader is None:
            exposure = get_portfolio_exposure(connection, portfolio_id, cutoff, max_age_days=0)
        else:
            # Only complete, currency-checked, exact-date positive holdings reach
            # this branch. Preserve row order and the existing pandas reduction.
            frame = pd.DataFrame.from_records(rows, columns=["ticker", "quantity", "market_value"])
            exposure = _exposure_from_valued_holdings(connection, portfolio_id, cutoff, frame)
    value = exposure["total_invested_value"] if exposure else 0.0 if complete else None
    weights = {r["ticker"]: r["weight"] for r in exposure["security_exposures"]} if exposure else {}
    concentration = dict(hhi=None, effective_holdings=None, largest_position_weight=None)
    if weights:
        hhi = portfolio_hhi(pd.Series(list(weights.values()), dtype=float))
        concentration = dict(hhi=hhi, effective_holdings=1.0 / hhi,
                             largest_position_weight=exposure["largest_security"]["weight"])
    else:
        reasons.append(_issue("no_invested_allocation", "No complete positive invested allocation is available for concentration or comparison.", scope))
    cash = None
    if cutoff and not currency_conflict:
        cash = float(get_cash_balance(connection, portfolio_id, cutoff))
        if not math.isfinite(cash):
            cash = None
            warnings.append(_issue("nonfinite_cash", "Cash balance is non-finite and is suppressed.", scope))
    classifications = {ticker: str(sector) if sector is not None and str(sector).strip() else "Unclassified"
                       for ticker, sector in connection.execute("SELECT ticker,sector FROM instruments")}
    return dict(valuation_date=cutoff, valuation_complete=complete, invested_value=value, cash_balance=cash,
                securities=[dict(ticker=row["ticker"], instrument_name=row["instrument_name"],
                                 classification=classifications[row["ticker"]], quantity=row["quantity"],
                                 effective_price_date=row["effective_price_date"], price_status=row["price_status"],
                                 market_value=row["market_value"], weight=weights.get(row["ticker"])) for row in rows],
                classification_exposures=[dict(classification=row["sector"], market_value=row["market_value"],
                                               weight=row["weight"], holdings_count=row["holdings_count"])
                                          for row in exposure["sector_exposures"]] if exposure else [],
                concentration=concentration, unavailable_reasons=reasons)


def get_allocation_response(connection, portfolio_id, as_of_date=None, *, _holdings_loader=None):
    """Require a caller-owned read snapshot; never advance an invalid reference.

    _holdings_loader is internal to Reports: it must supply the unchanged
    get_holdings_summary result for the supplied connection/portfolio/date/age.
    Without it, standalone Holdings and Exposure loading remain unchanged.
    """
    if not connection.in_transaction:
        raise HoldingsInputError("Caller must begin a SQLite read transaction")
    warnings = []
    current = _snapshot(connection, portfolio_id, as_of_date, "current", warnings, _holdings_loader)
    portfolio = connection.execute("SELECT name,currency FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
    cutoff = current["valuation_date"]
    reference_date = connection.execute(
        "SELECT MIN(date) FROM transactions WHERE portfolio_id=? AND transaction_type='BUY' AND date<=?",
        (portfolio_id, cutoff),
    ).fetchone()[0] if cutoff else None
    reference = None
    reference_reasons = []
    if reference_date is None:
        reference_reasons.append(_issue("no_initial_investment", "No recorded initial buy exists on or before this cutoff.", "reference"))
    else:
        try:
            reference = _snapshot(connection, portfolio_id, reference_date, "reference", warnings, _holdings_loader)
            reference_reasons.extend(reference["unavailable_reasons"])
        except (HoldingsDataConflict, HoldingsInputError) as error:
            reference_reasons.append(_issue("invalid_reference", str(error), "reference"))
    changes = None
    comparison_reasons = []
    if reference and not reference_reasons and current["valuation_complete"] and current["invested_value"] and reference["invested_value"]:
        now = {r["ticker"]: r["weight"] for r in current["securities"]}
        then = {r["ticker"]: r["weight"] for r in reference["securities"]}
        changes = [dict(ticker=ticker, current_weight=now.get(ticker, 0.0), reference_weight=then.get(ticker, 0.0),
                        weight_change_fraction=now.get(ticker, 0.0) - then.get(ticker, 0.0)) for ticker in sorted(now.keys() | then.keys())]
    else:
        comparison_reasons.append(_issue("comparison_unavailable", "Both current and initial historical invested allocations must be complete and positive; the reference date is never advanced to bypass missing data.", "reference"))
    return AllocationResponse(
        portfolio_id=portfolio_id, name=portfolio[0], currency=portfolio[1], requested_valuation_date=as_of_date,
        effective_valuation_date=cutoff, current=current,
        reference=dict(reference_date=reference_date, snapshot=reference, unavailable_reasons=reference_reasons),
        changes=changes, comparison_unavailable_reasons=comparison_reasons,
        target_unavailable_reasons=[_issue("investment_policy_unavailable", "No versioned investment-policy targets are recorded. Historical starting weights are not policy targets or rebalance recommendations.", "target")],
        warnings=warnings,
    )
