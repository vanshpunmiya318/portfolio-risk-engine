"""Pure, pinned XNYS session adapter; never obtains sessions from market prices.

Rules are shipped by exchange_calendars and operate offline. Source JSON records
the actual session coverage and a SHA-256 of ISO dates joined by LF, with a final
LF. Early closes are one session; UTC session labels are dates, not instants to
convert into America/New_York. Financial calculations and DB ownership stay
with the existing analysis pipeline.
"""
from datetime import date, datetime
from hashlib import sha256
from importlib.metadata import version
import json

import exchange_calendars

from riskengine.risk.return_intervals import VerifiedSessionCalendar


PROVIDER_VERSION = "4.13.2"


def _date(value: date | str) -> date:
    if isinstance(value, str):
        try:
            result = date.fromisoformat(value)
            if result.isoformat() == value:
                return result
        except ValueError:
            pass
    elif isinstance(value, date) and not isinstance(value, datetime):
        return value
    raise ValueError("Calendar boundaries must be native dates or YYYY-MM-DD strings")


def verified_nyse_calendar(start: date | str, end: date | str) -> VerifiedSessionCalendar:
    """Return verified sessions covering inclusive bounds, including edge padding.

    Construct a calendar with explicit year bounds, then retain the session on
    or before start and on or after end. This brackets non-session requests for
    prepare_analysis's coverage check without inventing weekend/holiday sessions.
    There are no clock-based defaults, DB operations, network calls or fallback
    rules. Provider errors fail closed rather than supplying an inferred calendar.
    """
    start_date, end_date = _date(start), _date(end)
    if end_date < start_date:
        raise ValueError("Calendar end must not precede start")
    if start_date.year == 1 or end_date.year == 9999:
        raise ValueError("Calendar boundaries must permit one year of construction padding")
    provider_version = version("exchange_calendars")
    if provider_version != PROVIDER_VERSION:
        raise RuntimeError(f"Expected exchange_calendars=={PROVIDER_VERSION}; found {provider_version}")
    calendar = exchange_calendars.get_calendar(
        "XNYS", start=date(start_date.year - 1, 1, 1).isoformat(),
        end=date(end_date.year + 1, 12, 31).isoformat(),
    )
    first = calendar.date_to_session(start_date.isoformat(), direction="previous")
    last = calendar.date_to_session(end_date.isoformat(), direction="next")
    sessions = tuple(label.date() for label in calendar.sessions_in_range(first, last))
    content = "".join(day.isoformat() + "\n" for day in sessions).encode("ascii")
    source = json.dumps({
        "provider": "exchange_calendars",
        "version": provider_version,
        "calendar": "XNYS",
        "requested_start": start_date.isoformat(),
        "requested_end": end_date.isoformat(),
        "coverage_start": sessions[0].isoformat(),
        "coverage_end": sessions[-1].isoformat(),
        "session_count": len(sessions),
        "session_checksum": "sha256:" + sha256(content).hexdigest(),
        "checksum_format": "ISO8601 dates, LF-separated, trailing LF, ASCII",
        "tzdata_version": version("tzdata"),
    }, sort_keys=True, separators=(",", ":"))
    return VerifiedSessionCalendar(sessions, source)
