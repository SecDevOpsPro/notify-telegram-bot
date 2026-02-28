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

from jinja2 import Template
from telegram import Update
from telegram.ext import ContextTypes

from notify_bot import db
from notify_bot.middlewares import require_approved
from notify_bot.services.bgtoll import BgtollError, CloudflareBlockedError, check_vignette
from notify_bot.services.mvr import MVRApiError, Obligation, check_by_licence, check_by_plate
from notify_bot.services.sofiatraffic import (
    CloudflareError as SofiaCloudflareError,
    SofiaTrafficError,
    check_clamp,
    check_sticker,
)

logger = logging.getLogger(__name__)

_OBLIGATIONS_TEMPLATE = Template(
    """<b>🔎 Obligations check</b>
{% for unit in units %}

<b>{{ unit.unit_group_label }}</b>
{% if unit.has_obligations %}
{% for ob in unit.obligations %}  • {{ ob }}
{% endfor %}
{% else %}  ✅ No obligations found
{% endif %}{% endfor %}"""
)

_DRIVER_PHOTO = (
    "https://icon-library.com/images/drivers-license-icon/drivers-license-icon-26.jpg"
)


def _render(units: list[Obligation]) -> str:
    return _OBLIGATIONS_TEMPLATE.render(units=units)


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

    await update.message.reply_photo(photo=_DRIVER_PHOTO)
    await update.message.reply_html(_render(units))


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
    lines = [f"🛣️ <b>Vignette for {plate}</b>", f"{status_icon} Status: {info.status or 'N/A'}"]
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
            f"🅿️ <b>Parking sticker for {plate}</b>\n\n"
            "❌ No active parking sticker found."
        )
        return

    status_icon = "✅" if info.is_valid else "❌"
    lines = [f"🅿️ <b>Parking sticker for {plate}</b>", f"{status_icon} Status: {info.status or 'Active'}"]
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
            f"🔓 <b>Wheel clamp for {plate}</b>\n\n"
            "✅ Vehicle is <b>not</b> wheel-clamped."
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

    await update.message.reply_html(_render(units))
