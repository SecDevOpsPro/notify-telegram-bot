"""Common handlers: /start, /help, /request."""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from notify_bot import config, db
from notify_bot.errors import format_error
from notify_bot.handlers import menu

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome the user and show their current access status."""
    user = update.effective_user
    if not user:
        return

    # Always register so the admin can see who contacted the bot
    try:
        await db.upsert_user(user.id, user.username, user.first_name)
        record = await db.get_user(user.id)
    except Exception as exc:
        logger.exception("Failed to register user_id=%s on /start", user.id)
        await update.message.reply_html(
            format_error(user.id, "⚠️ Something went wrong. Please try again.", exc)
        )
        return
    status = record["status"] if record else "unknown"

    if status == "approved":
        try:
            profile = await db.get_profile(user.id)
        except Exception as exc:
            logger.exception("Failed to fetch profile for user_id=%s on /start", user.id)
            await update.message.reply_html(
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
                "Use /help to see all available commands."
            )
        else:
            msg = (
                f"👋 Hello, {user.first_name}!\n\n"
                "✅ You're approved!\n"
                "Use /enroll to save your personal data (ID, licence, plate), "
                "then /help to see all available commands."
            )
    elif status == "pending":
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "⏳ Your access request is pending approval.\n"
            "You'll be notified here once the admin reviews it — "
            "then use /enroll to save your data."
        )
    elif status == "denied":
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "❌ Your access request was denied.\n"
            "Contact the bot owner if you think this is a mistake."
        )
    else:
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "This is a private bot.\n"
            "1️⃣ Use /request to ask the admin for access.\n"
            "2️⃣ Once approved, use /enroll to save your data."
        )

    await update.message.reply_text(msg)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the phase-appropriate command menu as tappable buttons."""
    user = update.effective_user
    if not user:
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

    await update.effective_message.reply_html(text, reply_markup=menu.build_help_keyboard(phase))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for /commands that don't match any registered handler."""
    await update.message.reply_text("❓ Unknown command. Use /help to see all available commands.")


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
