"""Central configuration — all settings read from environment variables."""

from __future__ import annotations

import os
from datetime import time

# ── Telegram ──────────────────────────────────────────────────────────────────
TOKEN: str = os.environ.get("TOKEN", "")

#: Telegram user ID of the bot owner / administrator.
ADMIN_TELEGRAM_ID: int = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

# ── Storage ───────────────────────────────────────────────────────────────────
DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "/app/data/bot.db")

# ── Scheduler ─────────────────────────────────────────────────────────────────
_raw_time: str = os.environ.get("DAILY_REPORT_TIME", "08:00")
try:
    _h, _m = _raw_time.split(":")
    #: UTC time at which the daily obligations report is sent to all approved users.
    DAILY_REPORT_TIME: time = time(int(_h), int(_m))
except (ValueError, TypeError) as _e:
    raise ValueError(
        f"Invalid DAILY_REPORT_TIME={_raw_time!r}. Expected HH:MM format (e.g. '08:00')."
    ) from _e

# ── MVR ───────────────────────────────────────────────────────────────────────

#: Session cookie for the MVR e-services API.  Rotate via env var when the
#: cookie expires (symptom: MVR checks return empty results or auth errors).
MVR_SESSION_ID: str = os.environ.get(
    "MVR_SESSION_ID", "b5345242-002d-4cca-878a-991c3db0cf0e"
)

# ── Proxy / Cloudflare bypass ──────────────────────────────────────────────────

#: Optional FlareSolverr base URL (e.g. ``http://flaresolverr:8191``).
#: When set, the Sofia Traffic service uses its REST API to bypass Cloudflare
#: challenges instead of making direct HTTP requests.
FLARESOLVERR_URL: str = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")
