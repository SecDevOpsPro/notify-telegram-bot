"""Shared helper for surfacing errors to users — verbose for the admin, terse for everyone else."""

from __future__ import annotations

import html

from notify_bot import config


def format_error(user_id: int, base_message: str, exc: Exception) -> str:
    """
    Build an HTML-safe error message.

    The admin and any configured debug user (see ``config.DEBUG_USER_IDS``)
    get the base message plus the exception type/details; regular users only
    ever see the base message.
    """
    if config.is_debug_user(user_id):
        detail = html.escape(f"{type(exc).__name__}: {exc}")
        return f"{base_message}\n\n🛠 <code>{detail}</code>"
    return base_message
