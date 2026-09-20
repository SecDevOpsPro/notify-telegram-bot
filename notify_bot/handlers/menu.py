"""
Phase-aware /help menu — inline keyboard buttons instead of a static command list.

- ``get_user_phase()`` resolves the caller's status (approved/enrolled/admin),
  the same way ``common.start()`` already does.
- ``build_help_keyboard()`` picks which button blocks to show for that phase.
- ``menu_callback()`` is the single CallbackQueryHandler for every "cmd:<name>"
  tap; it re-dispatches to the *same* handler function the matching /<name>
  command already uses, via a small Update stand-in that points
  `.message` / `.effective_message` at the tapped message (a real
  callback-query Update carries no top-level `.message`, which is what those
  handlers read from).

"cmd:enroll" is deliberately not in the dispatch table here — the /enroll
wizard is a ConversationHandler and needs PTB's own conversation tracking, so
it's wired as an extra entry point on that handler instead (see enroll.py).
"""

from __future__ import annotations

import logging
from typing import Literal, TypedDict, cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from notify_bot import config, db
from notify_bot.updates import HandlerCallback, require

logger = logging.getLogger(__name__)

_NOOP = "noop"


#: A user's DB status, or ``"unknown"`` for someone the bot has never seen.
type PhaseStatus = db.UserStatus | Literal["unknown"]


class MenuPhase(TypedDict):
    """Where the caller stands — decides which /help buttons they get."""

    status: PhaseStatus
    is_approved: bool
    has_profile: bool
    is_admin: bool


def _dispatch_table() -> dict[str, HandlerCallback[None]]:
    # Imported lazily (rather than at module load) to avoid a circular import:
    # common.py imports this module to build the /help keyboard, and some of
    # these handler modules import from common.py.
    from notify_bot.handlers.admin import brief_cmd, myip_cmd, pending_cmd, users_cmd
    from notify_bot.handlers.common import list_commands_command, request_access
    from notify_bot.handlers.enroll import unenroll_command
    from notify_bot.handlers.eur import eur_command
    from notify_bot.handlers.obligations import (
        clamp_command,
        driver_command,
        fines_command,
        gtp_command,
        mtpl_command,
        plate_command,
        sticker_command,
        vehicle_command,
        vignette_command,
    )

    return {
        "list_commands": list_commands_command,
        "request": request_access,
        "change": eur_command,
        "unenroll": unenroll_command,
        "driver": driver_command,
        "plate": plate_command,
        "vignette": vignette_command,
        "sticker": sticker_command,
        "clamp": clamp_command,
        "gtp": gtp_command,
        "mtpl": mtpl_command,
        "fines": fines_command,
        "vehicle": vehicle_command,
        "pending": pending_cmd,
        "users": users_cmd,
        "myip": myip_cmd,
        "brief": brief_cmd,
    }


# ── Phase resolution ─────────────────────────────────────────────────────────


async def get_user_phase(user_id: int) -> MenuPhase:
    """Resolve the caller's phase: approval status, enrollment, admin-ness."""
    record = await db.get_user(user_id)
    status: PhaseStatus = record["status"] if record else "unknown"
    is_approved = status == "approved"

    has_profile = False
    if is_approved:
        profile = await db.get_profile(user_id)
        has_profile = bool(
            profile
            and any(
                profile.get(field)
                for field in ("national_id", "driving_licence", "vehicle_plate", "talon_no")
            )
        )

    return {
        "status": status,
        "is_approved": is_approved,
        "has_profile": has_profile,
        "is_admin": config.is_admin(user_id),
    }


# ── Keyboard construction ────────────────────────────────────────────────────


def _header(text: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text, callback_data=_NOOP)]


def _row(*pairs: tuple[str, str]) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(label, callback_data=f"cmd:{cmd}") for label, cmd in pairs]


def all_commands_row() -> list[InlineKeyboardButton]:
    """The "📜 All commands" button — shared by /help's own keyboard and by
    /start, so every entry point offers a way to reach the full static
    command reference (see handlers/common.py's list_commands_command)."""
    return [InlineKeyboardButton("📜 All commands", callback_data="cmd:list_commands")]


def build_help_keyboard(phase: MenuPhase) -> InlineKeyboardMarkup:
    """Build the phase-appropriate inline keyboard shown under /help.

    Status (approved / enrolled) and role (admin) are independent axes: an
    admin who hasn't been DB-approved or hasn't run /enroll still gets the
    admin block, since admin commands are gated on `config.is_admin()` alone
    (see handlers/admin.py's `admin` decorator), not on approval/enrollment.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if not phase["is_approved"]:
        rows.append(_row(("📨 Request access", "request"), ("💶 Change (EUR)", "change")))
    elif not phase["has_profile"]:
        rows.append([InlineKeyboardButton("📝 Enroll your data", callback_data="cmd:enroll")])
        rows.append(_row(("💶 Change (EUR)", "change")))
    else:
        rows.append(_header("🚗 Vehicle checks"))
        rows.append(_row(("🚗 Driver", "driver"), ("🚙 Plate", "plate")))
        rows.append(_row(("🛣 Vignette", "vignette"), ("🅿 Sticker", "sticker")))
        rows.append(_row(("🔒 Clamp", "clamp"), ("🧾 GTP", "gtp")))
        rows.append(_row(("🛡 MTPL", "mtpl"), ("🚨 Fines", "fines")))
        rows.append([InlineKeyboardButton("🚗 Vehicle data", callback_data="cmd:vehicle")])
        rows.append(_header("⚙️ Account"))
        rows.append(_row(("💶 Change", "change"), ("🗑 Unenroll", "unenroll")))

    if phase["is_admin"]:
        rows.append(_header("🛠 Admin"))
        rows.append(_row(("✅ Pending", "pending"), ("👥 Users", "users")))
        rows.append(_row(("🌐 My IP", "myip"), ("📋 Brief", "brief")))

    rows.append(all_commands_row())

    return InlineKeyboardMarkup(rows)


# ── Callback dispatch ────────────────────────────────────────────────────────


class _ButtonUpdate:
    """Minimal Update stand-in so /command handlers can run from a button tap.

    Handlers reply via `.message` / `.effective_message`. For a real command
    update those hold the incoming message; for a callback-query update PTB
    leaves the top-level `.message` as None (the tapped message lives at
    `.callback_query.message` instead), so passing the raw Update straight
    into e.g. driver_command would crash on `update.message.reply_html`.
    Pointing both at the tapped message lets every existing handler be reused
    unmodified.
    """

    def __init__(self, update: Update, message: Message) -> None:
        self.effective_user = update.effective_user
        self.effective_chat = update.effective_chat
        self.message = message
        self.effective_message = message


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch every "cmd:<name>" button tap on the /help menu to its handler."""
    query = require(update.callback_query, "callback_query")
    await query.answer()

    _, _, action = (query.data or "").partition(":")
    handler = _dispatch_table().get(action)
    if handler is None:
        logger.warning("Unknown menu button callback_data=%s", query.data)
        return

    # An old enough message is reported by Telegram as inaccessible: it has an id
    # but no content, and none of the reply_* methods the handlers rely on.
    message = query.message
    if not isinstance(message, Message):
        logger.warning("Menu button tapped on an inaccessible message: %s", query.data)
        return

    context.args = []
    # _ButtonUpdate only implements the attributes handlers read (see its docstring),
    # so it is not a real Update — hence the cast at this one hand-off point.
    await handler(cast(Update, _ButtonUpdate(update, message)), context)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Section-header buttons are visual only — just clear the loading spinner."""
    await require(update.callback_query, "callback_query").answer()


def register(application: Application) -> None:
    """Register the menu's callback handlers.

    Must be added *after* the /enroll ConversationHandler so its own
    "cmd:enroll" entry point (a more specific match) claims that callback
    first — same ordering rule as the existing CommandHandlers.
    """
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^cmd:"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=f"^{_NOOP}$"))
