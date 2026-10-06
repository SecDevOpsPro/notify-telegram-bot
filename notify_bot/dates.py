"""
Shared parsing and formatting for the date strings returned by upstream APIs.

The Bulgarian services (boleron.bg, bgtoll.bg) mix ``dd.mm.yyyy [HH:MM[:SS]]``
display strings (sometimes with a trailing ``г.``/``ч.``) with ISO-8601
timestamps, depending on which field the payload carries.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from babel.dates import format_timedelta

DISPLAY_DATE_FORMAT = "%d.%m.%Y"

#: Babel locale for relative dates ("in 4 days") — matches the bot's English messages.
DISPLAY_LOCALE = "en"

#: Days remaining threshold below which an expiry warning is shown.
EXPIRY_WARN_DAYS = 14

_BG_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y")
_BG_UNIT_SUFFIX_RE = re.compile(r"\s*[гч]\.")  # "15.12.2025г." / "23:59ч."


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an API date string into a naive ``datetime``; ``None`` if unrecognized.

    Accepts ISO-8601 (``2025-12-17T00:00:00.000``, ``2026-01-01``) and the
    Bulgarian ``dd.mm.yyyy [HH:MM[:SS]]`` form, with or without ``г.``/``ч.``
    suffixes. Timezone info is dropped — the upstream times are local.
    """
    if not value:
        return None
    text = _BG_UNIT_SUFFIX_RE.sub("", value).strip()
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in _BG_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_date(value: datetime) -> str:
    """Render *value* as the ``dd.mm.yyyy`` date used in bot messages."""
    return value.strftime(DISPLAY_DATE_FORMAT)


def days_until(value: str | None) -> int | None:
    """Days from today until *value* (any :func:`parse_datetime` format); ``None`` if unparsed."""
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return (parsed.date() - date.today()).days


def expiry_warning(value: str | None) -> str | None:
    """``⚠️ Expires in N days!`` if *value* is under :data:`EXPIRY_WARN_DAYS` away, else None.

    The last day reads ``⚠️ Expires today!`` — Babel has no "today" form for a
    zero-length delta and would say "in 0 days".
    """
    remaining = days_until(value)
    if remaining is None or not 0 <= remaining < EXPIRY_WARN_DAYS:
        return None
    if remaining == 0:
        return "⚠️ Expires today!"
    when = format_timedelta(
        timedelta(days=remaining),
        granularity="day",
        threshold=EXPIRY_WARN_DAYS,  # keep "13 days", don't round up to "2 weeks"
        add_direction=True,
        locale=DISPLAY_LOCALE,
    )
    return f"⚠️ Expires {when}!"
