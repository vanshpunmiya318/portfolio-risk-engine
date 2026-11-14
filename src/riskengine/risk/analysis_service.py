"""Read-only analysis preparation, without metrics, transport or calendar guesses.

The caller must begin a SQLite transaction before calling and retain ownership
of that snapshot/connection. This module never commits, rolls back or closes it.
An unknown calendar permits observed interval construction, not daily eligibility.
"""
from datetime import date, datetime
from math import isfinite
import sqlite3

from riskengine.db.instruments import normalise_ticker
from riskengine.db.portfolios import get_portfolio_value, prepare_positions
from riskengine.db.prices import get_prices
from riskengine.db.transactions import (
    get_cash_balance, get_net_cash_flows, validate_transactions,
)
from riskengine.risk.analysis_models import (
    AnalysisCoverage, AnalysisIssue, AnalysisMethodology, AnalysisPeriod,
    AnalysisValuation, PreparedAnalysis,
)
from riskengine.risk.valuation_context import PreparedValuationContext
from riskengine.risk.return_intervals import (
    VerifiedSessionCalendar, get_benchmark_return_observations,
    get_portfolio_return_observations,
)


class AnalysisInputError(ValueError):
    """Invalid request or absence of a caller-owned read transaction."""


class AnalysisPortfolioNotFound(LookupError):
    pass


def _date(value):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
    except ValueError:
        pass
    raise AnalysisInputError("Dates must be calendar dates or YYYY-MM-DD strings")


def _finite(value):
    value = float(value)
    if not isfinite(value):
        raise ValueError("Non-finite valuation")
    return value


def prepare_analysis(
    connection: sqlite3.Connection, portfolio_id: int, *,
    start_date: date | str | None = None, end_date: date | str | None = None,
    benchmark_ticker: str = "SPY", confidence: float = .95,
    session_calendar: VerifiedSessionCalendar | None = None,
) -> PreparedAnalysis:
    """Prepare explicit boundaries and independently assessed portfolio/benchmark data.

    Verified sessions select the first session on/after start and last on/before
    end. Missing expected prices never move these boundaries. Without a calendar,
    only observed portfolio-price/event dates and explicit boundaries are known.
    Prior events establish positions and cash; events after the cutoff are ignored.
    """
    if isinstance(portfolio_id, bool) or not isinstance(portfolio_id, int) or not 0 < portfolio_id <= 9223372036854775807:
        raise AnalysisInputError("portfolio_id must be a positive SQLite integer")
    start, end = _date(start_date), _date(end_date)
    if start is not None and end is not None and end < start:
        raise AnalysisInputError("end_date cannot precede start_date")
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not isfinite(confidence) or not 0 < confidence < 1:
        raise AnalysisInputError("confidence must be finite and between 0 and 1")
    if not isinstance(benchmark_ticker, str) or not benchmark_ticker.strip():
        raise AnalysisInputError("benchmark_ticker must be nonempty")
    ticker = normalise_ticker(benchmark_ticker)
    if session_calendar is not None and not isinstance(session_calendar, VerifiedSessionCalendar):
        raise AnalysisInputError("session_calendar must be a VerifiedSessionCalendar")
    if not connection.in_transaction:
        raise AnalysisInputError("Caller must begin a SQLite read transaction before preparation")
    portfolio = connection.execute("SELECT name, currency FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
    if portfolio is None:
        raise AnalysisPortfolioNotFound("Portfolio not found")
    name, currency = portfolio
    issues = []

    def issue(code, message, scope="portfolio", category="coverage", a=None, b=None, instrument=None):
        issues.append(AnalysisIssue(code, message, scope, category, a, b, instrument))

    # Dates are portfolio-specific: an unavailable benchmark cannot change them.
    events = connection.execute(
        "SELECT date FROM transactions WHERE portfolio_id = ? UNION "
        "SELECT date FROM cash_flows WHERE portfolio_id = ? ORDER BY date",
        (portfolio_id, portfolio_id),
    ).fetchall()
    # The existing date-range read also supplies exact-date membership for all
    # portfolio instruments in this caller-owned snapshot. Keep raw stored
    # dates/tickers: normalizing them could turn invalid records into prices.
    price_rows = connection.execute(
        "SELECT DISTINCT i.ticker, p.date FROM prices p "
        "LEFT JOIN instruments i ON i.id = p.instrument_id WHERE p.instrument_id IN "
        "(SELECT instrument_id FROM transactions WHERE portfolio_id = ? AND (? IS NULL OR date <= ?)) ORDER BY p.date",
        (portfolio_id, end.isoformat() if end else None, end.isoformat() if end else None),
    ).fetchall()
    price_presence = {(symbol, day) for symbol, day in price_rows}
    event_dates = [date.fromisoformat(row[0]) for row in events if end is None or row[0] <= end.isoformat()]
    observed = [date.fromisoformat(day) for day in sorted({row[1] for row in price_rows}) if end is None or day <= end.isoformat()]
    lower = start or (min(event_dates) if event_dates else None)
    upper = end or (max(observed + event_dates) if observed or event_dates else None)
    sessions = ()
    calendar_known = False
    if lower is not None and upper is not None and lower <= upper:
        if session_calendar is not None:
            sessions = tuple(day for day in session_calendar.session_dates if lower <= day <= upper)
            calendar_known = session_calendar.session_dates[0] <= lower and upper <= session_calendar.session_dates[-1]
            if not calendar_known:
                issue("calendar_out_of_range", "Calendar does not certify the entire requested window.", "calendar", a=lower, b=upper)
        else:
            issue("unknown_calendar", "Observed dates do not certify trading-session coverage.", "calendar", a=lower, b=upper)
    else:
        issue("empty_period", "No portfolio reporting period is available.")
    if calendar_known:
        boundaries = list(sessions)
        if not boundaries:
            issue("no_sessions", "No verified sessions fall inside the requested window.", "calendar", a=lower, b=upper)
    else:
        boundaries = sorted({day for day in observed + event_dates + [d for d in (lower, upper) if d is not None]
                             if lower is not None and upper is not None and lower <= day <= upper})
    selected_start = boundaries[0] if boundaries else None
    selected_end = boundaries[-1] if boundaries else None
    # Flow dates are additional valuation boundaries, never silently shifted to a session.
    if boundaries:
        flow_dates = connection.execute(
            "SELECT DISTINCT date FROM cash_flows WHERE portfolio_id = ? AND date > ? AND date < ?",
            (portfolio_id, selected_start.isoformat(), selected_end.isoformat()),
        ).fetchall()
        boundaries = sorted(set(boundaries) | {date.fromisoformat(row[0]) for row in flow_dates})
    valuations = _prepare_valuations(connection, portfolio_id, currency, boundaries, price_presence, issue)
    # Omitted starts may exclude only a valid unfunded prefix, never missing data.
    if start is None:
        skipped = 0
        while valuations and valuations[0].complete and valuations[0].total_equity <= 0:
            valuations.pop(0)
            skipped += 1
        if skipped:
            issue("unfunded_prefix", "Excluded initial non-positive-capital valuations.", category="methodology")
        boundaries = [item.date for item in valuations]
        selected_start = boundaries[0] if boundaries else None
        sessions = tuple(day for day in sessions if selected_start is not None and day >= selected_start)
    p_intervals = []
    for left, right in zip(valuations, valuations[1:]):
        if not left.complete or not right.complete:
            continue
        if left.total_equity <= 0:
            issue("nonpositive_baseline", "Return requires positive starting equity.", a=left.date, b=right.date)
            continue
        try:
            p_intervals.extend(get_portfolio_return_observations(
                connection, portfolio_id, [left.date, right.date], max_age_days=0, calendar=session_calendar,
                prepared_valuations=(left, right),
            ))
        except ValueError as error:
            if str(error) == "Simple returns must be finite and at least -100%":
                issue("invalid_return", str(error), a=left.date, b=right.date)
                continue
            if not str(error).startswith(("Incomplete price coverage", "Intermediate cash flows require")):
                raise
            issue("return_coverage", str(error), a=left.date, b=right.date)

    benchmark = connection.execute("SELECT currency FROM instruments WHERE ticker=?", (ticker,)).fetchone()
    benchmark_currency = benchmark[0] if benchmark else None
    b_intervals = ()
    b_dates = set()
    benchmark_valid = True
    if selected_start is not None and selected_end is not None:
        prices = get_prices(connection, ticker, selected_start.isoformat(), selected_end.isoformat())
        b_dates = {stamp.date() for stamp in prices.index}
        if prices.empty:
            issue("benchmark_unavailable", "No benchmark prices in the selected period.", "benchmark")
        elif not all(isfinite(float(x)) and x > 0 for x in prices.close):
            benchmark_valid = False
            issue("invalid_benchmark_prices", "Benchmark closes must be positive and finite.", "benchmark")
        elif len(prices) >= 3:
            b_intervals = get_benchmark_return_observations(connection, ticker, selected_start.isoformat(), selected_end.isoformat(), calendar=session_calendar)
        else:
            issue("insufficient_benchmark_observations", "Legacy benchmark producer requires at least three prices.", "benchmark")
    else:
        issue("benchmark_unavailable", "No selected analysis period.", "benchmark")
    if benchmark_currency is not None and benchmark_currency != currency:
        issue("benchmark_currency_difference", "Benchmark returns are local-currency returns, not FX-adjusted comparisons.", "comparison", "methodology")
    missing_p = tuple(day for day in sessions if not any(v.date == day and v.complete for v in valuations))
    missing_b = tuple(day for day in sessions if day not in b_dates)
    for scope, missing in (("portfolio", missing_p), ("benchmark", missing_b)):
        for day in missing:
            issue("missing_session", "Expected session has no valid valuation/price.", scope, a=day, b=day)
    p_keys = {(item.start_date, item.end_date) for item in p_intervals}
    b_keys = {(item.start_date, item.end_date) for item in b_intervals}
    for a, b in sorted(p_keys ^ b_keys):
        issue("unmatched_interval", "No equivalent interval on the other side.", "comparison", a=a, b=b)
    for scope, data in (("portfolio", p_intervals), ("benchmark", b_intervals)):
        for item in data:
            if item.trading_sessions != 1:
                issue("unverified_sessions" if item.trading_sessions is None else "not_one_session",
                      "Interval is not verified as one trading session.", scope, a=item.start_date, b=item.end_date)

    expected = len(sessions) if calendar_known else None
    expected_intervals = max(0, len(boundaries) - 1) if calendar_known else None

    def coverage(data, count, valid_count, missing):
        complete = (not missing and valid_count == count and len(data) == max(0, len(boundaries) - 1)
                    and bool(boundaries)) if calendar_known else None
        eligible = bool(complete and len(data) >= 2 and all(item.trading_sessions == 1 for item in data))
        return AnalysisCoverage(calendar_known, expected, expected_intervals, count, valid_count, len(data), missing, complete, eligible)

    pc = coverage(p_intervals, len(valuations), sum(v.complete for v in valuations), missing_p)
    bc = coverage(b_intervals, len(b_dates), len(b_dates) if benchmark_valid else 0, missing_b)
    if len(p_intervals) < 2:
        issue("insufficient_portfolio_observations", "At least two valid intervals are required for daily analytics.")
    if not event_dates:
        issue("empty_portfolio", "No portfolio events through the requested cutoff.")
    return PreparedAnalysis(
        portfolio_id, name, currency, ticker, benchmark_currency, float(confidence),
        AnalysisPeriod(start, end, selected_start, selected_end,
                       valuations[0].date if valuations and valuations[0].complete and valuations[0].total_equity > 0 else None,
                       p_intervals[0].end_date if p_intervals else None,
                       valuations[-1].date if valuations and valuations[-1].complete else None),
        tuple(valuations), tuple(p_intervals), tuple(b_intervals), pc, bc,
        pc.daily_eligible and bc.daily_eligible and p_keys == b_keys and benchmark_currency == currency,
        tuple(issues), AnalysisMethodology(session_calendar.source if session_calendar else None),
    )


def _prepare_valuations(connection, portfolio_id, currency, boundaries, price_presence, issue):
    if not boundaries:
        return []
    # A connection with write history cannot safely identify a rollback of
    # pre-existing writes. Keep those callers on their original SQL path.
    if connection.total_changes:
        return _valuation_rows(connection, portfolio_id, currency, boundaries, price_presence, issue, None)
    with PreparedValuationContext(connection, portfolio_id, boundaries) as context:
        return _valuation_rows(connection, portfolio_id, currency, boundaries, price_presence, issue, context)


def _valuation_rows(connection, portfolio_id, currency, boundaries, price_presence, issue, context):
    cutoff = boundaries[-1]
    integrity_ok = True
    if cutoff is not None:
        try:
            if context is None:
                validate_transactions(connection, portfolio_id, cutoff.isoformat())
            else:
                context.validate(connection, portfolio_id, cutoff.isoformat())
        except ValueError as error:
            if not str(error).startswith("Negative position detected"):
                raise
            integrity_ok = False
            issue("invalid_positions", str(error), category="integrity", b=cutoff)

    def currencies_ok(day):
        conflicts = connection.execute(
            "SELECT DISTINCT i.ticker FROM transactions t JOIN instruments i ON i.id=t.instrument_id "
            "WHERE t.portfolio_id=? AND t.date<=? AND (t.currency!=? OR i.currency!=?)",
            (portfolio_id, day.isoformat(), currency, currency),
        ).fetchall()
        flows = connection.execute(
            "SELECT 1 FROM cash_flows WHERE portfolio_id=? AND date<=? AND currency!=? LIMIT 1",
            (portfolio_id, day.isoformat(), currency),
        ).fetchone()
        for row in conflicts:
            issue("currency_conflict", "Transaction/instrument currency differs from portfolio; no FX conversion.", a=day, b=day, instrument=row[0])
        if flows:
            issue("currency_conflict", "Cash-flow currency differs from portfolio; no FX conversion.", a=day, b=day)
        return not conflicts and not flows

    valuations = []
    for day in boundaries:
        value = cash = contributions = None
        valid = integrity_ok and currencies_ok(day)
        if valid:
            # Split-aware positions, unlike the legacy raw-quantity coverage query.
            if context is None:
                prepared_positions = prepare_positions(connection, portfolio_id, day.isoformat())
                positions = prepared_positions.to_frame()
            else:
                positions = context.positions(connection, portfolio_id, day.isoformat())
            active = positions.loc[positions.quantity > 0, "ticker"].tolist()
            for symbol in active:
                if (normalise_ticker(symbol), day.isoformat()) not in price_presence:
                    valid = False
                    issue("missing_price", "Exact-date active-holding price is missing.", a=day, b=day, instrument=symbol)
            try:
                if context is None:
                    cash = _finite(get_cash_balance(connection, portfolio_id, day.isoformat()))
                    contributions = _finite(get_net_cash_flows(connection, portfolio_id, day.isoformat()))
                else:
                    raw_contributions, raw_cash = context.cash(connection, portfolio_id, day.isoformat())
                    cash = _finite(raw_cash)
                    contributions = _finite(raw_contributions)
                if valid:
                    if context is None:
                        frame = get_portfolio_value(connection, portfolio_id, day.isoformat(), max_age_days=0,
                                                    prepared_positions=prepared_positions)
                    else:
                        frame = context.value(connection, portfolio_id, day.isoformat())
                    for item in frame.market_value:
                        _finite(item)
                    # Preserve the engine's aggregation order after validating
                    # every component (pandas sum would otherwise skip NaNs).
                    value = _finite(frame.market_value.sum())
                    _finite(value + cash)
            except ValueError as error:
                message = str(error)
                if message.startswith(("Incomplete corporate action coverage", "Invalid coverage date range")):
                    issue("corporate_action_conflict", message, category="integrity", a=day, b=day)
                elif message == "Non-finite valuation":
                    issue("invalid_valuation", message, a=day, b=day)
                else:
                    raise
                valid = False
        valuations.append(AnalysisValuation(day, value if valid else None, cash, value + cash if valid else None, contributions, valid))
    return valuations
