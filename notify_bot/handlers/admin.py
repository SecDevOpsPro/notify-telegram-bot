"""
Admin-only handlers.

Commands:
    /approve <user_id>   — approve a pending user
    /deny    <user_id>   — deny a pending user
    /pending             — list users awaiting approval
    /users               — list all approved users

Inline callbacks:
    approve:<user_id>    — sent via the access-request notification
    deny:<user_id>       — sent via the access-request notification
"""

from __future__ import annotations

import logging

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from notify_bot import config, db

logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return config.ADMIN_TELEGRAM_ID != 0 and user_id == config.ADMIN_TELEGRAM_ID


async def _notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    """Best-effort DM to a user; logs and ignores any error."""
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception:
        logger.warning("Could not DM user %s", user_id)


# ── Text commands ─────────────────────────────────────────────────────────────


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve <user_id>"""
    if not _is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    await db.set_user_status(target_id, "approved")
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> approved.", parse_mode="HTML"
    )
    await _notify_user(
        context, target_id, "✅ Your access has been approved!  Use /help to get started."
    )


async def deny_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deny <user_id>"""
    if not _is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /deny <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    await db.set_user_status(target_id, "denied")
    await update.message.reply_text(f"❌ User <code>{target_id}</code> denied.", parse_mode="HTML")
    await _notify_user(context, target_id, "❌ Your access request was denied.")


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pending — list users awaiting approval."""
    if not _is_admin(update.effective_user.id):
        return

    users = await db.list_users_by_status("pending")
    if not users:
        await update.message.reply_text("No pending access requests.")
        return

    lines = [
        f"• {u['first_name']} (@{u.get('username') or 'N/A'}) — <code>{u['user_id']}</code>"
        for u in users
    ]
    await update.message.reply_html(
        "⏳ <b>Pending requests</b> (/approve &lt;id&gt; to approve):\n\n" + "\n".join(lines)
    )


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/users — list all approved users."""
    if not _is_admin(update.effective_user.id):
        return

    users = await db.list_users_by_status("approved")
    if not users:
        await update.message.reply_text("No approved users yet.")
        return

    lines = [
        f"• {u['first_name']} (@{u.get('username') or 'N/A'}) — <code>{u['user_id']}</code>"
        for u in users
    ]
    await update.message.reply_html("✅ <b>Approved users:</b>\n\n" + "\n".join(lines))


async def myip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myip — show the public IP of the host running the bot."""
    if not _is_admin(update.effective_user.id):
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://api.ipify.org")
            resp.raise_for_status()
            ip = resp.text.strip()
        await update.message.reply_text(f"🌐 Public IP: <code>{ip}</code>", parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to fetch public IP: %s", exc)
        await update.message.reply_text("⚠️ Could not determine public IP.")


# ── Inline callback ───────────────────────────────────────────────────────────


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the Approve / Deny inline buttons from access-request notifications."""
    query = update.callback_query
    await query.answer()

    if not _is_admin(update.effective_user.id):
        await query.edit_message_text("⛔ Not authorised.")
        return

    try:
        action, target_id_str = query.data.split(":", 1)
        target_id = int(target_id_str)
    except (ValueError, AttributeError):
        await query.edit_message_text("⚠️ Malformed callback data.")
        return

    if action == "approve":
        await db.set_user_status(target_id, "approved")
        await query.edit_message_text(
            f"✅ Approved user <code>{target_id}</code>.", parse_mode="HTML"
        )
        await _notify_user(
            context, target_id, "✅ Your access has been approved!  Use /help to get started."
        )
    elif action == "deny":
        await db.set_user_status(target_id, "denied")
        await query.edit_message_text(
            f"❌ Denied user <code>{target_id}</code>.", parse_mode="HTML"
        )
        await _notify_user(context, target_id, "❌ Your access request was denied.")
    else:
        await query.edit_message_text("⚠️ Unknown action.")
