"""
Vehicle management.

/vehicles   — list saved vehicles, with buttons to make one the main
              (preferred) vehicle or remove it
/addvehicle — short wizard: plate, then talon (both required), with
              Back / Cancel buttons like /enroll

/enroll saves the first (main) vehicle; this module manages any others.
The main vehicle is what plate commands use when given no plate — see
``db`` for how it's kept valid when vehicles are removed.
"""

from __future__ import annotations

import logging
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
from notify_bot.handlers.enroll import PLATE_RE, TALON_RE
from notify_bot.middlewares import require_approved
from notify_bot.updates import require

__all__ = [
    "build_add_vehicle_handler",
    "build_stale_add_vehicle_button_handler",
    "build_vehicle_button_handler",
    "vehicles_command",
]

logger = logging.getLogger(__name__)

ASK_PLATE, ASK_TALON = range(2)

_PLATE_KEY = "addvehicle_plate"


def _uid(update: Update) -> int:
    return require(update.effective_user, "effective_user").id


def _reply_target(update: Update) -> Message:
    """The message to reply to — resolves for both a typed command and a button tap."""
    return require(update.effective_message, "effective_message")


def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return require(context.user_data, "user_data")


# ── /vehicles ─────────────────────────────────────────────────────────────────


def _pairs(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def _confirm_remove_keyboard(plate: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"✅ Yes, remove {plate}", callback_data=f"veh:del:{plate}"),
                InlineKeyboardButton("↩ Keep it", callback_data="veh:list:"),
            ]
        ]
    )


async def _render_vehicles(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """The /vehicles list and its per-vehicle buttons."""
    vehicles = await db.list_vehicles(uid)
    rows: list[list[InlineKeyboardButton]] = []
    if not vehicles:
        text = "🚘 <b>Your vehicles</b>\n\nNo vehicles saved yet."
    else:
        lines = ["🚘 <b>Your vehicles</b>", ""]
        for vehicle in vehicles:
            talon = vehicle["talon_no"]
            talon_text = f"talon <code>{talon}</code>" if talon else "⚠️ no talon"
            lines.append(f"• <code>{vehicle['plate']}</code> — {talon_text}")
        # Make-main buttons first, the remove buttons grouped after them.
        main_buttons = [
            InlineKeyboardButton(f"Make {v['plate']} main", callback_data=f"veh:main:{v['plate']}")
            for v in vehicles[1:]
        ]
        remove_buttons = [
            InlineKeyboardButton(f"🗑 Remove {v['plate']}", callback_data=f"veh:ask:{v['plate']}")
            for v in vehicles
        ]
        rows.extend(_pairs(main_buttons))
        rows.extend(_pairs(remove_buttons))
        # "main" only means something once there's a choice of vehicles.
        if len(vehicles) > 1:
            lines.append(f"\nMain vehicle: <code>{vehicles[0]['plate']}</code>")
        text = "\n".join(lines)
    if len(vehicles) < db.MAX_VEHICLES:
        rows.append([InlineKeyboardButton("➕ Add vehicle", callback_data="cmd:addvehicle")])
    return text, InlineKeyboardMarkup(rows) if rows else None


@require_approved
async def vehicles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/vehicles — list the user's vehicles with manage buttons."""
    message = require(update.message, "message")
    text, keyboard = await _render_vehicles(_uid(update))
    await message.reply_html(text, reply_markup=keyboard)


@require_approved
async def vehicle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a /vehicles button tap, then refresh the list.

    "veh:main:<plate>" makes it main; "veh:ask:<plate>" asks to confirm a
    removal, which "veh:del:<plate>" carries out and "veh:list:" calls off.
    """
    query = require(update.callback_query, "callback_query")
    uid = _uid(update)
    _, _, rest = (query.data or "").partition(":")
    action, _, plate = rest.partition(":")

    if action == "ask":
        await query.answer()
        try:
            await query.edit_message_text(
                f"🗑 Remove <code>{plate}</code> and its talon?",
                parse_mode="HTML",
                reply_markup=_confirm_remove_keyboard(plate),
            )
        except BadRequest:
            logger.debug("Could not show the remove confirmation", exc_info=True)
        return

    done = True
    note = ""
    try:
        if action == "list":
            pass
        elif action == "main":
            done = await db.set_preferred_vehicle(uid, plate)
            note = f"{plate} is now your main vehicle."
        elif action == "del":
            done = await db.delete_vehicle(uid, plate)
            note = f"🗑 {plate} removed."
        else:
            logger.warning("Unknown vehicle button callback_data=%s", query.data)
            await query.answer()
            return
        text, keyboard = await _render_vehicles(uid)
    except Exception as exc:
        logger.exception("Vehicle %s failed for user_id=%s", action, uid)
        await query.answer()
        await _reply_target(update).reply_html(
            format_error(uid, "⚠️ Something went wrong. Please try /vehicles again.", exc)
        )
        return

    await query.answer((note or None) if done else f"{plate} is no longer one of your vehicles.")
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest:
        # Message too old to edit, or unchanged — the answer() above already confirmed.
        logger.debug("Could not refresh the /vehicles list", exc_info=True)


def build_vehicle_button_handler() -> CallbackQueryHandler:
    return CallbackQueryHandler(vehicle_button, pattern=r"^veh:")


# ── /addvehicle wizard ────────────────────────────────────────────────────────


async def _ack_callback(update: Update) -> None:
    """Answer the tapped button (clears its loading spinner), if this update is one."""
    if update.callback_query:
        await update.callback_query.answer()


def _wizard_keyboard(*, has_back: bool) -> InlineKeyboardMarkup:
    """Back / Cancel, like the /enroll wizard's buttons — both steps are required."""
    row: list[InlineKeyboardButton] = []
    if has_back:
        row.append(InlineKeyboardButton("⬅ Back", callback_data="addveh:back"))
    row.append(InlineKeyboardButton("❌ Cancel", callback_data="addveh:cancel"))
    return InlineKeyboardMarkup([row])


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for both /addvehicle and the "➕ Add vehicle" button."""
    await _ack_callback(update)
    vehicles = await db.list_vehicles(_uid(update))
    if len(vehicles) >= db.MAX_VEHICLES:
        await _reply_target(update).reply_text(
            f"⚠️ You already have {db.MAX_VEHICLES} vehicles — remove one with /vehicles first."
        )
        return ConversationHandler.END
    return await _ask_plate(update, context)


async def _ask_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply_target(update).reply_html(
        "🚘 <b>Add a vehicle</b> — Step 1 of 2\n\n"
        "Please enter the <b>vehicle plate</b> (e.g. <code>CB1234AB</code>).\n"
        "Send /cancel to quit — or tap the button below.",
        reply_markup=_wizard_keyboard(has_back=False),
    )
    return ASK_PLATE


async def received_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = require(update.message, "message")
    plate = db.normalize_plate(require(message.text, "text"))
    if not PLATE_RE.match(plate):
        await message.reply_text("❌ Invalid plate format (e.g. CB1234AB).  Try again, or /cancel.")
        return ASK_PLATE

    _user_data(context)[_PLATE_KEY] = plate
    return await _ask_talon(update, context)


async def back_to_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    return await _ask_plate(update, context)


async def _ask_talon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plate: str = _user_data(context)[_PLATE_KEY]
    await _reply_target(update).reply_html(
        "🚘 <b>Add a vehicle</b> — Step 2 of 2\n\n"
        f"Please enter the <b>talon number</b> for <code>{plate}</code> "
        "(small registration card, 6–12 digits).\n"
        "Send /back to change the plate, /cancel to quit — or tap the buttons below.",
        reply_markup=_wizard_keyboard(has_back=True),
    )
    return ASK_TALON


async def received_talon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = require(update.message, "message")
    talon = require(message.text, "text").strip()
    if not TALON_RE.match(talon):
        await message.reply_text("❌ Invalid talon number (6–12 digits).  Try again, or /cancel.")
        return ASK_TALON
    return await _save(update, context, talon)


async def _save(update: Update, context: ContextTypes.DEFAULT_TYPE, talon: str | None) -> int:
    uid = _uid(update)
    plate: str = _user_data(context).pop(_PLATE_KEY)
    try:
        await db.save_vehicle(uid, plate, talon)
        text, keyboard = await _render_vehicles(uid)
    except db.VehicleLimitError:
        await _reply_target(update).reply_text(
            f"⚠️ You already have {db.MAX_VEHICLES} vehicles — remove one with /vehicles first."
        )
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("Failed to save vehicle for user_id=%s", uid)
        await _reply_target(update).reply_html(
            format_error(
                uid, "⚠️ Something went wrong while saving the vehicle. Please try again.", exc
            )
        )
        return ConversationHandler.END

    await _reply_target(update).reply_html(
        f"✅ <code>{plate}</code> saved.\n\n{text}", reply_markup=keyboard
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack_callback(update)
    _user_data(context).pop(_PLATE_KEY, None)
    await _reply_target(update).reply_text("Adding a vehicle cancelled.")
    return ConversationHandler.END


def build_add_vehicle_handler() -> ConversationHandler:
    """The /addvehicle ConversationHandler.

    Register it *before* the /help menu's generic "cmd:" handler so its
    "cmd:addvehicle" entry point claims that button first.
    """
    # Same PTB per_message warning as the /enroll wizard — see build_enroll_handler().
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="If 'per_message=False'", category=PTBUserWarning)
        return ConversationHandler(
            entry_points=[
                CommandHandler("addvehicle", require_approved(add_start)),
                CallbackQueryHandler(require_approved(add_start), pattern=r"^cmd:addvehicle$"),
            ],
            states={
                ASK_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_plate)],
                ASK_TALON: [
                    CommandHandler("back", back_to_plate),
                    CallbackQueryHandler(back_to_plate, pattern=r"^addveh:back$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, received_talon),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", cancel),
                CallbackQueryHandler(cancel, pattern=r"^addveh:cancel$"),
            ],
            allow_reentry=True,
        )


async def stale_add_vehicle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A wizard button tapped after the wizard ended — answer it and strip the dead keyboard."""
    query = require(update.callback_query, "callback_query")
    await query.answer("This session has ended — use /addvehicle to start again.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        logger.debug("Could not clear stale add-vehicle keyboard", exc_info=True)


def build_stale_add_vehicle_button_handler() -> CallbackQueryHandler:
    """Catch-all for "addveh:*" taps the ConversationHandler didn't claim.

    Must be registered *after* build_add_vehicle_handler().
    """
    return CallbackQueryHandler(stale_add_vehicle_button, pattern=r"^addveh:")
