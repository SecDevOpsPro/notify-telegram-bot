"""Common handlers: /start, /help, /request."""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from notify_bot import config, db

logger = logging.getLogger(__name__)

_HELP_PUBLIC = """
<b>📖 Help</b>

<b>Public commands</b> (no approval needed):
/start   — Welcome message
/help    — Show this message
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

<b>Admin only:</b>
/approve &lt;id&gt;, /deny &lt;id&gt;, /pending, /users
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome the user and show their current access status."""
    user = update.effective_user
    if not user:
        return

    # Always register so the admin can see who contacted the bot
    await db.upsert_user(user.id, user.username, user.first_name)
    record = await db.get_user(user.id)
    status = record["status"] if record else "unknown"

    if status == "approved":
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "✅ You're approved.\n"
            "Use /help to see all available commands."
        )
    elif status == "pending":
        msg = (
            f"👋 Hello, {user.first_name}!\n\n"
            "⏳ Your access request is pending approval.\n"
            "You'll be notified when the admin reviews it."
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
            "Use /request to ask the admin for access."
        )

    await update.message.reply_text(msg)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the full command reference."""
    await update.message.reply_html(_HELP_PUBLIC)


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Let a user request access.  The admin receives a DM with inline
    Approve / Deny buttons.
    """
    user = update.effective_user
    if not user:
        return

    await db.upsert_user(user.id, user.username, user.first_name)
    record = await db.get_user(user.id)

    if record and record["status"] == "approved":
        await update.message.reply_text("✅ You already have access!  Use /help to get started.")
        return

    if config.ADMIN_TELEGRAM_ID == 0:
        await update.message.reply_text(
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

    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_TELEGRAM_ID,
            text=(
                f"🔔 <b>New access request</b>\n\n"
                f"Name:     {user.first_name}\n"
                f"Username: @{user.username or 'N/A'}\n"
                f"User ID:  <code>{user.id}</code>"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Could not reach admin (id=%s)", config.ADMIN_TELEGRAM_ID)
        await update.message.reply_text(
            "⚠️ Could not reach the admin right now.  Please try again later."
        )
        return

    await db.set_user_status(user.id, "pending")
    await update.message.reply_text(
        "📨 Your request has been sent to the admin.\n"
        "You'll receive a message here once it's reviewed."
    )
