"""
Scheduled job definitions.

``daily_obligations_report`` is registered in ``run_bot.py`` via::

    job_queue.run_daily(daily_obligations_report, time=config.DAILY_REPORT_TIME)

Instead of running all users back-to-back (which causes 429s), it schedules
each user's report as a separate one-shot job staggered ``_USER_STAGGER``
seconds apart.  Each individual check also retries up to ``_RETRY_ATTEMPTS``
times with exponential backoff before giving up.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Type

from telegram.ext import ContextTypes

from notify_bot import db
from notify_bot.services.bgtoll import BgtollError, CloudflareBlockedError, check_vignette
from notify_bot.services.boleron import (
    BoleronError,
    BoleronVignetteInfo,
    check_fines,
    check_gtp,
    check_mtpl,
    check_vignette_boleron,
)
from notify_bot.services.mvr import MVRApiError, check_by_licence, check_by_plate, render_obligations
from notify_bot.services.sofiatraffic import (
    CloudflareError as SofiaCloudflareError,
    SofiaTrafficError,
    check_sticker_and_clamp,
)

logger = logging.getLogger(__name__)

# ── Tuning knobs ──────────────────────────────────────────────────────────────

#: Seconds between each user's report job (spreads API calls across time).
_USER_STAGGER: int = 260

#: Seconds to sleep between individual API calls within one user's report.
_INTER_CHECK_DELAY: float = 3.0

#: Maximum retry attempts for a single API call.
_RETRY_ATTEMPTS: int = 3

#: Base delay (seconds) for exponential backoff — doubles each attempt.
_RETRY_BASE_DELAY: float = 5.0

# ── Retry helper ──────────────────────────────────────────────────────────────


async def _retry(
    coro_fn: Callable[..., Coroutine[Any, Any, Any]],
    *args: Any,
    skip_on: tuple[Type[BaseException], ...] = (),
) -> Any:
    """
    Call ``coro_fn(*args)`` up to ``_RETRY_ATTEMPTS`` times.

    Exceptions listed in ``skip_on`` are re-raised immediately without retry
    (used for Cloudflare challenges that won't resolve with a retry).
    All other exceptions trigger an exponential backoff wait before the next
    attempt.  The final attempt re-raises whatever exception occurred.
    """
    last_exc: BaseException | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await coro_fn(*args)
        except skip_on:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS - 1:
                delay = _RETRY_BASE_DELAY * (2**attempt)
                logger.debug(
                    "Retry %d/%d for %s in %.0fs — %s",
                    attempt + 1,
                    _RETRY_ATTEMPTS,
                    coro_fn.__name__,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Per-user report ───────────────────────────────────────────────────────────


async def _send_user_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    One-shot job: build and send the daily report for a single user.

    ``context.job.data`` must be a dict with keys:
    ``user_id``, ``first_name``, ``national_id``, ``driving_licence``, ``vehicle_plate``.
    """
    user: dict = context.job.data  # type: ignore[union-attr]
    uid: int = user["user_id"]
    name: str = user.get("first_name") or "there"
    national_id: str | None = user.get("national_id")
    licence: str | None = user.get("driving_licence")
    plate: str | None = user.get("vehicle_plate")

    sections: list[str] = []

    if national_id and licence:
        try:
            units = await _retry(check_by_licence, national_id, licence)
            sections.append("🪪 <b>By driving licence:</b>\n" + render_obligations(units))
        except MVRApiError as exc:
            logger.warning("Licence check failed for user %s: %s", uid, exc)
            sections.append(f"🪪 <b>By driving licence:</b>\n⚠️ Check failed: {exc}")
        if plate:  # only pause if plate-based checks follow
            await asyncio.sleep(_INTER_CHECK_DELAY)

    if national_id and plate:
        try:
            units = await _retry(check_by_plate, national_id, plate)
            sections.append("🚗 <b>By vehicle plate (MVR):</b>\n" + render_obligations(units))
        except MVRApiError as exc:
            logger.warning("Plate check failed for user %s: %s", uid, exc)
            sections.append(f"🚗 <b>By vehicle plate (MVR):</b>\n⚠️ Check failed: {exc}")
        await asyncio.sleep(_INTER_CHECK_DELAY)

    if plate:
        try:
            vignette = await _retry(
                check_vignette, plate, skip_on=(CloudflareBlockedError,)
            )
            if vignette.found:
                status_icon = "✅" if vignette.is_valid else "❌"
                status_label = "Active" if vignette.is_valid else "Inactive"
                vignette_lines = [
                    f"🛣️ <b>Vignette ({plate}):</b>",
                    f"{status_icon} Status: {status_label}",
                ]
                if vignette.validity_date_from:
                    vignette_lines.append(
                        f"📅 Valid: {vignette.validity_date_from} → {vignette.validity_date_to}"
                    )
                if vignette.vignette_type:
                    vignette_lines.append(f"📋 Type: {vignette.vignette_type}")
                sections.append("\n".join(vignette_lines))
            else:
                sections.append(f"🛣️ <b>Vignette ({plate}):</b>\n❌ No active vignette found.")
        except CloudflareBlockedError:
            logger.debug("Vignette check blocked for user %s — falling back to boleron", uid)
            try:
                bv: BoleronVignetteInfo = await _retry(check_vignette_boleron, plate)
                if bv.found:
                    status_icon = "✅" if bv.active else "❌"
                    status_label = "Active" if bv.active else "Inactive"
                    bv_lines = [
                        f"🛣️ <b>Vignette ({plate}):</b>",
                        f"{status_icon} Status: {status_label}",
                    ]
                    if bv.valid_from:
                        bv_lines.append(f"📅 Valid: {bv.valid_from} → {bv.valid_to}")
                    if bv.validity_type:
                        bv_lines.append(f"📋 Type: {bv.validity_type.capitalize()}")
                    if bv.price:
                        bv_lines.append(f"💰 Price: {bv.price}")
                    sections.append("\n".join(bv_lines))
                else:
                    sections.append(f"🛣️ <b>Vignette ({plate}):</b>\n❌ No active vignette found.")
            except BoleronError as exc:
                logger.warning("Boleron vignette fallback failed for user %s: %s", uid, exc)
        except BgtollError as exc:
            logger.warning("Vignette check failed for user %s: %s", uid, exc)
        await asyncio.sleep(_INTER_CHECK_DELAY)

    if plate:
        try:
            sticker, clamp = await _retry(
                check_sticker_and_clamp, plate, skip_on=(SofiaCloudflareError,)
            )
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
                    f"🅿️ <b>Parking sticker ({plate}):</b>\n❌ No active parking sticker found."
                )
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
            logger.debug("Sticker/clamp check skipped for user %s — Cloudflare blocked", uid)
        except SofiaTrafficError as exc:
            logger.warning("Sticker/clamp check failed for user %s: %s", uid, exc)

    if plate:
        await asyncio.sleep(_INTER_CHECK_DELAY)
        try:
            gtp = await _retry(check_gtp, plate)
            if gtp.found:
                sections.append(f"🔧 <b>Technical Inspection ({plate}):</b>\n✅ Valid to: {gtp.valid_to}")
            else:
                sections.append(f"🔧 <b>Technical Inspection ({plate}):</b>\n❌ No valid inspection found.")
        except BoleronError as exc:
            logger.warning("GTP check failed for user %s: %s", uid, exc)
        await asyncio.sleep(_INTER_CHECK_DELAY)

        try:
            mtpl = await _retry(check_mtpl, plate)
            status_icon = "✅" if mtpl.active else "❌"
            mtpl_lines = [
                f"🛡️ <b>Civil Liability / MTPL ({plate}):</b>",
                f"{status_icon} {'Active' if mtpl.active else 'No active policy'}",
            ]
            if mtpl.insurer:
                mtpl_lines.append(f"🏢 {mtpl.insurer}")
            if mtpl.valid_to:
                mtpl_lines.append(f"📅 Valid to: {mtpl.valid_to}")
            sections.append("\n".join(mtpl_lines))
        except BoleronError as exc:
            logger.warning("MTPL check failed for user %s: %s", uid, exc)

    if national_id and licence:
        await asyncio.sleep(_INTER_CHECK_DELAY)
        try:
            fines = await _retry(check_fines, licence, national_id)
            if fines.has_fines:
                sym = fines.currency_symbol
                fines_lines = [
                    "🚔 <b>Traffic Fines:</b>",
                    f"❌ {fines.count} fine(s) — Total: {fines.total:.2f} {sym}",
                ]
                if fines.total_discount > 0:
                    fines_lines.append(f"💸 With discount: {fines.total_discount:.2f} {sym}")
                sections.append("\n".join(fines_lines))
            # No fines: omit (no news is good news)
        except BoleronError as exc:
            logger.warning("Fines check failed for user %s: %s", uid, exc)

    if not sections:
        return

    message = f"☀️ Good morning, {name}!\n\n" + "\n\n".join(sections)
    try:
        await context.bot.send_message(chat_id=uid, text=message, parse_mode="HTML")
        logger.debug("Daily report sent to user %s", uid)
    except Exception as exc:
        logger.warning("Could not deliver daily report to user %s: %s", uid, exc)


# ── Dispatcher ────────────────────────────────────────────────────────────────


async def daily_obligations_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Daily trigger: schedule one report job per user, staggered by ``_USER_STAGGER`` seconds.

    Spreading users across time avoids hitting rate limits on the MVR and
    sofiatraffic.bg APIs when many users are checked simultaneously.
    """
    users = await db.get_all_approved_with_profiles()
    logger.info(
        "Daily report: scheduling %d user report(s), %ds apart", len(users), _USER_STAGGER
    )

    for i, user in enumerate(users):
        context.job_queue.run_once(  # type: ignore[union-attr]
            _send_user_report,
            when=i * _USER_STAGGER,  # int seconds from now; 0 = immediate, 60 = in 60s, …
            data=user,
            name=f"report_user_{user['user_id']}",
        )
