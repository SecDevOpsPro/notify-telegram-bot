"""
Admin-only handlers.

Commands:
    /approve <user_id>   — approve a pending user
    /deny    <user_id>   — deny a pending user
    /pending             — list users awaiting approval
    /users               — list all approved users
    /brief [user_id]     — run today's daily report for one user right now

Inline callbacks:
    approve:<user_id>    — sent via the access-request notification
    deny:<user_id>       — sent via the access-request notification
"""

from __future__ import annotations

import functools
import html
import logging
from typing import Awaitable, Callable

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from notify_bot import config, db
from notify_bot.errors import format_error
from notify_bot.scheduler.jobs import send_user_report_now

logger = logging.getLogger(__name__)

_APPROVED_MSG = (
    "✅ Your access has been approved!\n"
    "Use /enroll to save your personal data (ID, licence, plate), "
    "then /help to see all available commands."
)

_NOT_AUTHORISED_MSG = "⛔ Not authorised."


def _is_admin(user_id: int) -> bool:
    return config.is_admin(user_id)


_Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def admin(handler: _Handler) -> _Handler:
    """Reject non-admins with a standard message before running *handler*."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_admin(update.effective_user.id):
            await update.message.reply_text(_NOT_AUTHORISED_MSG)
            return
        await handler(update, context)

    return wrapper


async def _notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    """Best-effort DM to a user; logs and ignores any error."""
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception:
        logger.warning("Could not DM user %s", user_id)


# ── Text commands ─────────────────────────────────────────────────────────────


@admin
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve <user_id>"""
    if not context.args:
        await update.message.reply_text("Usage: /approve <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    try:
        await db.set_user_status(target_id, "approved")
    except Exception as exc:
        logger.exception("Failed to approve user_id=%s", target_id)
        await update.message.reply_html(
            format_error(
                update.effective_user.id,
                "⚠️ Something went wrong while approving that user.",
                exc,
            )
        )
        return

    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> approved.", parse_mode="HTML"
    )
    await _notify_user(context, target_id, _APPROVED_MSG)


@admin
async def deny_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deny <user_id>"""
    if not context.args:
        await update.message.reply_text("Usage: /deny <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    try:
        await db.set_user_status(target_id, "denied")
    except Exception as exc:
        logger.exception("Failed to deny user_id=%s", target_id)
        await update.message.reply_html(
            format_error(
                update.effective_user.id,
                "⚠️ Something went wrong while denying that user.",
                exc,
            )
        )
        return

    await update.message.reply_text(f"❌ User <code>{target_id}</code> denied.", parse_mode="HTML")
    await _notify_user(context, target_id, "❌ Your access request was denied.")


@admin
async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pending — list users awaiting approval."""
    users = await db.list_users_by_status("pending")
    if not users:
        await update.message.reply_text("No pending access requests.")
        return

    lines = [
        f"• {html.escape(u['first_name'] or '')} "
        f"(@{html.escape(u.get('username') or 'N/A')}) — <code>{u['user_id']}</code>"
        for u in users
    ]
    await update.message.reply_html(
        "⏳ <b>Pending requests</b> (/approve &lt;id&gt; to approve):\n\n" + "\n".join(lines)
    )


@admin
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/users — list all approved users."""
    users = await db.list_users_by_status("approved")
    if not users:
        await update.message.reply_text("No approved users yet.")
        return

    lines = [
        f"• {html.escape(u['first_name'] or '')} "
        f"(@{html.escape(u.get('username') or 'N/A')}) — <code>{u['user_id']}</code>"
        for u in users
    ]
    await update.message.reply_html("✅ <b>Approved users:</b>\n\n" + "\n".join(lines))


@admin
async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/debug <user_id> — grant a user admin-level verbose error detail."""
    if not context.args:
        await update.message.reply_text("Usage: /debug <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    config.add_debug_user(target_id)
    await update.message.reply_text(f"🐛 User {target_id} now gets verbose error detail.")


@admin
async def undebug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/undebug <user_id> — revoke verbose error detail from a user."""
    if not context.args:
        await update.message.reply_text("Usage: /undebug <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    if config.remove_debug_user(target_id):
        await update.message.reply_text(f"🐛 User {target_id} no longer gets verbose error detail.")
    else:
        await update.message.reply_text(f"ℹ️ User {target_id} wasn't in the debug list.")


@admin
async def brief_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brief [user_id] — run today's daily report for one user right now.

    Defaults to the calling admin's own profile when no user_id is given.
    Reuses the same report-building logic as the scheduled daily job
    (``send_user_report_now``), so results match what that user would get
    at the scheduled report time — minus the inter-user stagger.
    """
    if context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
            return
    else:
        target_id = update.effective_user.id

    try:
        user = await db.get_user(target_id)
        profile = await db.get_profile(target_id)
    except Exception as exc:
        logger.exception("Failed to load user/profile for user_id=%s", target_id)
        await update.message.reply_html(
            format_error(
                update.effective_user.id,
                "⚠️ Something went wrong while loading that user's data.",
                exc,
            )
        )
        return

    if not user or user["status"] != "approved":
        await update.message.reply_text("❌ That user is not approved.")
        return
    if not profile or not (
        profile.get("national_id") or profile.get("driving_licence") or profile.get("vehicle_plate")
    ):
        await update.message.reply_text("❌ That user has no profile data saved.")
        return

    data = {
        "user_id": target_id,
        "first_name": user.get("first_name"),
        "national_id": profile.get("national_id"),
        "driving_licence": profile.get("driving_licence"),
        "vehicle_plate": profile.get("vehicle_plate"),
    }

    await update.message.reply_text(
        f"⏳ Running report for <code>{target_id}</code>…", parse_mode="HTML"
    )
    try:
        sent = await send_user_report_now(context, data)
    except Exception as exc:
        logger.exception("Failed to run report for user_id=%s", target_id)
        await update.message.reply_html(
            format_error(
                update.effective_user.id,
                "⚠️ Something went wrong while running that report.",
                exc,
            )
        )
        return

    if sent:
        await update.message.reply_text(
            f"✅ Report sent to <code>{target_id}</code>.", parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Nothing to report for <code>{target_id}</code> right now.", parse_mode="HTML"
        )


@admin
async def myip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/myip — show the public IP of the host running the bot."""
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
        await query.edit_message_text(_NOT_AUTHORISED_MSG)
        return

    try:
        action, target_id_str = query.data.split(":", 1)
        target_id = int(target_id_str)
    except (ValueError, AttributeError):
        await query.edit_message_text("⚠️ Malformed callback data.")
        return

    try:
        if action in ("approve", "deny"):
            await db.set_user_status(target_id, "approved" if action == "approve" else "denied")
    except Exception as exc:
        logger.exception("Failed to set status via callback for user_id=%s", target_id)
        await query.edit_message_text(
            format_error(
                update.effective_user.id,
                "⚠️ Something went wrong processing that action.",
                exc,
            ),
            parse_mode="HTML",
        )
        return

    if action == "approve":
        await query.edit_message_text(
            f"✅ Approved user <code>{target_id}</code>.", parse_mode="HTML"
        )
        await _notify_user(context, target_id, _APPROVED_MSG)
    elif action == "deny":
        await query.edit_message_text(
            f"❌ Denied user <code>{target_id}</code>.", parse_mode="HTML"
        )
        await _notify_user(context, target_id, "❌ Your access request was denied.")
    else:
        await query.edit_message_text("⚠️ Unknown action.")
