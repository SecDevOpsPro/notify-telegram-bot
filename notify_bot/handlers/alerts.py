"""
/alerts — choose what the daily report includes.

The first screen picks what to configure (the personal checks or one vehicle);
the second toggles that choice's checks.  When only one choice applies, the
first screen is skipped.  Everything is on until the user turns it off, and
turning a check off also stops the report from calling its API.

Callbacks: "alerts:menu", "alerts:open:<scope>" and "alerts:t:<scope>:<check>",
where the scope is a plate, or empty for the personal checks.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, ContextTypes

from notify_bot import db
from notify_bot.errors import format_error
from notify_bot.middlewares import require_approved
from notify_bot.report_checks import (
    LABELS,
    PERSONAL_SCOPE,
    ReportCheck,
    personal_checks,
    vehicle_checks,
)
from notify_bot.updates import require

__all__ = ["alerts_command", "build_alerts_button_handler"]

logger = logging.getLogger(__name__)

type _Screen = tuple[str, InlineKeyboardMarkup | None]

_NOTHING_TO_CONFIGURE = (
    "🔔 <b>Alerts</b>\n\nNothing to configure yet — save your data with /enroll first."
)


async def _load_options(uid: int) -> dict[str, tuple[ReportCheck, ...]]:
    """What the user can configure: each scope and its checks, personal first."""
    profile = await db.get_profile(uid)
    national_id = profile["national_id"] if profile else None
    licence = profile["driving_licence"] if profile else None

    options: dict[str, tuple[ReportCheck, ...]] = {}
    if personal := personal_checks(national_id, licence):
        options[PERSONAL_SCOPE] = personal
    for vehicle in await db.list_vehicles(uid):
        options[vehicle["plate"]] = vehicle_checks(national_id)
    return options


def _scope_label(scope: str) -> str:
    return "👤 Personal" if scope == PERSONAL_SCOPE else f"🚗 {scope}"


def _menu_screen(
    options: dict[str, tuple[ReportCheck, ...]], disabled: dict[str, set[ReportCheck]]
) -> _Screen:
    rows = []
    for scope, checks in options.items():
        enabled = sum(check not in disabled.get(scope, set()) for check in checks)
        label = f"{_scope_label(scope)} · {enabled}/{len(checks)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"alerts:open:{scope}")])
    text = "🔔 <b>Alerts</b>\n\nWhat do you want to configure in your daily report?"
    return text, InlineKeyboardMarkup(rows)


def _scope_screen(
    scope: str,
    options: dict[str, tuple[ReportCheck, ...]],
    disabled: dict[str, set[ReportCheck]],
) -> _Screen:
    off = disabled.get(scope, set())
    buttons = [
        InlineKeyboardButton(
            f"{'❌' if check in off else '✅'} {LABELS[check]}",
            callback_data=f"alerts:t:{scope}:{check}",
        )
        for check in options[scope]
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if len(options) > 1:
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="alerts:menu")])
    text = f"{_scope_label(scope)}\n\nChoose what to include in your daily report:"
    return text, InlineKeyboardMarkup(rows)


def _first_screen(
    options: dict[str, tuple[ReportCheck, ...]], disabled: dict[str, set[ReportCheck]]
) -> _Screen:
    if not options:
        return _NOTHING_TO_CONFIGURE, None
    if len(options) == 1:
        return _scope_screen(next(iter(options)), options, disabled)
    return _menu_screen(options, disabled)


@require_approved
async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alerts — open the daily report settings."""
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id
    try:
        options = await _load_options(uid)
        disabled = await db.list_opt_outs(uid)
    except Exception as exc:
        logger.exception("Failed to load alert settings for user_id=%s", uid)
        await message.reply_html(
            format_error(uid, "⚠️ Something went wrong. Please try /alerts again.", exc)
        )
        return
    text, keyboard = _first_screen(options, disabled)
    await message.reply_html(text, reply_markup=keyboard)


@require_approved
async def alerts_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a tap on either /alerts screen, then redraw the message."""
    query = require(update.callback_query, "callback_query")
    uid = require(update.effective_user, "effective_user").id
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    scope = parts[2] if len(parts) > 2 else PERSONAL_SCOPE
    check = parts[3] if len(parts) > 3 else ""

    note: str | None = None
    try:
        options = await _load_options(uid)
        disabled = await db.list_opt_outs(uid)
        if action == "menu":
            text, keyboard = _first_screen(options, disabled)
        elif action in ("open", "t") and scope not in options:
            # Removed since this message was sent — e.g. a vehicle, via /vehicles.
            note = "That's no longer available."
            text, keyboard = _first_screen(options, disabled)
        elif action == "open":
            text, keyboard = _scope_screen(scope, options, disabled)
        elif action == "t" and check in options[scope]:
            enable = check in disabled.get(scope, set())
            await db.set_check_enabled(uid, scope, check, enabled=enable)
            disabled = await db.list_opt_outs(uid)
            text, keyboard = _scope_screen(scope, options, disabled)
        else:
            logger.warning("Unknown alerts button callback_data=%s", query.data)
            await query.answer()
            return
    except Exception as exc:
        logger.exception("Alerts %s failed for user_id=%s", action, uid)
        await query.answer()
        await require(update.effective_message, "effective_message").reply_html(
            format_error(uid, "⚠️ Something went wrong. Please try /alerts again.", exc)
        )
        return

    await query.answer(note)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest:
        # Message too old to edit, or unchanged — the toggle itself was still saved.
        logger.debug("Could not refresh the /alerts message", exc_info=True)


def build_alerts_button_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(alerts_button, pattern=r"^alerts:")
