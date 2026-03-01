"""
Enrollment ConversationHandler wizard.

Guides an approved user through saving (or updating) their:
  1. National ID (EGN — 10 digits)
  2. Driving licence number
  3. Vehicle plate number

Each step shows the current stored value and offers /skip to keep it.
/cancel exits the wizard at any point.
"""
from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from notify_bot import db
from notify_bot.middlewares import require_approved

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────

ASK_NATIONAL_ID, ASK_LICENCE, ASK_PLATE = range(3)

# ── Validation patterns ───────────────────────────────────────────────────────

_EGN_RE = re.compile(r"^\d{10}$")
_LICENCE_RE = re.compile(r"^\d{5,12}$")
_PLATE_RE = re.compile(r"^[A-Z]{1,3}\d{3,4}[A-Z]{0,3}$", re.IGNORECASE)


# ── Entry point ───────────────────────────────────────────────────────────────


async def enroll_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the enrollment wizard — ask for National ID."""
    profile = await db.get_profile(update.effective_user.id)
    current = profile.get("national_id") or "—" if profile else "—"

    await update.message.reply_html(
        "📋 <b>Enrollment Wizard</b> — Step 1 of 3\n\n"
        f"Current National ID: <code>{current}</code>\n\n"
        "Please enter your <b>National ID (EGN)</b> — 10 digits.\n"
        "Send /skip to keep the current value, or /cancel to quit."
    )
    return ASK_NATIONAL_ID


# ── Step 1: National ID ───────────────────────────────────────────────────────


async def received_national_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not _EGN_RE.match(text):
        await update.message.reply_text(
            "❌ Invalid EGN — must be exactly 10 digits.  Try again or /skip."
        )
        return ASK_NATIONAL_ID

    context.user_data["enroll_national_id"] = text
    return await _ask_licence(update, context)


async def skip_national_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("enroll_national_id", None)
    return await _ask_licence(update, context)


async def _ask_licence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await db.get_profile(update.effective_user.id)
    current = profile.get("driving_licence") or "—" if profile else "—"

    await update.message.reply_html(
        "📋 <b>Enrollment Wizard</b> — Step 2 of 3\n\n"
        f"Current Driving Licence: <code>{current}</code>\n\n"
        "Please enter your <b>Driving Licence number</b> (digits only).\n"
        "Send /skip to keep the current value, or /cancel to quit."
    )
    return ASK_LICENCE


# ── Step 2: Driving licence ───────────────────────────────────────────────────


async def received_licence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not _LICENCE_RE.match(text):
        await update.message.reply_text(
            "❌ Invalid licence number (5–12 digits).  Try again or /skip."
        )
        return ASK_LICENCE

    context.user_data["enroll_licence"] = text
    return await _ask_plate(update, context)


async def skip_licence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("enroll_licence", None)
    return await _ask_plate(update, context)


async def _ask_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = await db.get_profile(update.effective_user.id)
    current = profile.get("vehicle_plate") or "—" if profile else "—"

    await update.message.reply_html(
        "📋 <b>Enrollment Wizard</b> — Step 3 of 3\n\n"
        f"Current Vehicle Plate: <code>{current}</code>\n\n"
        "Please enter your <b>vehicle plate</b> (e.g. <code>CB1234AB</code>).\n"
        "Send /skip to keep the current value, or /cancel to quit."
    )
    return ASK_PLATE


# ── Step 3: Vehicle plate ─────────────────────────────────────────────────────


async def received_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().upper()
    if not _PLATE_RE.match(text):
        await update.message.reply_text(
            "❌ Invalid plate format (e.g. CB1234AB).  Try again or /skip."
        )
        return ASK_PLATE

    context.user_data["enroll_plate"] = text
    return await _save_and_confirm(update, context)


async def skip_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("enroll_plate", None)
    return await _save_and_confirm(update, context)


# ── Confirmation & save ───────────────────────────────────────────────────────


async def _save_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    national_id = context.user_data.pop("enroll_national_id", None)
    licence = context.user_data.pop("enroll_licence", None)
    plate = context.user_data.pop("enroll_plate", None)

    await db.upsert_profile(
        uid,
        national_id=national_id,
        driving_licence=licence,
        vehicle_plate=plate,
    )

    profile = await db.get_profile(uid)
    await update.message.reply_html(
        "✅ <b>Profile saved!</b>\n\n"
        f"National ID:      <code>{profile.get('national_id') or '—'}</code>\n"
        f"Driving Licence:  <code>{profile.get('driving_licence') or '—'}</code>\n"
        f"Vehicle Plate:    <code>{profile.get('vehicle_plate') or '—'}</code>\n\n"
        "Use /driver to check driving licence obligations.\n"
        "Use /plate to check vehicle obligations."
    )
    return ConversationHandler.END


# ── Cancel ────────────────────────────────────────────────────────────────────


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for key in ("enroll_national_id", "enroll_licence", "enroll_plate"):
        context.user_data.pop(key, None)
    await update.message.reply_text("Enrollment cancelled.  Your existing data is unchanged.")
    return ConversationHandler.END


# ── Handler factory ───────────────────────────────────────────────────────────


def build_enroll_handler() -> ConversationHandler:
    """
    Build and return the fully configured ConversationHandler for /enroll.
    Must be registered *before* any plain CommandHandlers.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("enroll", require_approved(enroll_start))],
        states={
            ASK_NATIONAL_ID: [
                CommandHandler("skip", skip_national_id),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_national_id),
            ],
            ASK_LICENCE: [
                CommandHandler("skip", skip_licence),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_licence),
            ],
            ASK_PLATE: [
                CommandHandler("skip", skip_plate),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_plate),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # Allow re-entry so users can run /enroll again to update their data
        allow_reentry=True,
    )
