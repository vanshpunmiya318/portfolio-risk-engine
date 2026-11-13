"""Connection-based historical holdings with explicit valuation completeness.

Uses the existing valuation and average-cost engines; never opens a database.
"""
import math
from datetime import date

from riskengine.db.portfolios import get_portfolio_value
from riskengine.db.transactions import get_cost_basis


class PortfolioNotFound(ValueError):
    pass


class HoldingsInputError(ValueError):
    pass


class HoldingsDataConflict(ValueError):
    pass


def _calculate(connection, portfolio_id, cutoff):
    # Adapt only known integrity failures from the existing engine. Unrelated
    # ValueErrors remain programming/server errors, rather than HTTP conflicts.
    try:
        valuations = get_portfolio_value(connection, portfolio_id, cutoff, allow_missing_prices=True)
        costs = get_cost_basis(connection, portfolio_id, cutoff).set_index("ticker")
        return valuations, costs
    except ValueError as error:
        if str(error).startswith(("Negative position detected", "Incomplete corporate action coverage", "Invalid coverage date range")):
            raise HoldingsDataConflict(str(error)) from error
        raise


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def get_holdings_summary(connection, portfolio_id, as_of_date=None, max_price_age_days=0):
    """Use an inclusive cutoff; reject prices older than the calendar-day limit.

    Price age is in calendar days; zero requires an exact-date price.
    Omitted cutoff uses MAX(prices.date). With no prices, use the portfolio's
    last transaction date, explicitly warned; a completely empty portfolio
    has no effective date. No current date is invented.
    An explicit cutoff is never silently moved to an earlier trading date.
    No totals or weights are inferred from a partially priced portfolio.
    """
    if isinstance(portfolio_id, bool) or not isinstance(portfolio_id, int) or not 0 < portfolio_id <= 9223372036854775807:
        raise HoldingsInputError("portfolio_id must be a positive SQLite integer")
    if isinstance(max_price_age_days, bool) or not isinstance(max_price_age_days, int) or max_price_age_days < 0:
        raise HoldingsInputError("max_price_age_days must be a nonnegative integer")
    try:
        requested = date.fromisoformat(as_of_date).isoformat() if as_of_date is not None else None
    except (ValueError, TypeError) as error:
        raise HoldingsInputError("as_of_date must be a valid YYYY-MM-DD date") from error
    portfolio = connection.execute(
        "SELECT name, currency FROM portfolios WHERE id = ?", (portfolio_id,)
    ).fetchone()
    if portfolio is None:
        raise PortfolioNotFound("Portfolio not found")
    cutoff = requested or connection.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    warnings = []
    if cutoff is None:
        cutoff = connection.execute(
            "SELECT MAX(date) FROM transactions WHERE portfolio_id = ?", (portfolio_id,)
        ).fetchone()[0]
        if cutoff is None:
            return dict(
                portfolio_id=portfolio_id, name=portfolio[0], currency=portfolio[1],
                requested_valuation_date=None, effective_valuation_date=None,
                max_price_age_days=max_price_age_days, valuation_complete=True,
                holdings=[], totals=dict(holding_count=0, valued_holding_count=0,
                                        market_value=0.0, remaining_average_cost_basis=0.0, unrealized_gain_loss=0.0),
                warnings=[dict(code="no_open_positions", message="Portfolio has no transactions or market prices; no valuation date is available.")],
            )
        warnings.append(dict(code="cutoff_from_transactions", message="No market prices available; cutoff is the portfolio's last transaction date, not a market valuation date."))
    try:
        valuation_date = date.fromisoformat(cutoff)
    except (ValueError, TypeError) as error:
        raise HoldingsDataConflict("Invalid stored valuation date") from error
    valuations, costs = _calculate(connection, portfolio_id, cutoff)
    metadata = {
        row[0]: (row[1], row[2]) for row in connection.execute("SELECT ticker, name, currency FROM instruments")
    }
    transaction_currencies = {}
    for ticker, currency in connection.execute(
        "SELECT DISTINCT i.ticker, t.currency FROM transactions t "
        "JOIN instruments i ON i.id = t.instrument_id "
        "WHERE t.portfolio_id = ? AND t.date <= ?", (portfolio_id, cutoff),
    ):
        transaction_currencies.setdefault(ticker, set()).add(currency)
    holdings = []
    for row in valuations.itertuples(index=False):
        quantity = finite(row.quantity)
        basis = finite(costs.loc[row.ticker, "total_cost"]) if row.ticker in costs.index else None
        value = finite(row.market_value)
        price_date = None if str(row.price_date) == "NaT" else row.price_date.date()
        age = (valuation_date - price_date).days if price_date is not None else None
        market_price = finite(value / quantity) if value is not None and quantity is not None and quantity > 0 else None
        status = "missing" if price_date is None else "stale" if age > max_price_age_days else "carried_forward" if age > 0 else "current"
        if status in {"missing", "stale"}:
            value = None
            warnings.append(dict(code=status + "_price", ticker=row.ticker,
                                 message="No price on or before the cutoff." if status == "missing" else "Price exceeds the allowed calendar-day age; excluded from valuation."))
        elif market_price is None or quantity is None or basis is None:
            status, value = "invalid", None
            warnings.append(dict(code="invalid_holding", ticker=row.ticker, message="Non-finite or unavailable valuation inputs."))
        elif status == "carried_forward":
            crossed_split = connection.execute(
                "SELECT 1 FROM corporate_actions ca JOIN instruments i ON i.id = ca.instrument_id "
                "WHERE i.ticker = ? AND ca.action_type = 'SPLIT' AND ca.ratio != 1 "
                "AND ca.ex_date > ? AND ca.ex_date <= ? LIMIT 1",
                (row.ticker, price_date.isoformat(), cutoff),
            ).fetchone()
            if crossed_split:
                status, value = "invalid", None
                warnings.append(dict(code="price_before_split", ticker=row.ticker,
                                     message="Price predates a stock split; excluded rather than applying it to post-split quantities."))
        name, currency = metadata[row.ticker]
        currency_conflict = currency != portfolio[1] or any(
            trade_currency != currency for trade_currency in transaction_currencies.get(row.ticker, set())
        )
        if currency_conflict:
            value, basis = None, None
            warnings.append(dict(code="currency_mismatch", ticker=row.ticker,
                                 message="Transaction, instrument or portfolio currencies conflict; valuation and cost basis excluded. No FX conversion is applied."))
        if status == "carried_forward" and value is not None:
            warnings.append(dict(code="carried_forward_price", ticker=row.ticker, message="Earlier price accepted within the allowed calendar-day age."))
        holdings.append(dict(
            ticker=row.ticker, instrument_name=name, quantity=quantity,
            effective_price_date=price_date.isoformat() if price_date else None,
            price_age_days=age, price_status=status, market_price=market_price,
            market_price_currency=currency,
            currency_status="conflict" if currency_conflict else "consistent",
            market_value=value, remaining_average_cost_basis=basis,
            unrealized_gain_loss=finite(value - basis) if value is not None and basis is not None else None,
            invested_weight=None,
        ))
    complete = all(h["market_value"] is not None and h["remaining_average_cost_basis"] is not None for h in holdings)
    # Currency mismatches also invalidate cost totals, even when basis is finite.
    basis_complete = all(h["remaining_average_cost_basis"] is not None for h in holdings) and not any(w["code"] == "currency_mismatch" for w in warnings)
    total_basis = finite(sum(h["remaining_average_cost_basis"] for h in holdings)) if basis_complete else None
    total_value = finite(sum(h["market_value"] for h in holdings)) if complete else None
    complete = complete and total_value is not None and total_basis is not None
    if complete and total_value > 0:
        for holding in holdings:
            holding["invested_weight"] = finite(holding["market_value"] / total_value)
    if not holdings:
        warnings.append(dict(code="no_open_positions", message="No open positions at the valuation cutoff."))
    if not complete:
        warnings.append(dict(code="incomplete_valuation", message="Aggregate market value, unrealized gain/loss and all weights are unavailable."))
    return dict(
        portfolio_id=portfolio_id, name=portfolio[0], currency=portfolio[1],
        requested_valuation_date=requested, effective_valuation_date=cutoff,
        max_price_age_days=max_price_age_days, valuation_complete=complete,
        holdings=holdings,
        totals=dict(holding_count=len(holdings), valued_holding_count=sum(h["market_value"] is not None for h in holdings),
                    market_value=total_value if complete else None, remaining_average_cost_basis=total_basis,
                    unrealized_gain_loss=finite(total_value - total_basis) if complete else None),
        warnings=warnings,
    )
