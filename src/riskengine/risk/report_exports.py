"""Pure presentation of a validated ReportResponse; no I/O or financial engines."""
from collections import defaultdict
from html import escape
from typing import Literal

from riskengine.api.report_models import ReportResponse


def report_json(report: ReportResponse) -> str:
    """Preserve every field, null, default and observation at original precision."""
    return report.model_dump_json()


def report_filename(report: ReportResponse, format: Literal["json", "markdown"]) -> str:
    period = report.period
    start = period.requested_start_date.isoformat() if period.requested_start_date else "auto"
    end = period.requested_end_date.isoformat() if period.requested_end_date else "auto"
    cutoff = report.endpoint.valuation_date.isoformat() if report.endpoint.valuation_date else "undated"
    extension = {"json": "json", "markdown": "md"}[format]
    # Only typed IDs/dates and fixed tokens enter the attachment header.
    return f"portfolio-{report.portfolio_id}-report-{start}-to-{end}-asof-{cutoff}.{extension}"


def _cell(value):
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value) if value else "None recorded"
    text = escape(" ".join(str(value).split()), quote=False)
    for char in ("\\", "`", "*", "_", "[", "]", "|", "#"):
        text = text.replace(char, "\\" + char)
    return text


def _number(value, digits=2):
    return "Unavailable" if value is None else f"{value:,.{digits}f}"


def _unit(value, unit, difference=False):
    if value is None:
        return "Unavailable"
    if unit == "fraction":
        suffix = " pp" if difference else "%"
        return f"{value * 100:,.2f}{suffix}"
    if unit == "ratio":
        return f"{value:,.2f} (ratio)"
    raise ValueError(f"Unsupported metric unit: {unit}")


def _metric(metric, difference=False):
    if metric.value is None and metric.unavailable_reasons and all(
            reason.code == "not_applicable" for reason in metric.unavailable_reasons):
        return "Not applicable"
    return _unit(metric.value, metric.unit, difference)


def _table(lines, title, headings, rows):
    lines.extend([f"## {title}", "", "| " + " | ".join(headings) + " |",
                  "| " + " | ".join("---" for _ in headings) + " |"])
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    lines.append("")


def _date_range(days):
    days = sorted({day for day in days if day is not None})
    return f"{days[0]} to {days[-1]} ({len(days)} distinct dates)" if days else "None recorded"


def _diagnostics(document):
    """Retain section/metric context; collapse repeated dates, not their meaning.

    Exact individual dates remain in the complete JSON export. Time-series
    observations are not traversed or rendered as diagnostic text.
    """
    grouped = defaultdict(lambda: {"paths": set(), "dates": set()})

    def cutoff_context(paths):
        # Only explicit section paths establish context. Do not infer a date
        # from a ticker, a price observation, or a generic diagnostic scope.
        contexts = set()
        holdings, allocation = document["holdings"], document["allocation"]
        for path in paths:
            if path.startswith("holdings.") and holdings is not None:
                label, day = "holdings cutoff context", holdings["effective_valuation_date"]
            elif path.startswith("allocation.current.") and allocation is not None:
                label, day = "current allocation cutoff context", allocation["current"]["valuation_date"]
            elif path.startswith("allocation.reference.") and allocation is not None:
                label, day = "reference date context", allocation["reference"]["reference_date"]
            elif path.startswith("endpoint."):
                label, day = "endpoint cutoff context", document["endpoint"]["valuation_date"]
            else:
                continue
            if day is not None:
                contexts.add(f"{label}: {day}")
        return "; ".join(sorted(contexts))

    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                location = f"{path}.{key}" if path else key
                if key in {"warnings", "unavailable_reasons", "comparison_unavailable_reasons", "target_unavailable_reasons"}:
                    for reason in child:
                        identity = tuple(reason.get(field) for field in
                                         ("code", "message", "scope", "category", "metric", "ticker"))
                        group = grouped[identity]
                        group["paths"].add(location)
                        group["dates"].add((reason.get("start_date"), reason.get("end_date")))
                elif key not in {"valuations", "portfolio_intervals", "benchmark_intervals", "points"}:
                    walk(child, location)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(document, "")
    rows = []
    for (code, message, scope, category, metric, ticker), details in grouped.items():
        intervals = sorted((pair for pair in details["dates"] if any(pair)),
                           key=lambda pair: (pair[0] or "", pair[1] or ""))
        # Preserve single reason boundaries exactly; summarize multiple dated
        # occurrences with a count and range rather than hundreds of rows.
        dates = (f"{intervals[0][0] or 'Unavailable'} to {intervals[0][1] or 'Unavailable'}"
                 if len(intervals) == 1 else
                 f"{len(intervals)} dated occurrences; " + _date_range(day for pair in intervals for day in pair)
                 if intervals else "Date not supplied; see section cutoff")
        if not intervals:
            context = cutoff_context(details["paths"])
            if context:
                dates += f" ({context})"
        rows.append((code, message, scope or "Section-local", category or "Not specified",
                     metric or "Section-level", ticker or "Portfolio/benchmark", dates,
                     ", ".join(sorted(details["paths"]))))
    return rows


def report_markdown(report: ReportResponse) -> str:
    """Readable summary of supplied values; unit scaling is presentation only."""
    document = report.model_dump(mode="json")
    lines = ["# Portfolio Report", "",
             "Each download is freshly composed. Matching parameters and cutoff dates do not guarantee "
             "identity with a separately requested preview or another download.", "",
             "The filename's asof date is a reporting cutoff, not a snapshot identifier.", "",
             "Unavailable values are not zero. Monetary values are rounded to two decimal places for "
             "display only; the JSON export retains the complete document and original numeric precision.", ""]
    _table(lines, "Portfolio", ("Field", "Value"), [
        ("Name", report.name), ("Portfolio ID", report.portfolio_id), ("Currency", report.currency),
        ("Overall availability", {"ok": "Available (ok)", "partial": "Partial", "unavailable": "Unavailable"}[report.status]),
        ("Performance availability", report.performance.status), ("Risk availability", report.risk.status),
    ])
    dates = [(key.replace("_", " ").capitalize(), value if value is not None else
              "Automatic (not requested)" if key.startswith("requested_") else None)
             for key, value in document["period"].items()]
    dates.extend([("Exact endpoint cutoff", document["endpoint"]["valuation_date"]),
                  ("Historical allocation reference", document["allocation"]["reference"]["reference_date"]
                   if report.allocation is not None else None)])
    _table(lines, "Reporting dates", ("Field", "Date"), dates)
    endpoint_labels = {"total_equity": "Total equity", "invested_value": "Invested holdings value",
                       "cash_balance": "Cash balance", "net_contributions": "Net external contributions through cutoff",
                       "remaining_average_cost_basis": "Remaining average-cost basis"}
    _table(lines, "Financial summary", ("Amount", "Value", "Currency"), [
        (label, _number(getattr(report.endpoint, name).value), report.endpoint.currency)
        for name, label in endpoint_labels.items()])
    metric_labels = {"cumulative_return": "Cumulative return", "annualized_return": "Annualized return",
                     "annualized_volatility": "Annualized volatility", "sharpe_ratio": "Sharpe ratio",
                     "maximum_drawdown": "Maximum drawdown", "beta": "Beta versus selected benchmark",
                     "historical_var": "Historical VaR (loss convention)",
                     "historical_cvar": "Historical CVaR (loss convention)"}
    for title, response in (("Performance", report.performance), ("Risk", report.risk)):
        rows = []
        for name in type(response.portfolio_metrics).model_fields:
            portfolio, benchmark = getattr(response.portfolio_metrics, name), getattr(response.benchmark_metrics, name)
            rows.append((metric_labels[name], _metric(portfolio), _metric(benchmark),
                         portfolio.observations, benchmark.observations))
        _table(lines, title, ("Metric", "Portfolio", "Benchmark", "Portfolio observations", "Benchmark observations"), rows)
    _table(lines, "Benchmark comparison", ("Metric", "Value", "Observations"), [
        ("Excess cumulative return", _metric(report.performance.comparison.excess_cumulative_return, True),
         report.performance.comparison.excess_cumulative_return.observations),
        ("Excess annualized return", _metric(report.performance.comparison.excess_annualized_return, True),
         report.performance.comparison.excess_annualized_return.observations),
    ])
    lines.extend(["Comparison differences expressed as pp are percentage points; ratio-valued metrics remain ratios.", ""])
    if report.holdings is None:
        lines.extend(["## Holdings", "", "Unavailable; see diagnostics.", ""])
    else:
        _table(lines, "Holdings", ("Security", "Instrument", "Quantity", "Market value", "Remaining basis",
                                  "Unrealized price gain/loss", "Price date", "Price status", "Currency status"), [
            (h.ticker, h.instrument_name, _number(h.quantity, 8), _number(h.market_value),
             _number(h.remaining_average_cost_basis), _number(h.unrealized_gain_loss),
             h.effective_price_date, h.price_status, h.currency_status) for h in report.holdings.holdings])
        lines.extend([f"Holdings amounts use portfolio currency {_cell(report.currency)}; conflicted values remain unavailable.",
                      f"Cost basis method: {_cell(report.holdings.cost_basis_method)}.", ""])
    allocation = report.allocation
    if allocation is None:
        lines.extend(["## Allocation and concentration", "", "Unavailable; see diagnostics.", ""])
    else:
        reference = allocation.reference.snapshot
        _table(lines, "Allocation and concentration", ("Metric", "Current", "Historical starting allocation"), [
            ("Invested value", _number(allocation.current.invested_value), _number(reference.invested_value) if reference else None),
            ("HHI (index)", _number(allocation.current.concentration.hhi, 4), _number(reference.concentration.hhi, 4) if reference else None),
            ("Effective holdings (count)", _number(allocation.current.concentration.effective_holdings), _number(reference.concentration.effective_holdings) if reference else None),
            ("Largest position weight", _unit(allocation.current.concentration.largest_position_weight, allocation.weight_unit),
             _unit(reference.concentration.largest_position_weight, allocation.weight_unit) if reference else None),
        ])
        _table(lines, "Security allocation comparison", ("Security", "Current weight", "Historical starting weight", "Change"), [
            (row.ticker, _unit(row.current_weight, allocation.weight_unit), _unit(row.reference_weight, allocation.weight_unit),
             _unit(row.weight_change_fraction, "fraction", True)) for row in (allocation.changes or [])])
        if allocation.changes is None:
            lines.extend(["Comparison unavailable; see diagnostics. No missing weights are replaced by zero.", ""])
        _table(lines, "Current instrument classification exposures", ("Classification", "Market value", "Weight", "Holdings"), [
            (row.classification, _number(row.market_value), _unit(row.weight, allocation.weight_unit), row.holdings_count)
            for row in allocation.current.classification_exposures])
        lines.extend(["Allocation weights exclude cash. Historical starting weights are not investment-policy targets. "
                      "Instrument classifications are not underlying ETF holdings or look-through exposures.",
                      f"Classification basis: {_cell(allocation.classification_basis)}.",
                      f"Reference methodology: {_cell(allocation.reference.methodology)}.",
                      f"Reference provenance: {_cell(allocation.reference.provenance)}.", ""])
    coverage_rows = []
    for label, response in (("Performance", report.performance), ("Risk", report.risk)):
        for side in ("portfolio", "benchmark"):
            coverage = document[label.lower()][f"{side}_coverage"]
            for key, value in coverage.items():
                if key == "missing_sessions":
                    value = f"{len(value)} missing; {_date_range(value)}"
                coverage_rows.append((label, side, key, value))
    _table(lines, "Coverage", ("Section", "Series", "Field", "Value"), coverage_rows)
    series_rows = []
    for section in ("performance", "risk"):
        response = document[section]
        for key in ("valuations", "portfolio_intervals", "benchmark_intervals"):
            points = response[key]
            days = [day for point in points for day in (point.get("start_date"), point.get("date", point.get("end_date")))]
            unit = report.currency if key == "valuations" else response["methodology"]["return_unit"]
            series_rows.append((section, f"{key} ({unit})", len(points), _date_range(days)))
        chart_name = "wealth_index" if section == "performance" else "drawdown"
        for side, series in response[chart_name].items():
            series_rows.append((section, f"{chart_name}.{side} ({series['unit']})", len(series["points"]),
                                _date_range(point["date"] for point in series["points"])))
    _table(lines, "Time-series summary", ("Section", "Series and unit", "Observations", "Date coverage"), series_rows)
    lines.extend(["Individual observations and exact missing-session dates are retained in JSON. "
                  "A date range does not imply complete or uninterrupted coverage.", ""])
    diagnostic_rows = _diagnostics(document)
    _table(lines, "Availability reasons and diagnostics", ("Code", "Message", "Scope", "Category", "Metric", "Security", "Dates", "Locations"), diagnostic_rows)
    if not diagnostic_rows:
        lines.extend(["No diagnostics recorded.", ""])
    lines.extend(["Repeated dated diagnostics are summarized by count and range; exact occurrences remain in JSON. "
                  "Section/reference dates shown as context do not supply missing diagnostic dates or establish "
                  "corporate-action coverage intervals.", ""])
    for section in ("performance", "risk"):
        response = document[section]
        _table(lines, f"{section.capitalize()} methodology and conventions", ("Field", "Value"), [
            ("Benchmark ticker", response["benchmark_ticker"]), ("Benchmark currency", response["benchmark_currency"]),
            ("Confidence (probability)", _unit(response["confidence"], "fraction")),
            ("Comparison eligible", response["comparison_eligible"]),
            *response["methodology"].items(), *response["metric_conventions"].items(),
        ])
    lines.extend(["Benchmark returns use raw closing prices; dividends are not reinvested. "
                  "Comparisons are subject to the documented currency and interval eligibility checks; no FX conversion is performed.",
                  "Corporate-action validation is source-dependent. For active holdings valued with Tiingo raw closes, "
                  "the engine requires continuous stored Tiingo corporate-action coverage from the instrument's first "
                  "portfolio trade through the valuation cutoff. Other price sources use adjusted close and do not "
                  "undergo this same Tiingo coverage check; identical corporate-action coverage guarantees must not "
                  "be assumed across sources. Complete price coverage alone does not establish corporate-action coverage. "
                  "Latest price dates and recorded price sources are not evidence of certified action-coverage intervals. "
                  "This checks stored coverage records, not an independent audit of all corporate actions.",
                  "Risk metrics are historical estimates, not predictions. VaR/CVaR retain their loss convention, "
                  "including negative values for gains in the observed tail. Maximum drawdown signs follow the supplied conventions.",
                  "Legacy Attribution is excluded; no rebalancing recommendations or policy targets are inferred.", ""])
    provenance = dict(document["provenance"])
    provenance["persisted_snapshot_id"] = "None (no persisted snapshot)"
    _table(lines, "Provenance", ("Field", "Value"), provenance.items())
    lines.extend(["The latest stored portfolio price date describes data recency, not proof of complete valuation coverage.", ""])
    return "\n".join(lines)
