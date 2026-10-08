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
from typing import Any, Callable, Coroutine, Type, cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from notify_bot import db
from notify_bot.dates import expiry_warning
from notify_bot.db import ReportTarget
from notify_bot.errors import format_error
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
    check_clamp,
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

# ── Helpers ───────────────────────────────────────────────────────────────────


def _check_failed(uid: int, title: str, exc: Exception) -> str:
    """Section shown when a check errored — the report always lists every check.

    Its retry is a button under the report (see ``_retry_button``): a typed
    hint like "/plate CB1234AB" isn't enough, since tapping a command in a
    message sends it without its argument.
    """
    return format_error(uid, f"{title}\n⚠️ Check failed — tap its 🔁 button below to retry.", exc)


def _retry_button(label: str, command: str, plate: str | None = None) -> InlineKeyboardButton:
    """A "🔁 Retry" button that re-runs *command* (for *plate*) via the /help menu callback."""
    if plate:
        return InlineKeyboardButton(f"🔁 {label} {plate}", callback_data=f"cmd:{command}:{plate}")
    return InlineKeyboardButton(f"🔁 {label}", callback_data=f"cmd:{command}")


def _report_keyboard(
    fines: InlineKeyboardMarkup | None, retries: list[InlineKeyboardButton]
) -> InlineKeyboardMarkup | None:
    """The fines shortcuts, then the failed checks' retry buttons, two per row."""
    rows = [list(row) for row in fines.inline_keyboard] if fines else []
    rows.extend(retries[i : i + 2] for i in range(0, len(retries), 2))
    return InlineKeyboardMarkup(rows) if rows else None


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
    """One user's daily report: one message per entry, each with its own buttons.

    The first entry is the greeting with the personal checks (fines, licence)
    and the main vehicle's sections; each one after it is another vehicle's.
    ``keyboards[i]`` holds the fine shortcuts and retry buttons for
    ``entries[i]``'s checks, so a button sits under the section it acts on.
    """

    entries: tuple[str, ...]
    keyboards: tuple[InlineKeyboardMarkup | None, ...]

    @property
    def text(self) -> str:
        """The whole report as one string."""
        return "\n\n".join(self.entries)

    @property
    def buttons(self) -> list[InlineKeyboardButton]:
        """Every button across the report's messages, in order."""
        return [b for kb in self.keyboards if kb for row in kb.inline_keyboard for b in row]


async def _send_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int, report: _Report) -> None:
    """Send *report* one entry per message, each with its own buttons."""
    for text, keyboard in zip(report.entries, report.keyboards, strict=True):
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard
        )


async def _build_report(user: ReportTarget) -> _Report | None:
    """
    Run all obligation checks for one user and compose their report.

    Every check that applies to the user gets a section, whether it came back
    clean, with findings, or failed — except the MVR licence/plate and wheel
    clamp checks, which only appear when they find something or fail.  The
    parking sticker isn't checked at all: sofiatraffic.bg misses active
    stickers too often for an unprompted report (/sticker still offers it).
    Sections follow the order the checks run in: fines and the MVR licence
    details first, then for each vehicle (preferred first) its MVR plate
    details, inspection, MTPL, vignette and wheel clamp.  Returns ``None``
    only when the user has no profile data that any check applies to.
    """
    uid: int = user["user_id"]
    name: str = user.get("first_name") or "there"
    national_id: str | None = user.get("national_id")
    licence: str | None = user.get("driving_licence")

    sections: list[str] = []
    licence_units: list[Obligation] = []  # decide which /driver, /plate buttons the report gets
    plate_units: dict[str, list[Obligation]] = {}
    # One per failed check, by plate (None for the personal checks).
    retries: dict[str | None, list[InlineKeyboardButton]] = {}

    calls_made = False

    async def pause() -> None:
        """Space out API calls: sleep before every call but the first."""
        nonlocal calls_made
        if calls_made:
            await asyncio.sleep(_INTER_CHECK_DELAY)
        calls_made = True

    if national_id and licence:
        try:
            await pause()
            fines = await _retry(check_fines, driver_licence_no=licence, egn=national_id)
            if fines.has_fines:
                sym = fines.currency_symbol
                fines_lines = [
                    "🚔 <b>Traffic Fines:</b>",
                    f"❌ {fines.count} fine(s) — Total: {fines.total:.2f} {sym}",
                ]
                if fines.total_discount > 0:
                    fines_lines.append(f"💸 With discount: {fines.total_discount:.2f} {sym}")
                fines_section = "\n".join(fines_lines)
            else:
                fines_section = "🚔 <b>Traffic Fines:</b>\n✅ No fines."
        except BoleronError as exc:
            logger.warning("Fines check failed for user %s: %s", uid, exc)
            fines_section = _check_failed(uid, "🚔 <b>Traffic Fines:</b>", exc)
            retries.setdefault(None, []).append(_retry_button("Retry fines", "fines"))
        sections.append(fines_section)

    if national_id and licence:
        try:
            await pause()
            units = await _retry(check_by_licence, national_id=national_id, licence_number=licence)
            if any(unit.has_obligations for unit in units):
                sections.append("🪪 <b>By driving licence:</b>\n" + render_obligations(units))
            licence_units = units
        except MVRApiError as exc:
            logger.warning("Licence check failed for user %s: %s", uid, exc)
            sections.append(_check_failed(uid, "🪪 <b>By driving licence:</b>", exc))
            retries.setdefault(None, []).append(_retry_button("Retry driver", "driver"))

    vehicle_starts: list[int] = []  # where each vehicle's sections begin
    for vehicle in user["vehicles"]:
        vehicle_starts.append(len(sections))
        plate = vehicle["plate"]
        talon = vehicle["talon_no"]

        if national_id:
            plate_title = f"🚗 <b>By vehicle plate {plate} (MVR):</b>"
            try:
                await pause()
                units = await _retry(check_by_plate, national_id=national_id, plate_number=plate)
                if any(unit.has_obligations for unit in units):
                    sections.append(f"{plate_title}\n" + render_obligations(units))
                plate_units[plate] = units
            except MVRApiError as exc:
                logger.warning("Plate check failed for user %s (%s): %s", uid, plate, exc)
                sections.append(_check_failed(uid, plate_title, exc))
                retries.setdefault(plate, []).append(_retry_button("Retry plate", "plate", plate))

        gtp_title = f"🔧 <b>Technical Inspection ({plate}):</b>"
        if not talon:
            sections.append(f"{gtp_title}\n⚠️ Talon number missing — save it with /vehicles.")
        else:
            try:
                await pause()
                gtp = await _retry(check_gtp, car_no=plate, talon_no=talon)
                if gtp.found:
                    gtp_lines = [gtp_title, f"✅ Valid until: {gtp.valid_to}"]
                    if warning := expiry_warning(gtp.valid_to):
                        gtp_lines.append(warning)
                    sections.append("\n".join(gtp_lines))
                else:
                    sections.append(f"{gtp_title}\n❌ No valid inspection found.")
            except BoleronError as exc:
                logger.warning("GTP check failed for user %s (%s): %s", uid, plate, exc)
                sections.append(_check_failed(uid, gtp_title, exc))
                retries.setdefault(plate, []).append(_retry_button("Retry GTP", "gtp", plate))

        mtpl_title = f"🛡️ <b>Civil Liability / MTPL ({plate}):</b>"
        try:
            await pause()
            mtpl = await _retry(check_mtpl, plate)
            status_icon = "✅" if mtpl.active else "❌"
            mtpl_lines = [
                mtpl_title,
                f"{status_icon} {'Active' if mtpl.active else 'No active policy'}",
            ]
            if mtpl.insurer:
                mtpl_lines.append(f"🏢 {mtpl.insurer}")
            if mtpl.valid_to:
                mtpl_lines.append(f"📅 Valid until: {mtpl.valid_to}")
            if warning := expiry_warning(mtpl.valid_to):
                mtpl_lines.append(warning)
            sections.append("\n".join(mtpl_lines))
        except BoleronError as exc:
            logger.warning("MTPL check failed for user %s (%s): %s", uid, plate, exc)
            sections.append(_check_failed(uid, mtpl_title, exc))
            retries.setdefault(plate, []).append(_retry_button("Retry MTPL", "mtpl", plate))

        vignette_title = f"🛣️ <b>Vignette ({plate}):</b>"
        try:
            await pause()
            vignette = await _retry(check_vignette, plate, skip_on=(CloudflareBlockedError,))
            if vignette.found:
                status_icon = "✅" if vignette.is_valid else "❌"
                status_label = "Active" if vignette.is_valid else "Inactive"
                vignette_lines = [vignette_title, f"{status_icon} Status: {status_label}"]
                vignette_lines.extend(
                    format_validity_period(vignette.validity_date_from, vignette.validity_date_to)
                )
                if vignette.vignette_type:
                    vignette_lines.append(f"📋 Type: {vignette.vignette_type}")
                if warning := expiry_warning(vignette.validity_date_to):
                    vignette_lines.append(warning)
                sections.append("\n".join(vignette_lines))
            else:
                sections.append(f"{vignette_title}\n❌ No active vignette found.")
        except (CloudflareBlockedError, BgtollError) as exc:
            logger.debug(
                "Vignette check failed for user %s (%s) — falling back to boleron", uid, exc
            )
            try:
                bv: BoleronVignetteInfo = await _retry(check_vignette_boleron, plate)
                if bv.found:
                    status_icon = "✅" if bv.active else "❌"
                    status_label = "Active" if bv.active else "Inactive"
                    bv_lines = [vignette_title, f"{status_icon} Status: {status_label}"]
                    bv_lines.extend(format_validity_period(bv.valid_from, bv.valid_to))
                    if bv.validity_type:
                        bv_lines.append(f"📋 Type: {bv.validity_type.capitalize()}")
                    if bv.price:
                        bv_lines.append(f"💰 Price: {bv.price}")
                    if warning := expiry_warning(bv.valid_to):
                        bv_lines.append(warning)
                    sections.append("\n".join(bv_lines))
                else:
                    sections.append(f"{vignette_title}\n❌ No active vignette found.")
            except BoleronError as exc:
                logger.warning("Boleron vignette fallback failed for user %s: %s", uid, exc)
                sections.append(_check_failed(uid, vignette_title, exc))
                retries.setdefault(plate, []).append(
                    _retry_button("Retry vignette", "vignette", plate)
                )

        clamp_title = f"🔒 <b>Wheel clamp ({plate}):</b>"
        try:
            await pause()
            clamp = await _retry(check_clamp, plate, skip_on=(SofiaCloudflareError,))
            if clamp.found and clamp.clamped:
                clamp_lines = [clamp_title, "❌ Vehicle <b>IS wheel-clamped!</b>"]
                if clamp.clamped_at:
                    clamp_lines.append(f"🕐 Clamped at: {clamp.clamped_at}")
                if clamp.location:
                    clamp_lines.append(f"📍 Location: {clamp.location}")
                sections.append("\n".join(clamp_lines))
        except (SofiaCloudflareError, SofiaTrafficError) as exc:
            logger.warning("Clamp check failed for user %s (%s): %s", uid, plate, exc)
            sections.append(_check_failed(uid, clamp_title, exc))
            retries.setdefault(plate, []).append(_retry_button("Retry clamp", "clamp", plate))

    if not sections:
        return None

    # One message for the personal checks + the main vehicle, then one per other
    # vehicle; each gets the buttons for its own checks.
    plates: list[str | None] = [v["plate"] for v in user["vehicles"]]
    groups = [[None, *plates[:1]], *([p] for p in plates[1:])]
    bounds = [0, *vehicle_starts[1:], len(sections)]
    entries = ["\n\n".join(sections[a:b]) for a, b in zip(bounds, bounds[1:], strict=False)]
    entries[0] = f"☀️ Good morning, {name}!\n\n{entries[0]}"
    keyboards = [
        _report_keyboard(
            build_fines_keyboard(
                licence=licence_units if None in group else [],
                plates={p: plate_units[p] for p in group if p in plate_units},
            ),
            [b for key in group for b in retries.get(key, [])],
        )
        for group in groups
    ]
    return _Report(entries=tuple(entries), keyboards=tuple(keyboards))


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
        await _send_report(context, uid, report)
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
    await _send_report(context, user["user_id"], report)
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
