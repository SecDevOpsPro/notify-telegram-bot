"""
Shared parsing and formatting for the date strings returned by upstream APIs.

The Bulgarian services (boleron.bg, bgtoll.bg) mix ``dd.mm.yyyy [HH:MM[:SS]]``
display strings (sometimes with a trailing ``г.``/``ч.``) with ISO-8601
timestamps, depending on which field the payload carries.
"""

from __future__ import annotations

import re
from datetime import datetime

DISPLAY_DATE_FORMAT = "%d.%m.%Y"

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
