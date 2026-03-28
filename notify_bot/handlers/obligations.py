"""
Obligation check handlers.

/driver   — check by driving licence (uses stored national_id + driving_licence)
/plate    — check by vehicle plate   (uses stored national_id + vehicle_plate)
/vignette — check e-vignette for vehicle plate (uses stored vehicle_plate or arg)
/sticker  — check Sofia parking sticker        (uses stored vehicle_plate or arg)
/clamp    — check Sofia wheel-clamp status     (uses stored vehicle_plate or arg)

All commands require admin approval.  Plate-based commands require the
vehicle_plate field to be filled via /enroll (or accept a plate argument).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from notify_bot import db
from notify_bot.middlewares import require_approved
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
from notify_bot.services.bgtoll import BgtollError, CloudflareBlockedError, check_vignette
from notify_bot.services.mvr import MVRApiError, check_by_licence, check_by_plate, render_obligations
from notify_bot.services.sofiatraffic import (
    CloudflareError as SofiaCloudflareError,
    SofiaTrafficError,
    check_clamp,
    check_sticker,
)

logger = logging.getLogger(__name__)


# ── /driver ───────────────────────────────────────────────────────────────────


@require_approved
async def driver_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check traffic/document obligations by driving licence number."""
    uid = update.effective_user.id
    profile = await db.get_profile(uid)

    if not profile or not profile.get("national_id") or not profile.get("driving_licence"):
        await update.message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /enroll to save your National ID and Driving Licence number first."
        )
        return

    await update.message.reply_text("🔍 Checking obligations by driving licence…")

    try:
        units = await check_by_licence(profile["national_id"], profile["driving_licence"])
    except MVRApiError as exc:
        logger.exception("MVR API error for user %s", uid)
        await update.message.reply_text(f"⚠️ MVR API error: {exc}")
        return

    await update.message.reply_html("<b>🔎 Obligations check</b>\n" + render_obligations(units))


# ── /vignette ─────────────────────────────────────────────────────────────────


@require_approved
async def vignette_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check road e-vignette status via bgtoll.bg.

    Usage: /vignette          — uses the plate stored via /enroll
           /vignette CB1234AB — check an ad-hoc plate
    """
    uid = update.effective_user.id

    # Plate from command arg takes precedence over enrolled plate
    plate: str | None = None
    if context.args:
        plate = context.args[0].strip().upper()

    if not plate:
        profile = await db.get_profile(uid)
        plate = (profile or {}).get("vehicle_plate")

    if not plate:
        await update.message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/vignette CB1234AB</code> or save your plate with /enroll."
        )
        return

    await update.message.reply_text(f"🔍 Checking vignette for {plate}…")

    try:
        info = await check_vignette(plate)
    except CloudflareBlockedError:
        await update.message.reply_html(
            f"⚠️ <b>Cloudflare blocked the request.</b>\n\n"
            "The bgtoll.bg site requires a browser to pass its bot-detection challenge.\n"
            f'Check manually: <a href="https://check.bgtoll.bg/">check.bgtoll.bg</a>'
        )
        return
    except BgtollError as exc:
        logger.exception("bgtoll API error for user %s", uid)
        await update.message.reply_text(f"⚠️ Vignette service error: {exc}")
        return

    if not info.found:
        await update.message.reply_html(
            f"🛣️ <b>Vignette for {plate}</b>\n\n❌ No active vignette found."
        )
        return

    status_icon = "✅" if info.is_valid else "❌"
    status_label = "Active" if info.is_valid else "Inactive"
    lines = [f"🛣️ <b>Vignette for {plate}</b>", f"{status_icon} Status: {status_label}"]
    if info.validity_date_from:
        lines.append(f"📅 Valid: {info.validity_date_from} → {info.validity_date_to}")
    if info.vignette_type:
        lines.append(f"📋 Type: {info.vignette_type}")
    if info.emission_class:
        lines.append(f"🌿 Emission class: {info.emission_class}")

    await update.message.reply_html("\n".join(lines))


# ── /sticker ──────────────────────────────────────────────────────────────────


@require_approved
async def sticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check Sofia parking e-vignette sticker via sofiatraffic.bg.

    Usage: /sticker          — uses the plate stored via /enroll
           /sticker CB1234AB — check an ad-hoc plate
    """
    uid = update.effective_user.id

    plate: str | None = None
    if context.args:
        plate = context.args[0].strip().upper()

    if not plate:
        profile = await db.get_profile(uid)
        plate = (profile or {}).get("vehicle_plate")

    if not plate:
        await update.message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/sticker CB1234AB</code> or save your plate with /enroll."
        )
        return

    await update.message.reply_text(f"🔍 Checking parking sticker for {plate}…")

    try:
        info = await check_sticker(plate)
    except SofiaCloudflareError:
        await update.message.reply_html(
            "⚠️ <b>Cloudflare blocked the request.</b>\n\n"
            "Check manually: "
            '<a href="https://www.sofiatraffic.bg/en/parking">sofiatraffic.bg/parking</a>'
        )
        return
    except SofiaTrafficError as exc:
        logger.exception("Sofia Traffic API error for user %s", uid)
        await update.message.reply_text(f"⚠️ Sofia Traffic service error: {exc}")
        return

    if not info.found:
        await update.message.reply_html(
            f"🅿️ <b>Parking sticker for {plate}</b>\n\n❌ No active parking sticker found."
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

    await update.message.reply_html("\n".join(lines))


# ── /clamp ────────────────────────────────────────────────────────────────────


@require_approved
async def clamp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check whether a vehicle is wheel-clamped in Sofia via sofiatraffic.bg.

    Usage: /clamp          — uses the plate stored via /enroll
           /clamp CB1234AB — check an ad-hoc plate
    """
    uid = update.effective_user.id

    plate: str | None = None
    if context.args:
        plate = context.args[0].strip().upper()

    if not plate:
        profile = await db.get_profile(uid)
        plate = (profile or {}).get("vehicle_plate")

    if not plate:
        await update.message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/clamp CB1234AB</code> or save your plate with /enroll."
        )
        return

    await update.message.reply_text(f"🔍 Checking wheel-clamp status for {plate}…")

    try:
        info = await check_clamp(plate)
    except SofiaCloudflareError:
        await update.message.reply_html(
            "⚠️ <b>Cloudflare blocked the request.</b>\n\n"
            "Check manually: "
            '<a href="https://www.sofiatraffic.bg/en/parking">sofiatraffic.bg/parking</a>'
        )
        return
    except SofiaTrafficError as exc:
        logger.exception("Sofia Traffic API error for user %s", uid)
        await update.message.reply_text(f"⚠️ Sofia Traffic service error: {exc}")
        return

    if not info.found or not info.clamped:
        await update.message.reply_html(
            f"🔓 <b>Wheel clamp for {plate}</b>\n\n✅ Vehicle is <b>not</b> wheel-clamped."
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

    await update.message.reply_html("\n".join(lines))


@require_approved
async def plate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check traffic/document obligations by vehicle plate number."""
    uid = update.effective_user.id
    profile = await db.get_profile(uid)

    if not profile or not profile.get("national_id") or not profile.get("vehicle_plate"):
        await update.message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /enroll to save your National ID and Vehicle Plate first."
        )
        return

    await update.message.reply_text("🔍 Checking obligations by vehicle plate…")

    try:
        units = await check_by_plate(profile["national_id"], profile["vehicle_plate"])
    except MVRApiError as exc:
        logger.exception("MVR API error for user %s", uid)
        await update.message.reply_text(f"⚠️ MVR API error: {exc}")
        return

    await update.message.reply_html("<b>🔎 Obligations check</b>\n" + render_obligations(units))


# ── /gtp ──────────────────────────────────────────────────────────────────────


@require_approved
async def gtp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check technical inspection (ГТП) validity via boleron.bg.

    Usage: /gtp          — uses the plate stored via /enroll
           /gtp CB1234AB — check an ad-hoc plate
    """
    uid = update.effective_user.id

    plate: str | None = None
    if context.args:
        plate = context.args[0].strip().upper()

    if not plate:
        profile = await db.get_profile(uid)
        plate = (profile or {}).get("vehicle_plate")

    if not plate:
        await update.message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/gtp CB1234AB</code> or save your plate with /enroll."
        )
        return

    await update.message.reply_text(f"🔍 Checking technical inspection for {plate}…")

    try:
        info = await check_gtp(plate)
    except BoleronError as exc:
        logger.exception("Boleron GTP error for user %s", uid)
        await update.message.reply_text(f"⚠️ Service error: {exc}")
        return

    if not info.found:
        await update.message.reply_html(
            f"🔧 <b>Technical Inspection for {plate}</b>\n\n❌ No valid inspection found."
        )
        return

    await update.message.reply_html(
        f"🔧 <b>Technical Inspection for {plate}</b>\n"
        f"✅ Valid to: <b>{info.valid_to}</b>"
    )


# ── /mtpl ─────────────────────────────────────────────────────────────────────


@require_approved
async def mtpl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check MTPL civil liability insurance via boleron.bg.

    Usage: /mtpl          — uses the plate stored via /enroll
           /mtpl CB1234AB — check an ad-hoc plate
    """
    uid = update.effective_user.id

    plate: str | None = None
    if context.args:
        plate = context.args[0].strip().upper()

    if not plate:
        profile = await db.get_profile(uid)
        plate = (profile or {}).get("vehicle_plate")

    if not plate:
        await update.message.reply_html(
            "⚠️ <b>No plate found.</b>\n\n"
            "Use <code>/mtpl CB1234AB</code> or save your plate with /enroll."
        )
        return

    await update.message.reply_text(f"🔍 Checking civil liability insurance for {plate}…")

    try:
        info = await check_mtpl(plate)
    except BoleronError as exc:
        logger.exception("Boleron MTPL error for user %s", uid)
        await update.message.reply_text(f"⚠️ Service error: {exc}")
        return

    status_icon = "✅" if info.active else "❌"
    lines = [
        f"🛡️ <b>Civil Liability (MTPL) for {plate}</b>",
        f"{status_icon} Status: {'Active' if info.active else 'No active policy'}",
    ]
    if info.insurer:
        lines.append(f"🏢 Insurer: {info.insurer}")
    if info.valid_from and info.valid_to:
        lines.append(f"📅 Valid: {info.valid_from} → {info.valid_to}")

    await update.message.reply_html("\n".join(lines))


# ── /fines ────────────────────────────────────────────────────────────────────


@require_approved
async def fines_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check traffic fines (КАТ) via boleron.bg using stored EGN + driving licence."""
    uid = update.effective_user.id
    profile = await db.get_profile(uid)

    if not profile or not profile.get("national_id") or not profile.get("driving_licence"):
        await update.message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /enroll to save your National ID and Driving Licence first."
        )
        return

    await update.message.reply_text("🔍 Checking traffic fines…")

    try:
        result = await check_fines(profile["driving_licence"], profile["national_id"])
    except BoleronError as exc:
        logger.exception("Boleron fines error for user %s", uid)
        await update.message.reply_text(f"⚠️ Service error: {exc}")
        return

    if not result.has_fines:
        await update.message.reply_html(
            "🚔 <b>Traffic Fines</b>\n\n✅ No unpaid traffic fines found."
        )
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

    lines.append('\n<a href="https://www.boleron.bg/en/fine-check-result/">Pay online at boleron.bg</a>')
    await update.message.reply_html("\n".join(lines))

@require_approved
async def vehicle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show vehicle registration data using stored plate + talon number."""
    uid = update.effective_user.id
    profile = await db.get_profile(uid)

    if not profile or not profile.get("vehicle_plate") or not profile.get("talon_no"):
        await update.message.reply_html(
            "⚠️ <b>Missing data.</b>\n\n"
            "Use /enroll to save your vehicle plate and talon number first."
        )
        return

    await update.message.reply_text("🔍 Looking up vehicle data…")

    try:
        v: VehicleData = await check_vehicle_data(
            profile["vehicle_plate"], profile["talon_no"]
        )
    except BoleronNotFoundError:
        plate = profile["vehicle_plate"]
        talon = profile["talon_no"]
        await update.message.reply_html(
            "⚠️ <b>Vehicle not found.</b>\n\n"
            f"No data found for plate <code>{plate}</code> / talon <code>{talon}</code> "
            "in the boleron.bg database.\n\n"
            "This vehicle may not be registered in their system yet."
        )
        return
    except BoleronError as exc:
        logger.warning("Boleron vehicleDataServices error for user %s: %s", uid, exc)
        await update.message.reply_text(f"⚠️ Service error: {exc}")
        return

    lines = [
        f"🚗 <b>Vehicle: {v.make_model or ((v.make or '') + ' ' + (v.model or '')).strip()}</b>",
    ]
    if v.build_year:
        lines.append(f"Year:        {v.build_year}")
    if v.first_reg_date:
        lines.append(f"First reg:   {v.first_reg_date}")
    if v.vin:
        lines.append(f"VIN:         <code>{v.vin}</code>")
    if v.engine:
        cc = f" / {v.engine_cc} cc" if v.engine_cc else ""
        kw = f" / {v.power_kw} kW" if v.power_kw else ""
        lines.append(f"Engine:      {v.engine}{cc}{kw}")
    if v.color:
        lines.append(f"Color:       {v.color}")
    if v.vehicle_class:
        lines.append(f"Class:       {v.vehicle_class}")
    if v.seats:
        lines.append(f"Seats:       {v.seats}")
    if v.leasing:
        lines.append("🏦 Leasing vehicle")

    await update.message.reply_html("\n".join(lines))
