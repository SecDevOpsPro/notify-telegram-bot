"""
Enrollment ConversationHandler wizard.

Guides an approved user through saving (or updating) their:
  1. National ID (EGN — 10 digits)
  2. Driving licence number
  3. Vehicle plate number

Each step shows the current stored value and offers /skip to keep it.
/back returns to the previous step (unavailable on step 1).
/cancel exits the wizard at any point.
"""

from __future__ import annotations

import logging
import re
import warnings
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from notify_bot import db
from notify_bot.errors import format_error
from notify_bot.middlewares import require_approved
from notify_bot.updates import require

# Exported for run_bot registration
__all__ = ["build_enroll_handler", "build_stale_enroll_button_handler", "unenroll_command"]

logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────

ASK_NATIONAL_ID, ASK_LICENCE, ASK_PLATE, ASK_TALON = range(4)

# ── Validation patterns ───────────────────────────────────────────────────────

_EGN_RE = re.compile(r"^\d{10}$")
_LICENCE_RE = re.compile(r"^(?:\d{5,12}|[A-Z]{2}\d{7})$", re.IGNORECASE)
_PLATE_RE = re.compile(r"^[A-Z]{1,3}\d{3,4}[A-Z]{0,3}$", re.IGNORECASE)
_TALON_RE = re.compile(r"^\d{6,12}$")


def _uid(update: Update) -> int:
    return require(update.effective_user, "effective_user").id


def _reply_target(update: Update) -> Message:
    """The message to reply to — resolves for both a typed command and a button tap."""
    return require(update.effective_message, "effective_message")


def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """The per-user scratch dict holding the wizard's not-yet-saved answers."""
    return require(context.user_data, "user_data")


async def _ack_callback(update: Update) -> None:
    """Answer the tapped button (clears its loading spinner), if this update is one."""
    if update.callback_query:
        await update.callback_query.answer()


async def _current_value(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, key: str, field: db.ProfileField
) -> tuple[str | None, bool]:
    """The value a step should display/offer to keep, and whether it's
    actually saved to the DB yet.

    Prefers whatever was typed for this field earlier *this* wizard run
    (e.g. before the user stepped away via /back) — that's real but not yet
    persisted, so ``is_saved`` is False for it — falling back to the
    persisted DB value (``is_saved`` True) otherwise.
    """
    user_data = _user_data(context)
    if key in user_data:
        return user_data[key], False
    profile = await db.get_profile(user_id)
    return (profile.get(field) if profile else None), True


def _format_current(raw: str | None, is_saved: bool) -> str:
    if not raw:
        return "—"
    if is_saved:
        return f"<code>{raw}</code>"
    return f"<code>{raw}</code> ⚠️ <i>not yet saved</i>"


def _skip_hint_and_keyboard(has_value: bool, has_back: bool) -> tuple[str, InlineKeyboardMarkup]:
    """There's nothing to fall back to when the field has no saved value yet,
    so skipping isn't offered — only Back (if not on step 1) and Cancel are."""
    row: list[InlineKeyboardButton] = []
    if has_back:
        row.append(InlineKeyboardButton("⬅ Back", callback_data="enroll:back"))
    if has_value:
        row.append(InlineKeyboardButton("⏭ Skip", callback_data="enroll:skip"))
    row.append(InlineKeyboardButton("❌ Cancel", callback_data="enroll:cancel"))

    lead = (
        "Send /skip to keep the current value"
        if has_value
        else "This field has no saved value yet — please enter one"
    )
    options = ["/back to go to the previous step"] if has_back else []
    options.append("/cancel to quit")
    hint = f"{lead}, " + ", ".join(options) + " — or tap the buttons below."

    return hint, InlineKeyboardMarkup([row])


# ── Entry point ───────────────────────────────────────────────────────────────


async def enroll_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the enrollment wizard — ask for National ID.

    Reused as both the /enroll command entry point and the "📝 Enroll your
    data" menu button's entry point — a button tap carries a callback_query
    with no top-level `.message`, so this replies via `.effective_message`
    (which resolves correctly for both) rather than `.message`.
    """
    await _ack_callback(update)
    return await _ask_national_id(update, context)


async def _ask_national_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw, is_saved = await _current_value(context, _uid(update), "enroll_national_id", "national_id")
    hint, keyboard = _skip_hint_and_keyboard(bool(raw), has_back=False)

    await _reply_target(update).reply_html(
        "📋 <b>Enrollment Wizard</b> — Step 1 of 4\n\n"
        f"Current National ID: {_format_current(raw, is_saved)}\n\n"
        f"Please enter your <b>National ID (EGN)</b> — 10 digits.\n{hint}",
        reply_markup=keyboard,
    )
    return ASK_NATIONAL_ID


# ── Step 1: National ID ───────────────────────────────────────────────────────


async def received_national_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = require(update.message, "message")
    text = require(message.text, "text").strip()
    if not _EGN_RE.match(text):
        can_skip, _ = await _current_value(
            context, _uid(update), "enroll_national_id", "national_id"
        )
        retry_hint = "Try again or /skip." if can_skip else "Try again, or /cancel to quit."
        await message.reply_text(f"❌ Invalid EGN — must be exactly 10 digits.  {retry_hint}")
        return ASK_NATIONAL_ID

    _user_data(context)["enroll_national_id"] = text
    return await _ask_licence(update, context)


async def skip_national_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    raw, _ = await _current_value(context, _uid(update), "enroll_national_id", "national_id")
    if not raw:
        await _reply_target(update).reply_text(
            "❌ You don't have a saved National ID to skip — please enter one, or /cancel to quit."
        )
        return ASK_NATIONAL_ID
    return await _ask_licence(update, context)


async def back_to_national_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    return await _ask_national_id(update, context)


async def _ask_licence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw, is_saved = await _current_value(context, _uid(update), "enroll_licence", "driving_licence")
    hint, keyboard = _skip_hint_and_keyboard(bool(raw), has_back=True)

    await _reply_target(update).reply_html(
        "📋 <b>Enrollment Wizard</b> — Step 2 of 4\n\n"
        f"Current Driving Licence: {_format_current(raw, is_saved)}\n\n"
        "Please enter your <b>Driving Licence number</b> (digits only, or 2 letters + 7 digits "
        f"e.g. <code>DA2123456</code>).\n{hint}",
        reply_markup=keyboard,
    )
    return ASK_LICENCE


# ── Step 2: Driving licence ───────────────────────────────────────────────────


async def received_licence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = require(update.message, "message")
    text = require(message.text, "text").strip().upper()
    if not _LICENCE_RE.match(text):
        can_skip, _ = await _current_value(
            context, _uid(update), "enroll_licence", "driving_licence"
        )
        retry_hint = "Try again or /skip." if can_skip else "Try again, or /cancel to quit."
        await message.reply_text(
            "❌ Invalid licence number (5–12 digits, or 2 letters + 7 digits e.g. DA2123456).  "
            f"{retry_hint}"
        )
        return ASK_LICENCE

    _user_data(context)["enroll_licence"] = text
    return await _ask_plate(update, context)


async def skip_licence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    raw, _ = await _current_value(context, _uid(update), "enroll_licence", "driving_licence")
    if not raw:
        await _reply_target(update).reply_text(
            "❌ You don't have a saved Driving Licence to skip — "
            "please enter one, or /cancel to quit."
        )
        return ASK_LICENCE
    return await _ask_plate(update, context)


async def back_to_licence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    return await _ask_licence(update, context)


async def _ask_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw, is_saved = await _current_value(context, _uid(update), "enroll_plate", "vehicle_plate")
    hint, keyboard = _skip_hint_and_keyboard(bool(raw), has_back=True)

    await _reply_target(update).reply_html(
        "📋 <b>Enrollment Wizard</b> — Step 3 of 4\n\n"
        f"Current Vehicle Plate: {_format_current(raw, is_saved)}\n\n"
        f"Please enter your <b>vehicle plate</b> (e.g. <code>CB1234AB</code>).\n{hint}",
        reply_markup=keyboard,
    )
    return ASK_PLATE


# ── Step 3: Vehicle plate ─────────────────────────────────────────────────────


async def received_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = require(update.message, "message")
    text = require(message.text, "text").strip().upper()
    if not _PLATE_RE.match(text):
        can_skip, _ = await _current_value(context, _uid(update), "enroll_plate", "vehicle_plate")
        retry_hint = "Try again or /skip." if can_skip else "Try again, or /cancel to quit."
        await message.reply_text(f"❌ Invalid plate format (e.g. CB1234AB).  {retry_hint}")
        return ASK_PLATE

    _user_data(context)["enroll_plate"] = text
    return await _ask_talon(update, context)


async def skip_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    raw, _ = await _current_value(context, _uid(update), "enroll_plate", "vehicle_plate")
    if not raw:
        await _reply_target(update).reply_text(
            "❌ You don't have a saved Vehicle Plate to skip — "
            "please enter one, or /cancel to quit."
        )
        return ASK_PLATE
    return await _ask_talon(update, context)


async def back_to_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    return await _ask_plate(update, context)


async def _ask_talon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw, is_saved = await _current_value(context, _uid(update), "enroll_talon", "talon_no")
    hint, keyboard = _skip_hint_and_keyboard(bool(raw), has_back=True)

    await _reply_target(update).reply_html(
        "📋 <b>Enrollment Wizard</b> — Step 4 of 4\n\n"
        f"Current Talon No: {_format_current(raw, is_saved)}\n\n"
        f"Please enter your <b>Talon number</b> (small registration card, 6–12 digits).\n{hint}",
        reply_markup=keyboard,
    )
    return ASK_TALON


# ── Step 4: Talon number ───────────────────────────────────────────────────


async def received_talon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = require(update.message, "message")
    text = require(message.text, "text").strip()
    if not _TALON_RE.match(text):
        can_skip, _ = await _current_value(context, _uid(update), "enroll_talon", "talon_no")
        retry_hint = "Try again or /skip." if can_skip else "Try again, or /cancel to quit."
        await message.reply_text(f"❌ Invalid talon number (6–12 digits).  {retry_hint}")
        return ASK_TALON

    _user_data(context)["enroll_talon"] = text
    return await _save_and_confirm(update, context)


async def skip_talon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    raw, _ = await _current_value(context, _uid(update), "enroll_talon", "talon_no")
    if not raw:
        await _reply_target(update).reply_text(
            "❌ You don't have a saved Talon No to skip — please enter one, or /cancel to quit."
        )
        return ASK_TALON
    return await _save_and_confirm(update, context)


# ── Confirmation & save ─────────────────────────────────────────────────


async def _save_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = _uid(update)
    user_data = _user_data(context)
    national_id = user_data.pop("enroll_national_id", None)
    licence = user_data.pop("enroll_licence", None)
    plate = user_data.pop("enroll_plate", None)
    talon = user_data.pop("enroll_talon", None)

    try:
        await db.upsert_profile(
            uid,
            national_id=national_id,
            driving_licence=licence,
            vehicle_plate=plate,
            talon_no=talon,
        )
        profile = await db.get_profile(uid)
    except Exception as exc:
        logger.exception("Failed to save profile for user_id=%s", uid)
        await _reply_target(update).reply_html(
            format_error(
                uid,
                "⚠️ Something went wrong while saving your data. Please try /enroll again, "
                "or contact the admin if this keeps happening.",
                exc,
            )
        )
        return ConversationHandler.END
    if profile is None:
        # Save succeeded but the immediate re-fetch came back empty (e.g. a
        # concurrent /unenroll raced us) — don't crash on profile.get(...).
        logger.warning("Profile fetch returned no row right after save for user_id=%s", uid)
        await _reply_target(update).reply_text(
            "⚠️ Your data was saved, but we couldn't confirm the details. "
            "Use /enroll to review them."
        )
        return ConversationHandler.END

    await _reply_target(update).reply_html(
        "✅ <b>Profile saved!</b>\n\n"
        f"National ID:      <code>{profile.get('national_id') or '—'}</code>\n"
        f"Driving Licence:  <code>{profile.get('driving_licence') or '—'}</code>\n"
        f"Vehicle Plate:    <code>{profile.get('vehicle_plate') or '—'}</code>\n"
        f"Talon No:         <code>{profile.get('talon_no') or '—'}</code>\n\n"
        "Use /driver to check driving licence obligations.\n"
        "Use /plate to check vehicle obligations."
    )
    return ConversationHandler.END


# ── Cancel ────────────────────────────────────────────────────────────────────


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    user_data = _user_data(context)
    for key in ("enroll_national_id", "enroll_licence", "enroll_plate", "enroll_talon"):
        user_data.pop(key, None)
    await _reply_target(update).reply_text(
        "Enrollment cancelled.  Your existing data is unchanged."
    )
    return ConversationHandler.END


async def stale_enroll_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a wizard button tapped after the wizard already ended.

    The Skip/Back/Cancel keyboards stay on old prompts after /cancel, a save,
    or a restart, but ConversationHandler only routes those callbacks while a
    conversation is active — an unclaimed tap would leave the button stuck
    on its loading spinner. Answer it, and strip the dead keyboard.
    """
    query = require(update.callback_query, "callback_query")
    await query.answer("This enrollment session has ended — use /enroll to start again.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        # Message too old to edit, or keyboard already gone — nothing to clean up.
        logger.debug("Could not clear stale enroll keyboard", exc_info=True)


# ── De-registration ──────────────────────────────────────────────────────────


@require_approved
async def unenroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unenroll — delete the user's saved profile data."""
    message = require(update.message, "message")
    uid = _uid(update)
    profile = await db.get_profile(uid)
    if not profile:
        await message.reply_text("ℹ️ You don't have any saved profile data to remove.")
        return

    try:
        await db.delete_profile(uid)
    except Exception as exc:
        logger.exception("Failed to delete profile for user_id=%s", uid)
        await message.reply_html(
            format_error(
                uid,
                "⚠️ Something went wrong while deleting your data. Please try /unenroll again, "
                "or contact the admin if this keeps happening.",
                exc,
            )
        )
        return

    await message.reply_text(
        "🗑️ Your profile data has been deleted.\n"
        "National ID, driving licence, vehicle plate and talon number have been removed.\n\n"
        "Use /enroll to save new data at any time."
    )


# ── Handler factory ───────────────────────────────────────────────────────────


def build_enroll_handler() -> ConversationHandler:
    """
    Build and return the fully configured ConversationHandler for /enroll.
    Must be registered *before* any plain CommandHandlers.
    """
    # PTB warns whenever per_message=False and a CallbackQueryHandler is present,
    # even when set explicitly. per_message=True is not an option here: it needs
    # every handler to be a CallbackQueryHandler, but the wizard also takes
    # commands and free-text replies. The per-user conversation is what we want,
    # and stale buttons are caught by build_stale_enroll_button_handler().
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="If 'per_message=False'", category=PTBUserWarning)
        return _build_enroll_conversation()


def _build_enroll_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("enroll", require_approved(enroll_start)),
            CallbackQueryHandler(require_approved(enroll_start), pattern=r"^cmd:enroll$"),
        ],
        states={
            ASK_NATIONAL_ID: [
                CommandHandler("skip", skip_national_id),
                CallbackQueryHandler(skip_national_id, pattern=r"^enroll:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_national_id),
            ],
            ASK_LICENCE: [
                CommandHandler("back", back_to_national_id),
                CallbackQueryHandler(back_to_national_id, pattern=r"^enroll:back$"),
                CommandHandler("skip", skip_licence),
                CallbackQueryHandler(skip_licence, pattern=r"^enroll:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_licence),
            ],
            ASK_PLATE: [
                CommandHandler("back", back_to_licence),
                CallbackQueryHandler(back_to_licence, pattern=r"^enroll:back$"),
                CommandHandler("skip", skip_plate),
                CallbackQueryHandler(skip_plate, pattern=r"^enroll:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_plate),
            ],
            ASK_TALON: [
                CommandHandler("back", back_to_plate),
                CallbackQueryHandler(back_to_plate, pattern=r"^enroll:back$"),
                CommandHandler("skip", skip_talon),
                CallbackQueryHandler(skip_talon, pattern=r"^enroll:skip$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_talon),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^enroll:cancel$"),
        ],
        # Allow re-entry so users can run /enroll again to update their data
        allow_reentry=True,
    )


def build_stale_enroll_button_handler() -> CallbackQueryHandler:
    """Catch-all for "enroll:*" taps the ConversationHandler didn't claim.

    Must be registered *after* build_enroll_handler() so live wizard buttons
    reach their step handlers first.
    """
    return CallbackQueryHandler(stale_enroll_button, pattern=r"^enroll:")
