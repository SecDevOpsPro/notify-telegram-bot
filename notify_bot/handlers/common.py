"""Common handlers: /start, /help, /request."""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from notify_bot import config, db
from notify_bot.errors import format_error
from notify_bot.handlers import menu
from notify_bot.updates import require

logger = logging.getLogger(__name__)

_HELP_HEADER = "<b>📖 Help</b>"

_HELP_SUBTITLE_NOT_APPROVED = "This is a private bot — tap below to request access."
_HELP_SUBTITLE_NOT_ENROLLED = "✅ You're approved — enroll your data to unlock vehicle checks."
_HELP_SUBTITLE_ENROLLED = "Tap a button below to run a check."

# Admin is a role, independent of the admin's own approval/enrollment status
# above — shown whenever the caller is an admin, in every status variant.
_HELP_ADMIN_NOTE = (
    "\n\n🛠 <b>Admin tools</b> are available below, regardless of your own status.\n"
    "<i>Commands that take an ID argument stay text-only: "
    "/approve &lt;id&gt;, /deny &lt;id&gt;, /debug &lt;id&gt;, /undebug &lt;id&gt;.</i>"
)

# ── Full static command reference — "/help list-commands" or the "📜 All
# commands" button, as opposed to /help's own phase-appropriate button menu.

_LIST_COMMANDS_ARGS = {"list-commands", "list_commands", "listcommands"}

_ALL_COMMANDS_HEADER = "<b>📖 All Commands</b>"

_ALL_COMMANDS_PUBLIC = """
<b>Public commands</b> (no approval needed):
/start   — Welcome message
/help    — Phase-appropriate command menu
/help list-commands — This full command reference
/request — Ask the admin for access
/change  — EUR exchange rates (Cuba)

<b>After approval:</b>
/enroll   — Save your personal data (ID, licence, plate)
/unenroll — Delete your saved profile data
/driver   — Check driving licence obligations (MVR)
/plate    — Check vehicle obligations (MVR)
/vignette — Check road e-vignette (bgtoll.bg) — also: /vignette &lt;plate&gt;
/sticker  — Check Sofia parking sticker (sofiatraffic.bg) — also: /sticker &lt;plate&gt;
/clamp    — Check wheel-clamp status (sofiatraffic.bg) — also: /clamp &lt;plate&gt;
/gtp      — Check technical inspection validity — also: /gtp &lt;plate&gt;
/mtpl     — Check civil liability insurance — also: /mtpl &lt;plate&gt;
/fines    — Check traffic fines (KAT)
/vehicle  — Show vehicle registration data (plate + talon required)
"""

_ALL_COMMANDS_ADMIN = """
<b>Admin only:</b>
/approve &lt;id&gt;, /deny &lt;id&gt;, /pending, /users, /myip
/debug &lt;id&gt;, /undebug &lt;id&gt;
/brief [id]
"""


def _all_commands_text(is_admin: bool) -> str:
    text = _ALL_COMMANDS_HEADER + _ALL_COMMANDS_PUBLIC
    if is_admin:
        text += _ALL_COMMANDS_ADMIN
    return text


async def list_commands_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the full static command reference (every command, regardless of
    phase) — reused by "/help list-commands" and the "📜 All commands" button."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    await message.reply_html(_all_commands_text(config.is_admin(user.id)))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome the user and show their current access status."""
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    # Always register so the admin can see who contacted the bot
    try:
        await db.upsert_user(user.id, user.username, user.first_name)
        record = await db.get_user(user.id)
    except Exception as exc:
        logger.exception("Failed to register user_id=%s on /start", user.id)
        await message.reply_html(
            format_error(user.id, "⚠️ Something went wrong. Please try again.", exc)
        )
        return
    status = record["status"] if record else "unknown"
    reply_markup = None

    if status == "approved":
        try:
            profile = await db.get_profile(user.id)
        except Exception as exc:
            logger.exception("Failed to fetch profile for user_id=%s on /start", user.id)
            await message.reply_html(
                format_error(user.id, "⚠️ Something went wrong. Please try again.", exc)
            )
            return
        has_profile = bool(
            profile
            and any(
                profile.get(field)
                for field in ("national_id", "driving_licence", "vehicle_plate", "talon_no")
            )
        )
        if has_profile:
            msg = (
                f"👋 Hello, {user.first_name}!\n\n"
                "✅ You're approved.\n"
                "Tap a button below to run a check, or use /help to see all available commands."
            )
            reply_markup = menu.build_help_keyboard(
                {
                    "status": status,
                    "is_approved": True,
                    "has_profile": True,
                    "is_admin": config.is_admin(user.id),
                }
            )
        else:
            msg = (
                f"👋 Hello, {user.first_name}!\n\n"
                "✅ You're approved!\n"
                "Use /enroll (or tap the button below) to save your personal data "
                "(ID, licence, plate), then /help to see all available commands."
            )
            reply_markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📝 Enroll your data", callback_data="cmd:enroll")],
                    menu.all_commands_row(),
                ]
            )
    elif status == "pending":
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "⏳ Your access request is pending approval.\n"
            "You'll be notified here once the admin reviews it — "
            "then use /enroll to save your data."
        )
        reply_markup = InlineKeyboardMarkup([menu.all_commands_row()])
    elif status == "denied":
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "❌ Your access request was denied.\n"
            "Contact the bot owner if you think this is a mistake."
        )
        reply_markup = InlineKeyboardMarkup([menu.all_commands_row()])
    else:
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "This is a private bot.\n"
            "1️⃣ Use /request to ask the admin for access.\n"
            "2️⃣ Once approved, use /enroll to save your data."
        )
        reply_markup = InlineKeyboardMarkup([menu.all_commands_row()])

    await message.reply_text(msg, reply_markup=reply_markup)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the phase-appropriate command menu as tappable buttons.

    "/help list-commands" bypasses the phase menu and shows the full static
    command reference instead (see list_commands_command).
    """
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if context.args and context.args[0].lower() in _LIST_COMMANDS_ARGS:
        await list_commands_command(update, context)
        return

    phase = await menu.get_user_phase(user.id)

    if not phase["is_approved"]:
        subtitle = _HELP_SUBTITLE_NOT_APPROVED
    elif not phase["has_profile"]:
        subtitle = _HELP_SUBTITLE_NOT_ENROLLED
    else:
        subtitle = _HELP_SUBTITLE_ENROLLED

    text = f"{_HELP_HEADER}\n\n{subtitle}"
    if phase["is_admin"]:
        text += _HELP_ADMIN_NOTE

    await message.reply_html(text, reply_markup=menu.build_help_keyboard(phase))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for /commands that don't match any registered handler."""
    message = require(update.message, "message")
    await message.reply_text("❓ Unknown command. Use /help to see all available commands.")


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Let a user request access.  The admin receives a DM with inline
    Approve / Deny buttons.
    """
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    logger.info("Access request received from user %s (@%s)", user.id, user.username)

    try:
        await db.upsert_user(user.id, user.username, user.first_name)
        record = await db.get_user(user.id)
    except Exception as exc:
        logger.exception("Failed to register user_id=%s on /request", user.id)
        await message.reply_html(
            format_error(user.id, "⚠️ Something went wrong. Please try again.", exc)
        )
        return

    if record and record["status"] == "approved":
        await message.reply_text("✅ You already have access!  Use /help to get started.")
        return

    if config.ADMIN_TELEGRAM_ID == 0:
        logger.warning(
            "Access request from user %s but no ADMIN_TELEGRAM_ID is configured", user.id
        )
        await message.reply_text(
            "⚠️ No admin is configured for this bot.  Please contact the owner directly."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{user.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny:{user.id}"),
            ]
        ]
    )

    safe_name = html.escape(user.first_name or "")
    safe_username = html.escape(user.username) if user.username else "N/A"

    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_TELEGRAM_ID,
            text=(
                f"🔔 <b>New access request</b>\n\n"
                f"Name:     {safe_name}\n"
                f"Username: @{safe_username}\n"
                f"User ID:  <code>{user.id}</code>"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.exception(
            "Could not notify admin (id=%s) of access request from user %s",
            config.ADMIN_TELEGRAM_ID,
            user.id,
        )
        await message.reply_html(
            format_error(
                user.id,
                "⚠️ Could not reach the admin right now.  Please try again later.",
                exc,
            )
        )
        return

    try:
        await db.set_user_status(user.id, "pending")
    except Exception as exc:
        logger.exception("Failed to set pending status for user_id=%s", user.id)
        await message.reply_html(
            format_error(
                user.id,
                "⚠️ Your request reached the admin but we couldn't update your status. "
                "Contact the admin if you don't hear back.",
                exc,
            )
        )
        return

    logger.info("Access request from user %s forwarded to admin", user.id)
    await message.reply_text(
        "📨 Your request has been sent to the admin.\n"
        "You'll receive a message here once it's reviewed."
    )
