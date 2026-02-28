"""
Scheduled job definitions.

``daily_obligations_report`` is registered in ``run_bot.py`` via::

    job_queue.run_daily(daily_obligations_report, time=config.DAILY_REPORT_TIME)

It iterates every approved user who has at least one profile field set and sends
them a personalized obligations report.
"""
from __future__ import annotations

import logging

from jinja2 import Template
from telegram.ext import ContextTypes

from notify_bot import db
from notify_bot.services.bgtoll import BgtollError, CloudflareBlockedError, check_vignette
from notify_bot.services.mvr import MVRApiError, Obligation, check_by_licence, check_by_plate
from notify_bot.services.sofiatraffic import (
    CloudflareError as SofiaCloudflareError,
    SofiaTrafficError,
    check_clamp,
    check_sticker,
)

logger = logging.getLogger(__name__)

_TEMPLATE = Template(
    """{% for unit in units %}
<b>{{ unit.unit_group_label }}</b>
{% if unit.has_obligations %}
{% for ob in unit.obligations %}  • {{ ob }}
{% endfor %}
{% else %}  ✅ No obligations
{% endif %}{% endfor %}"""
)


def _render(units: list[Obligation]) -> str:
    return _TEMPLATE.render(units=units)


async def daily_obligations_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Send a daily obligations report to every approved user with profile data.

    Runs at the time configured by ``DAILY_REPORT_TIME`` (default 08:00 UTC).
    """
    users = await db.get_all_approved_with_profiles()
    logger.info("Daily report: processing %d user(s)", len(users))

    for user in users:
        uid: int = user["user_id"]
        name: str = user.get("first_name") or "there"
        national_id: str | None = user.get("national_id")
        licence: str | None = user.get("driving_licence")
        plate: str | None = user.get("vehicle_plate")

        sections: list[str] = []

        if national_id and licence:
            try:
                units = await check_by_licence(national_id, licence)
                sections.append("🪪 <b>By driving licence:</b>\n" + _render(units))
            except MVRApiError as exc:
                logger.warning("Licence check failed for user %s: %s", uid, exc)
                sections.append(f"🪪 <b>By driving licence:</b>\n⚠️ Check failed: {exc}")

        if national_id and plate:
            try:
                units = await check_by_plate(national_id, plate)
                sections.append("🚗 <b>By vehicle plate (MVR):</b>\n" + _render(units))
            except MVRApiError as exc:
                logger.warning("Plate check failed for user %s: %s", uid, exc)
                sections.append(f"🚗 <b>By vehicle plate (MVR):</b>\n⚠️ Check failed: {exc}")

        if plate:
            try:
                vignette = await check_vignette(plate)
                if vignette.found:
                    status_icon = "✅" if vignette.is_valid else "❌"
                    vignette_lines = [
                        f"🛣️ <b>Vignette ({plate}):</b>",
                        f"{status_icon} Status: {vignette.status or 'N/A'}",
                    ]
                    if vignette.validity_date_from:
                        vignette_lines.append(f"📅 Valid: {vignette.validity_date_from} → {vignette.validity_date_to}")
                    if vignette.vignette_type:
                        vignette_lines.append(f"📋 Type: {vignette.vignette_type}")
                    sections.append("\n".join(vignette_lines))
                else:
                    sections.append(
                        f"🛣️ <b>Vignette ({plate}):</b>\n"
                        "❌ No active vignette found."
                    )
            except CloudflareBlockedError:
                # CF challenge — skip silently in scheduled context, don't spam users
                logger.debug("Vignette check skipped for user %s — Cloudflare blocked", uid)
            except BgtollError as exc:
                logger.warning("Vignette check failed for user %s: %s", uid, exc)

        if plate:
            try:
                sticker = await check_sticker(plate)
                if sticker.found:
                    status_icon = "✅" if sticker.is_valid else "❌"
                    sticker_lines = [f"🅿️ <b>Parking sticker ({plate}):</b>"]
                    sticker_lines.append(f"{status_icon} Status: {sticker.status or 'Active'}")
                    if sticker.valid_from:
                        sticker_lines.append(f"📅 Valid: {sticker.valid_from} → {sticker.valid_to}")
                    if sticker.zone:
                        sticker_lines.append(f"📍 Zone: {sticker.zone}")
                    sections.append("\n".join(sticker_lines))
                else:
                    sections.append(
                        f"🅿️ <b>Parking sticker ({plate}):</b>\n"
                        "❌ No active parking sticker found."
                    )
            except SofiaCloudflareError:
                logger.debug("Sticker check skipped for user %s — Cloudflare blocked", uid)
            except SofiaTrafficError as exc:
                logger.warning("Sticker check failed for user %s: %s", uid, exc)

        if plate:
            try:
                clamp = await check_clamp(plate)
                if clamp.found and clamp.clamped:
                    clamp_lines = [
                        f"🔒 <b>Wheel clamp ({plate}):</b>",
                        "❌ Vehicle <b>IS wheel-clamped!</b>",
                    ]
                    if clamp.clamped_at:
                        clamp_lines.append(f"🕐 Clamped at: {clamp.clamped_at}")
                    if clamp.location:
                        clamp_lines.append(f"📍 Location: {clamp.location}")
                    sections.append("\n".join(clamp_lines))
                # If not clamped: omit from daily report (no news is good news)
            except SofiaCloudflareError:
                logger.debug("Clamp check skipped for user %s — Cloudflare blocked", uid)
            except SofiaTrafficError as exc:
                logger.warning("Clamp check failed for user %s: %s", uid, exc)

        if not sections:
            continue

        message = f"☀️ Good morning, {name}!\n\n" + "\n\n".join(sections)

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=message,
                parse_mode="HTML",
            )
            logger.debug("Daily report sent to user %s", uid)
        except Exception as exc:
            logger.warning("Could not deliver daily report to user %s: %s", uid, exc)
