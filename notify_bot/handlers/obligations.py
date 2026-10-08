"""
Obligation check handlers.

/driver   — check by driving licence (uses stored national_id + driving_licence)
/plate    — check by vehicle plate   (uses stored national_id + a plate)
/vignette — check e-vignette for vehicle plate
/sticker  — check Sofia parking sticker
/clamp    — check Sofia wheel-clamp status

All commands require admin approval.  Plate-based commands take an optional
plate argument; without one they use the user's main (preferred) vehicle —
see ``_pick_vehicle``.
"""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from notify_bot import config, db
from notify_bot.dates import expiry_warning
from notify_bot.errors import format_error
from notify_bot.formatting import align_fields
from notify_bot.handlers.enroll import PLATE_RE
from notify_bot.middlewares import require_approved
from notify_bot.payment_buttons import build_copy_keyboard
from notify_bot.services.bgtoll import (
    BgtollError,
    CloudflareBlockedError,
    check_vignette,
    format_validity_period,
)
from notify_bot.services.boleron import (
    BoleronError,
    BoleronNotFoundError,
    VehicleData,
    check_fines,
    check_gtp,
    check_mtpl,
    check_vehicle_data,
    check_vignette_boleron,
)
from notify_bot.services.mvr import (
    MVRApiError,
    Obligation,
    check_by_licence,
    check_by_plate,
    render_fine_messages,
)
from notify_bot.services.sofiatraffic import (
    CloudflareError as SofiaCloudflareError,
)
from notify_bot.services.sofiatraffic import (
    SofiaTrafficError,
    check_clamp,
    check_sticker,
)
from notify_bot.updates import require

logger = logging.getLogger(__name__)


async def _reply_check_failed(
    message: Message, uid: int, title: str, exc: Exception, footer: str = ""
) -> None:
    """Report a failed check — terse for regular users, with exception detail for debug users."""
    await message.reply_html(
        format_error(uid, f"{title}\n\n⚠️ Check failed — please try again later.{footer}", exc)
    )


# sofiatraffic.bg sometimes gives a negative answer when the opposite is true,
# so "no sticker" / "not clamped" replies carry a warning instead of reading as final.
_SOFIA_UNRELIABLE_WARNING = (
    "⚠️ <i>sofiatraffic.bg is unreliable and may show no sticker even when an active one exists.</i>"
)
_SOFIA_CLAMP_UNRELIABLE_WARNING = (
    "⚠️ <i>sofiatraffic.bg is unreliable and may show a vehicle as not clamped even when it is.</i>"
)

_SOFIA_MANUAL_LINK = (
    '\nCheck manually: <a href="https://www.sofiatraffic.bg/en/parking">sofiatraffic.bg/parking</a>'
)


def _debug_note(uid: int, note: str) -> str:
    """Extra diagnostic line appended to a reply, shown only to debug users."""
    if not config.is_debug_user(uid):
        return ""
    return f"\n\n🛠 <code>{html.escape(note)}</code>"


async def _rejected_plate_arg(message: Message, args: list[str] | None) -> bool:
    """Reply and return True when a plate argument isn't a valid plate.

    Plates end up in HTML replies, so anything else must not get that far.
    """
    if not args or PLATE_RE.match(db.normalize_plate(args[0])):
        return False
    await message.reply_text("❌ Invalid plate format (e.g. CB1234AB).")
    return True


async def _pick_vehicle(
    uid: int, args: list[str] | None, command: str
) -> tuple[str | None, db.VehicleRow | None, InlineKeyboardMarkup | None]:
    """Resolve which vehicle a plate command checks.

    A plate argument wins; it's paired with the user's saved vehicle of that
    plate, if any (for its talon).  Otherwise the preferred vehicle is used,
    and the returned keyboard has a button per other vehicle that re-runs
    *command* for it (a ``cmd:<command>:<plate>`` menu callback).  Returns
    ``(plate, vehicle, others)``; plate is None when there's no argument and
    no saved vehicle.
    """
    vehicles = await db.list_vehicles(uid)
    if args:
        plate = db.normalize_plate(args[0])
        return plate, next((v for v in vehicles if v["plate"] == plate), None), None
    if not vehicles:
        return None, None, None
    preferred, others = vehicles[0], vehicles[1:]
    buttons = [
        InlineKeyboardButton(f"🚘 Lookup {v['plate']}", callback_data=f"cmd:{command}:{v['plate']}")
        for v in others
    ]
    keyboard = (
        InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])
        if buttons
        else None
    )
    return preferred["plate"], preferred, keyboard


async def _reply_with_obligations(message: Message, units: list[Obligation]) -> None:
    """Send an obligations check: one message per payable fine, each with its copy buttons."""
    for part in render_fine_messages(units):
        await message.reply_html(part.text, reply_markup=build_copy_keyboard(part.payment))


# ── /driver ───────────────────────────────────────────────────────────────────


@require_approved
async def driver_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check traffic/document obligations by driving licence number."""
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id
    profile = await db.get_profile(uid)
    national_id = profile.get("national_id") if profile else None
    licence = profile.get("driving_licence") if profile else None

    if not national_id or not licence:
        await message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /enroll to save your National ID and Driving Licence number first."
        )
        return

    await message.reply_text("🔍 Checking obligations by driving licence…")

    try:
        units = await check_by_licence(national_id=national_id, licence_number=licence)
    except MVRApiError as exc:
        logger.exception("MVR API error for user %s", uid)
        await _reply_check_failed(message, uid, "🪪 <b>Obligations by driving licence</b>", exc)
        return

    await _reply_with_obligations(message, units)


# ── /vignette ─────────────────────────────────────────────────────────────────


@require_approved
async def vignette_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check road e-vignette status via bgtoll.bg.

    Usage: /vignette          — uses your main vehicle
           /vignette CB1234AB — check an ad-hoc plate
    """
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id

    if await _rejected_plate_arg(message, context.args):
        return
    plate, _, others = await _pick_vehicle(uid, context.args, "vignette")

    if not plate:
        await message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/vignette CB1234AB</code> or save your plate with /enroll."
        )
        return

    await message.reply_text(f"🔍 Checking vignette for {plate}…", reply_markup=others)

    try:
        info = await check_vignette(plate)
    except (CloudflareBlockedError, BgtollError) as exc:
        logger.debug("bgtoll failed for user %s (%s) — trying boleron fallback", uid, exc)
        try:
            bv = await check_vignette_boleron(plate)
        except BoleronError as boleron_exc:
            logger.warning(
                "Boleron vignette fallback also failed for user %s: %s", uid, boleron_exc
            )
            await _reply_check_failed(
                message,
                uid,
                f"🛣️ <b>Vignette for {plate}</b>",
                boleron_exc,
                '\nCheck manually: <a href="https://check.bgtoll.bg/">check.bgtoll.bg</a>'
                + _debug_note(uid, f"bgtoll.bg failed first: {type(exc).__name__}: {exc}"),
            )
            return
        fallback_note = _debug_note(
            uid, f"Via boleron.bg — bgtoll.bg failed: {type(exc).__name__}: {exc}"
        )
        if not bv.found:
            await message.reply_html(
                f"🛣️ <b>Vignette for {plate}</b>\n\n❌ No active vignette found.{fallback_note}"
            )
            return
        status_icon = "✅" if bv.active else "❌"
        status_label = "Active" if bv.active else "Inactive"
        lines = [f"🛣️ <b>Vignette for {plate}</b>", f"{status_icon} Status: {status_label}"]
        lines.extend(format_validity_period(bv.valid_from, bv.valid_to))
        if bv.validity_type:
            lines.append(f"📋 Type: {bv.validity_type.capitalize()}")
        if bv.price:
            lines.append(f"💰 Price: {bv.price}")
        if warning := expiry_warning(bv.valid_to):
            lines.append(warning)
        await message.reply_html("\n".join(lines) + fallback_note)
        return

    if not info.found:
        await message.reply_html(f"🛣️ <b>Vignette for {plate}</b>\n\n❌ No active vignette found.")
        return

    status_icon = "✅" if info.is_valid else "❌"
    status_label = "Active" if info.is_valid else "Inactive"
    lines = [f"🛣️ <b>Vignette for {plate}</b>", f"{status_icon} Status: {status_label}"]
    lines.extend(format_validity_period(info.validity_date_from, info.validity_date_to))
    if info.vignette_type:
        lines.append(f"📋 Type: {info.vignette_type}")
    if info.emission_class:
        lines.append(f"🌿 Emission class: {info.emission_class}")
    if warning := expiry_warning(info.validity_date_to):
        lines.append(warning)

    await message.reply_html("\n".join(lines))


# ── /sticker ──────────────────────────────────────────────────────────────────


@require_approved
async def sticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check Sofia parking e-vignette sticker via sofiatraffic.bg.

    Usage: /sticker          — uses your main vehicle
           /sticker CB1234AB — check an ad-hoc plate
    """
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id

    if await _rejected_plate_arg(message, context.args):
        return
    plate, _, others = await _pick_vehicle(uid, context.args, "sticker")

    if not plate:
        await message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/sticker CB1234AB</code> or save your plate with /enroll."
        )
        return

    await message.reply_text(f"🔍 Checking parking sticker for {plate}…", reply_markup=others)

    try:
        info = await check_sticker(plate)
    except (SofiaCloudflareError, SofiaTrafficError) as exc:
        logger.warning("Sofia Traffic sticker check failed for user %s: %s", uid, exc)
        await _reply_check_failed(
            message, uid, f"🅿️ <b>Parking sticker for {plate}</b>", exc, _SOFIA_MANUAL_LINK
        )
        return

    if not info.found:
        await message.reply_html(
            f"🅿️ <b>Parking sticker for {plate}</b>\n\n"
            f"✅ No active parking sticker found.\n\n{_SOFIA_UNRELIABLE_WARNING}"
        )
        return

    status_icon = "✅" if info.is_valid else "❌"
    lines = [
        f"🅿️ <b>Parking sticker for {plate}</b>",
        f"{status_icon} Status: {info.status or 'Active'}",
    ]
    if info.valid_from:
        lines.append(f"📅 Valid: {info.valid_from} → {info.valid_to}")
    if info.zone:
        lines.append(f"📍 Zone: {info.zone}")
    if info.sticker_type:
        lines.append(f"📋 Type: {info.sticker_type}")

    await message.reply_html("\n".join(lines))


# ── /clamp ────────────────────────────────────────────────────────────────────


@require_approved
async def clamp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check whether a vehicle is wheel-clamped in Sofia via sofiatraffic.bg.

    Usage: /clamp          — uses your main vehicle
           /clamp CB1234AB — check an ad-hoc plate
    """
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id

    if await _rejected_plate_arg(message, context.args):
        return
    plate, _, others = await _pick_vehicle(uid, context.args, "clamp")

    if not plate:
        await message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/clamp CB1234AB</code> or save your plate with /enroll."
        )
        return

    await message.reply_text(f"🔍 Checking wheel-clamp status for {plate}…", reply_markup=others)

    try:
        info = await check_clamp(plate)
    except (SofiaCloudflareError, SofiaTrafficError) as exc:
        logger.warning("Sofia Traffic clamp check failed for user %s: %s", uid, exc)
        await _reply_check_failed(
            message, uid, f"🔒 <b>Wheel clamp for {plate}</b>", exc, _SOFIA_MANUAL_LINK
        )
        return

    if not info.found or not info.clamped:
        await message.reply_html(
            f"🔓 <b>Wheel clamp for {plate}</b>\n\n✅ Vehicle is <b>not</b> wheel-clamped.\n\n"
            f"{_SOFIA_CLAMP_UNRELIABLE_WARNING}"
        )
        return

    lines = [f"🔒 <b>Wheel clamp for {plate}</b>", "❌ Vehicle <b>IS wheel-clamped!</b>"]
    if info.clamped_at:
        lines.append(f"🕐 Clamped at: {info.clamped_at}")
    if info.location:
        lines.append(f"📍 Location: {info.location}")
    if info.release_instructions:
        lines.append(f"ℹ️ {info.release_instructions}")
    lines.append('\n<a href="https://www.sofiatraffic.bg/en/parking">sofiatraffic.bg/parking</a>')

    await message.reply_html("\n".join(lines))


@require_approved
async def plate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check traffic/document obligations by vehicle plate number.

    Usage: /plate          — uses your main vehicle
           /plate CB1234AB — check another plate
    """
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id
    profile = await db.get_profile(uid)
    national_id = profile.get("national_id") if profile else None
    if await _rejected_plate_arg(message, context.args):
        return
    plate, _, others = await _pick_vehicle(uid, context.args, "plate")

    if not national_id or not plate:
        await message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /enroll to save your National ID and Vehicle Plate first."
        )
        return

    await message.reply_text(
        f"🔍 Checking obligations by vehicle plate {plate}…", reply_markup=others
    )

    try:
        units = await check_by_plate(national_id=national_id, plate_number=plate)
    except MVRApiError as exc:
        logger.exception("MVR API error for user %s", uid)
        await _reply_check_failed(
            message, uid, f"🚗 <b>Obligations by vehicle plate {plate}</b>", exc
        )
        return

    await _reply_with_obligations(message, units)


# ── /gtp ──────────────────────────────────────────────────────────────────────


@require_approved
async def gtp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check technical inspection (ГТП) validity via boleron.bg.

    The API needs the talon (small registration card) number alongside the plate.

    Usage: /gtp                    — uses your main vehicle's plate and talon
           /gtp CB1234AB 009999999 — check an ad-hoc plate + talon
           /gtp CB1234AB           — only for one of your saved vehicles (uses its talon)
    """
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id

    args = context.args or []
    if await _rejected_plate_arg(message, context.args):
        return
    plate, vehicle, others = await _pick_vehicle(uid, args[:1], "gtp")
    talon: str | None = args[1].strip() if len(args) > 1 else None
    if not talon and vehicle:
        talon = vehicle["talon_no"]

    if not plate or not talon:
        await message.reply_html(
            "⚠️ <b>Plate and talon number needed.</b>\n\n"
            "The technical inspection check requires both. Use "
            "<code>/gtp CB1234AB 009999999</code> or save them with /vehicles."
        )
        return

    await message.reply_text(f"🔍 Checking technical inspection for {plate}…", reply_markup=others)

    try:
        info = await check_gtp(car_no=plate, talon_no=talon)
    except BoleronError as exc:
        logger.exception("Boleron GTP error for user %s", uid)
        await _reply_check_failed(message, uid, f"🔧 <b>Technical Inspection for {plate}</b>", exc)
        return

    if not info.found:
        await message.reply_html(
            f"🔧 <b>Technical Inspection for {plate}</b>\n\n❌ No valid inspection found."
        )
        return

    lines = [
        f"🔧 <b>Technical Inspection for {plate}</b>",
        f"✅ Valid until: <b>{info.valid_to}</b>",
    ]
    if warning := expiry_warning(info.valid_to):
        lines.append(warning)
    await message.reply_html("\n".join(lines))


# ── /mtpl ─────────────────────────────────────────────────────────────────────


@require_approved
async def mtpl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check MTPL civil liability insurance via boleron.bg.

    Usage: /mtpl          — uses your main vehicle
           /mtpl CB1234AB — check an ad-hoc plate
    """
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id

    if await _rejected_plate_arg(message, context.args):
        return
    plate, _, others = await _pick_vehicle(uid, context.args, "mtpl")

    if not plate:
        await message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/mtpl CB1234AB</code> or save your plate with /enroll."
        )
        return

    await message.reply_text(
        f"🔍 Checking civil liability insurance for {plate}…", reply_markup=others
    )

    try:
        info = await check_mtpl(plate)
    except BoleronError as exc:
        logger.exception("Boleron MTPL error for user %s", uid)
        await _reply_check_failed(message, uid, f"🛡️ <b>Civil Liability (MTPL) for {plate}</b>", exc)
        return

    status_icon = "✅" if info.active else "❌"
    lines = [
        f"🛡️ <b>Civil Liability (MTPL) for {plate}</b>",
        f"{status_icon} Status: {'Active' if info.active else 'No active policy'}",
    ]
    if info.insurer:
        lines.append(f"🏢 Insurer: {info.insurer}")
    if info.valid_from and info.valid_to:
        lines.append(f"📅 Valid from: {info.valid_from} to {info.valid_to}")
    if warning := expiry_warning(info.valid_to):
        lines.append(warning)

    await message.reply_html("\n".join(lines))


# ── /fines ────────────────────────────────────────────────────────────────────


@require_approved
async def fines_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check traffic fines (КАТ) via boleron.bg using stored EGN + driving licence."""
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id
    profile = await db.get_profile(uid)
    national_id = profile.get("national_id") if profile else None
    licence = profile.get("driving_licence") if profile else None

    if not national_id or not licence:
        await message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /enroll to save your National ID and Driving Licence first."
        )
        return

    await message.reply_text("🔍 Checking traffic fines…")

    try:
        result = await check_fines(driver_licence_no=licence, egn=national_id)
    except BoleronError as exc:
        logger.exception("Boleron fines error for user %s", uid)
        await _reply_check_failed(message, uid, "🚔 <b>Traffic Fines</b>", exc)
        return

    if not result.has_fines:
        await message.reply_html("🚔 <b>Traffic Fines</b>\n\n✅ No unpaid traffic fines found.")
        return

    sym = result.currency_symbol
    lines = [
        "🚔 <b>Traffic Fines</b>",
        f"❌ <b>{result.count}</b> fine(s) — Total: <b>{result.total:.2f} {sym}</b>",
    ]
    if result.total_discount > 0:
        lines.append(f"💸 With 30% discount: {result.total_discount:.2f} {sym}")
    for fine in result.details:
        desc = fine.description or fine.anpp_number or "Fine"
        lines.append(f"• {desc}: {fine.amount:.2f} {sym}")

    lines.append(
        '\n<a href="https://www.boleron.bg/en/fine-check-result/">Pay online at boleron.bg</a>'
    )
    await message.reply_html("\n".join(lines))


@require_approved
async def vehicle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show vehicle registration data using a saved vehicle's plate + talon number.

    Usage: /vehicle          — your main vehicle
           /vehicle CB1234AB — another of your saved vehicles
    """
    message = require(update.message, "message")
    uid = require(update.effective_user, "effective_user").id
    if await _rejected_plate_arg(message, context.args):
        return
    plate, vehicle, others = await _pick_vehicle(uid, context.args, "vehicle")
    talon = vehicle["talon_no"] if vehicle else None

    if not plate or not talon:
        await message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /vehicles to save the vehicle with its plate and talon number first."
        )
        return

    await message.reply_text(f"🔍 Looking up vehicle data for {plate}…", reply_markup=others)

    try:
        v: VehicleData = await check_vehicle_data(car_no=plate, talon_no=talon)
    except BoleronNotFoundError:
        await message.reply_html(
            "⚠️ <b>Vehicle not found.</b>\n\n"
            f"No data found for plate <code>{plate}</code> / talon <code>{talon}</code> "
            "in the boleron.bg database.\n\n"
            "This vehicle may not be registered in their system yet."
        )
        return
    except BoleronError as exc:
        logger.warning("Boleron vehicleDataServices error for user %s: %s", uid, exc)
        await _reply_check_failed(message, uid, "🚗 <b>Vehicle data</b>", exc)
        return

    fields: list[tuple[str, str]] = []
    if v.build_year:
        fields.append(("Year", str(v.build_year)))
    if v.first_reg_date:
        fields.append(("First reg", v.first_reg_date))
    if v.vin:
        fields.append(("VIN", f"<code>{v.vin}</code>"))
    if v.engine:
        cc = f" / {v.engine_cc} cc" if v.engine_cc else ""
        kw = f" / {v.power_kw} kW" if v.power_kw else ""
        fields.append(("Engine", f"{v.engine}{cc}{kw}"))
    if v.color:
        fields.append(("Color", v.color))
    if v.vehicle_class:
        fields.append(("Class", v.vehicle_class))
    if v.seats:
        fields.append(("Seats", str(v.seats)))

    lines = [
        f"🚗 <b>Vehicle: {v.make_model or ((v.make or '') + ' ' + (v.model or '')).strip()}</b>",
        *align_fields(fields),
    ]
    if v.leasing:
        lines.append("🏦 Leasing vehicle")

    await message.reply_html("\n".join(lines))
