"""Common handlers: /start, /help, /request."""

from __future__ import annotations

import html
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
/gtp      — Check technical inspection validity — also: /gtp &lt;plate&gt;
/mtpl     — Check civil liability insurance — also: /mtpl &lt;plate&gt;
/fines    — Check traffic fines (KAT)
/vehicle  — Show vehicle registration data (plate + talon required)

<b>Admin only:</b>
/approve &lt;id&gt;, /deny &lt;id&gt;, /pending, /users, /myip
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
        profile = await db.get_profile(user.id)
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
    """Display the full command reference."""
    await update.message.reply_html(_HELP_PUBLIC)


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

    await db.upsert_user(user.id, user.username, user.first_name)
    record = await db.get_user(user.id)

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
    except Exception:
        logger.exception(
            "Could not notify admin (id=%s) of access request from user %s",
            config.ADMIN_TELEGRAM_ID,
            user.id,
        )
        await message.reply_text(
            "⚠️ Could not reach the admin right now.  Please try again later."
        )
        return

    await db.set_user_status(user.id, "pending")
    logger.info("Access request from user %s forwarded to admin", user.id)
    await message.reply_text(
        "📨 Your request has been sent to the admin.\n"
        "You'll receive a message here once it's reviewed."
    )
