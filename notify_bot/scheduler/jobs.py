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
import random
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Coroutine, Type, cast

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

from notify_bot import db
from notify_bot.dates import parse_datetime
from notify_bot.db import ReportTarget
from notify_bot.payment_buttons import build_fines_keyboard
from notify_bot.services.bgtoll import (
    BgtollError,
    CloudflareBlockedError,
    check_vignette,
    format_validity_period,
)
from notify_bot.services.boleron import (
    BoleronError,
    BoleronVignetteInfo,
    check_fines,
    check_gtp,
    check_mtpl,
    check_vignette_boleron,
)
from notify_bot.services.mvr import (
    MVRApiError,
    Obligation,
    check_by_licence,
    check_by_plate,
    render_obligations,
)
from notify_bot.services.sofiatraffic import (
    CloudflareError as SofiaCloudflareError,
)
from notify_bot.services.sofiatraffic import (
    SofiaTrafficError,
    check_sticker_and_clamp,
)
from notify_bot.updates import MissingUpdateFieldError, require

logger = logging.getLogger(__name__)

# ── Tuning knobs ──────────────────────────────────────────────────────────────

#: Seconds between each user's report job (spreads API calls across time).
_USER_STAGGER: int = 420

#: Seconds to sleep between individual API calls within one user's report.
_INTER_CHECK_DELAY: float = 3.0

#: Maximum retry attempts for a single API call.
_RETRY_ATTEMPTS: int = 3

#: Base delay (seconds) for exponential backoff — doubles each attempt.
_RETRY_BASE_DELAY: float = 5.0

#: Days remaining threshold below which an expiry warning is shown.
_EXPIRY_WARN_DAYS: int = 14

# ── Helpers ───────────────────────────────────────────────────────────────────


def _days_until(date_str: str | None) -> int | None:
    """
    Return the number of days between today and *date_str*.

    Accepts anything :func:`notify_bot.dates.parse_datetime` understands
    (``dd.mm.yyyy`` and ISO-8601, with or without a time).  Returns ``None``
    when the input is absent or unparseable.
    """
    parsed = parse_datetime(date_str)
    if parsed is None:
        return None
    return (parsed.date() - date.today()).days


# ── Retry helper ──────────────────────────────────────────────────────────────


async def _retry[T](
    coro_fn: Callable[..., Coroutine[Any, Any, T]],
    *args: Any,
    skip_on: tuple[Type[BaseException], ...] = (),
    **kwargs: Any,
) -> T:
    """
    Call ``coro_fn(*args, **kwargs)`` up to ``_RETRY_ATTEMPTS`` times.

    Exceptions listed in ``skip_on`` are re-raised immediately without retry
    (used for Cloudflare challenges that won't resolve with a retry).
    All other exceptions trigger an exponential backoff wait before the next
    attempt.  The final attempt re-raises whatever exception occurred.
    """
    last_exc: BaseException | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await coro_fn(*args, **kwargs)
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
    # Only reachable if _RETRY_ATTEMPTS were configured as 0 — nothing was ever tried.
    raise last_exc or RuntimeError("_retry made no attempts")


# ── Per-user report ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Report:
    """One user's daily report: the message text plus its /driver and /plate fine shortcuts."""

    text: str
    reply_markup: InlineKeyboardMarkup | None


async def _build_report(user: ReportTarget) -> _Report | None:
    """
    Run all obligation checks for one user and compose their report.

    Returns ``None`` when there is nothing to report (every check came back
    clean or unavailable) — the "no news is good news" convention used
    throughout this module.
    """
    uid: int = user["user_id"]
    name: str = user.get("first_name") or "there"
    national_id: str | None = user.get("national_id")
    licence: str | None = user.get("driving_licence")
    plate: str | None = user.get("vehicle_plate")

    sections: list[str] = []
    licence_units: list[Obligation] = []  # decide which /driver, /plate buttons the report gets
    plate_units: list[Obligation] = []

    if national_id and licence:
        try:
            units = await _retry(check_by_licence, national_id=national_id, licence_number=licence)
            sections.append("🪪 <b>By driving licence:</b>\n" + render_obligations(units))
            licence_units = units
        except MVRApiError as exc:
            logger.warning("Licence check failed for user %s: %s", uid, exc)
            sections.append(f"🪪 <b>By driving licence:</b>\n⚠️ Check failed: {exc}")
        if plate:  # only pause if plate-based checks follow
            await asyncio.sleep(_INTER_CHECK_DELAY)

    if national_id and plate:
        try:
            units = await _retry(check_by_plate, national_id=national_id, plate_number=plate)
            sections.append("🚗 <b>By vehicle plate (MVR):</b>\n" + render_obligations(units))
            plate_units = units
        except MVRApiError as exc:
            logger.warning("Plate check failed for user %s: %s", uid, exc)
            sections.append(f"🚗 <b>By vehicle plate (MVR):</b>\n⚠️ Check failed: {exc}")
        await asyncio.sleep(_INTER_CHECK_DELAY)

    if plate:
        try:
            vignette = await _retry(check_vignette, plate, skip_on=(CloudflareBlockedError,))
            if vignette.found:
                status_icon = "✅" if vignette.is_valid else "❌"
                status_label = "Active" if vignette.is_valid else "Inactive"
                vignette_lines = [
                    f"🛣️ <b>Vignette ({plate}):</b>",
                    f"{status_icon} Status: {status_label}",
                ]
                vignette_lines.extend(
                    format_validity_period(vignette.validity_date_from, vignette.validity_date_to)
                )
                if vignette.vignette_type:
                    vignette_lines.append(f"📋 Type: {vignette.vignette_type}")
                remaining = _days_until(vignette.validity_date_to)
                if remaining is not None and 0 <= remaining < _EXPIRY_WARN_DAYS:
                    vignette_lines.append(
                        f"⚠️ Expires in {remaining} day{'s' if remaining != 1 else ''}!"
                    )
                sections.append("\n".join(vignette_lines))
            else:
                sections.append(f"🛣️ <b>Vignette ({plate}):</b>\n❌ No active vignette found.")
        except (CloudflareBlockedError, BgtollError) as exc:
            logger.debug(
                "Vignette check failed for user %s (%s) — falling back to boleron", uid, exc
            )
            try:
                bv: BoleronVignetteInfo = await _retry(check_vignette_boleron, plate)
                if bv.found:
                    status_icon = "✅" if bv.active else "❌"
                    status_label = "Active" if bv.active else "Inactive"
                    bv_lines = [
                        f"🛣️ <b>Vignette ({plate}):</b>",
                        f"{status_icon} Status: {status_label}",
                    ]
                    bv_lines.extend(format_validity_period(bv.valid_from, bv.valid_to))
                    if bv.validity_type:
                        bv_lines.append(f"📋 Type: {bv.validity_type.capitalize()}")
                    if bv.price:
                        bv_lines.append(f"💰 Price: {bv.price}")
                    remaining = _days_until(bv.valid_to)
                    if remaining is not None and 0 <= remaining < _EXPIRY_WARN_DAYS:
                        bv_lines.append(
                            f"⚠️ Expires in {remaining} day{'s' if remaining != 1 else ''}!"
                        )
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
            # If not found: omit from daily report (no news is good news)
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
                gtp_lines = [
                    f"🔧 <b>Technical Inspection ({plate}):</b>",
                    f"✅ Valid until: {gtp.valid_to}",
                ]
                remaining = _days_until(gtp.valid_to)
                if remaining is not None and 0 <= remaining < _EXPIRY_WARN_DAYS:
                    gtp_lines.append(
                        f"⚠️ Expires in {remaining} day{'s' if remaining != 1 else ''}!"
                    )
                sections.append("\n".join(gtp_lines))
            else:
                sections.append(
                    f"🔧 <b>Technical Inspection ({plate}):</b>\n❌ No valid inspection found."
                )
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
                mtpl_lines.append(f"📅 Valid until: {mtpl.valid_to}")
            remaining = _days_until(mtpl.valid_to)
            if remaining is not None and 0 <= remaining < _EXPIRY_WARN_DAYS:
                mtpl_lines.append(f"⚠️ Expires in {remaining} day{'s' if remaining != 1 else ''}!")
            sections.append("\n".join(mtpl_lines))
        except BoleronError as exc:
            logger.warning("MTPL check failed for user %s: %s", uid, exc)

    if national_id and licence:
        await asyncio.sleep(_INTER_CHECK_DELAY)
        try:
            fines = await _retry(check_fines, driver_licence_no=licence, egn=national_id)
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
        return None

    return _Report(
        text=f"☀️ Good morning, {name}!\n\n" + "\n\n".join(sections),
        reply_markup=build_fines_keyboard(licence=licence_units, plate=plate_units),
    )


async def _send_user_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    One-shot job: build and send the daily report for a single user.

    ``context.job.data`` must be the :class:`~notify_bot.db.ReportTarget` that
    ``daily_obligations_report`` scheduled the job with.
    """
    user = cast(ReportTarget, require(context.job, "job").data)
    uid: int = user["user_id"]
    report = await _build_report(user)
    if report is None:
        return
    try:
        await context.bot.send_message(
            chat_id=uid, text=report.text, parse_mode="HTML", reply_markup=report.reply_markup
        )
        logger.debug("Daily report sent to user %s", uid)
    except Exception as exc:
        logger.warning("Could not deliver daily report to user %s: %s", uid, exc)


async def send_user_report_now(context: ContextTypes.DEFAULT_TYPE, user: ReportTarget) -> bool:
    """
    Build and immediately send one user's report, bypassing the job queue.

    Public counterpart to ``_send_user_report`` for callers that need the
    result synchronously — currently the admin ``/brief`` command — and
    need to know whether a report was actually sent, since an empty result
    is silent by design (see ``_build_report``).

    Returns ``True`` if a report was sent, ``False`` if there was nothing
    to report.
    """
    report = await _build_report(user)
    if report is None:
        return False
    await context.bot.send_message(
        chat_id=user["user_id"],
        text=report.text,
        parse_mode="HTML",
        reply_markup=report.reply_markup,
    )
    return True


# ── Dispatcher ────────────────────────────────────────────────────────────────


async def daily_obligations_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Daily trigger: schedule one report job per user, staggered by ``_USER_STAGGER`` seconds.

    Spreading users across time avoids hitting rate limits on the MVR and
    sofiatraffic.bg APIs when many users are checked simultaneously.
    """
    # Not require(): PTB types this property with a free TypeVar (JobQueue[ST]) that
    # a generic helper can't unify.
    job_queue = context.job_queue
    if job_queue is None:
        raise MissingUpdateFieldError("context has no job_queue")
    users = await db.get_all_approved_with_profiles()
    logger.info("Daily report: scheduling %d user report(s), %ds apart", len(users), _USER_STAGGER)

    for i, user in enumerate(users):
        job_queue.run_once(
            _send_user_report,
            when=i
            * random.randint(
                240, _USER_STAGGER
            ),  # int seconds from now; 0 = immediate, 60 = in 60s, …
            data=user,
            name=f"report_user_{user['user_id']}",
        )
