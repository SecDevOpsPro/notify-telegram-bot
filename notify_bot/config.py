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
_h, _m = _raw_time.split(":")

#: UTC time at which the daily obligations report is sent to all approved users.
DAILY_REPORT_TIME: time = time(int(_h), int(_m))
