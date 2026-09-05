"""Tests for the scheduled daily digest (notify_bot/scheduler/jobs.py)."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.scheduler.jobs import (
    _build_report_message,
    _days_until,
    _retry,
    daily_obligations_report,
    send_user_report_now,
)
from notify_bot.services.bgtoll import BgtollError, CloudflareBlockedError, VignetteInfo
from notify_bot.services.boleron import (
    BoleronError,
    BoleronVignetteInfo,
    FinesResult,
    GtpInfo,
    MtplInfo,
)
from notify_bot.services.mvr import MVRApiError, Obligation
from notify_bot.services.sofiatraffic import ClampInfo, StickerInfo, SofiaTrafficError

PLATE = "XH2856"

_FULL_USER = {
    "user_id": 1,
    "first_name": "Test",
    "national_id": "1234567890",
    "driving_licence": "123456789",
    "vehicle_plate": PLATE,
}


def _soon(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%d.%m.%Y")


# ── _days_until ───────────────────────────────────────────────────────────────


def test_days_until_none_when_no_date():
    assert _days_until(None) is None


def test_days_until_bg_format():
    assert _days_until(_soon(5)) == 5


def test_days_until_iso_format():
    iso = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
    assert _days_until(iso) == 3


def test_days_until_negative_for_past_date():
    past = (date.today() - timedelta(days=2)).strftime("%d.%m.%Y")
    assert _days_until(past) == -2


def test_days_until_none_when_unparseable():
    assert _days_until("not-a-date") is None


# ── _retry ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_returns_on_first_success():
    coro_fn = AsyncMock(return_value="ok")
    assert await _retry(coro_fn) == "ok"
    assert coro_fn.await_count == 1


@pytest.mark.asyncio
async def test_retry_recovers_after_transient_failures():
    coro_fn = AsyncMock(side_effect=[ValueError("boom"), ValueError("boom"), "ok"])
    with patch("asyncio.sleep", AsyncMock(return_value=None)):
        assert await _retry(coro_fn) == "ok"
    assert coro_fn.await_count == 3


@pytest.mark.asyncio
async def test_retry_raises_after_exhausting_attempts():
    coro_fn = AsyncMock(side_effect=ValueError("boom"))
    with patch("asyncio.sleep", AsyncMock(return_value=None)):
        with pytest.raises(ValueError, match="boom"):
            await _retry(coro_fn)
    assert coro_fn.await_count == 3


@pytest.mark.asyncio
async def test_retry_does_not_retry_skip_on_exceptions():
    coro_fn = AsyncMock(side_effect=CloudflareBlockedError("blocked"))
    with patch("asyncio.sleep", AsyncMock(return_value=None)) as mock_sleep:
        with pytest.raises(CloudflareBlockedError):
            await _retry(coro_fn, skip_on=(CloudflareBlockedError,))
    assert coro_fn.await_count == 1
    mock_sleep.assert_not_called()


# ── _build_report_message: defaults + patch helper ──────────────────────────

_DEFAULT_VIGNETTE = VignetteInfo(plate=PLATE, country="BG", found=False)
_DEFAULT_STICKER = StickerInfo(plate=PLATE, found=False)
_DEFAULT_CLAMP = ClampInfo(plate=PLATE, found=False)
_DEFAULT_GTP = GtpInfo(found=False)
_DEFAULT_MTPL = MtplInfo(active=False)
_DEFAULT_FINES = FinesResult(has_fines=False, count=0, total=0.0, total_discount=0.0)


@contextmanager
def _patched(**overrides):
    """
    Patch every external check `_build_report_message` calls with an
    "nothing found" default, then apply per-test overrides on top.

    ``overrides`` maps a short name (licence, plate, vignette, vignette_boleron,
    sticker_and_clamp, gtp, mtpl, fines) to the mock that should replace the
    default for that check.
    """
    targets = {
        "licence": ("notify_bot.scheduler.jobs.check_by_licence", AsyncMock(return_value=[])),
        "plate": ("notify_bot.scheduler.jobs.check_by_plate", AsyncMock(return_value=[])),
        "vignette": ("notify_bot.scheduler.jobs.check_vignette", AsyncMock(return_value=_DEFAULT_VIGNETTE)),
        "vignette_boleron": (
            "notify_bot.scheduler.jobs.check_vignette_boleron",
            AsyncMock(return_value=BoleronVignetteInfo(found=False)),
        ),
        "sticker_and_clamp": (
            "notify_bot.scheduler.jobs.check_sticker_and_clamp",
            AsyncMock(return_value=(_DEFAULT_STICKER, _DEFAULT_CLAMP)),
        ),
        "gtp": ("notify_bot.scheduler.jobs.check_gtp", AsyncMock(return_value=_DEFAULT_GTP)),
        "mtpl": ("notify_bot.scheduler.jobs.check_mtpl", AsyncMock(return_value=_DEFAULT_MTPL)),
        "fines": ("notify_bot.scheduler.jobs.check_fines", AsyncMock(return_value=_DEFAULT_FINES)),
    }
    with ExitStack() as stack:
        for name, (path, default_mock) in targets.items():
            stack.enter_context(patch(path, overrides.get(name, default_mock)))
        stack.enter_context(patch("asyncio.sleep", AsyncMock(return_value=None)))
        yield


# ── _build_report_message: top-level behavior ────────────────────────────────


@pytest.mark.asyncio
async def test_report_is_none_when_user_has_no_identifiers():
    user = {"user_id": 1, "first_name": "Test", "national_id": None, "driving_licence": None, "vehicle_plate": None}
    with _patched():
        message = await _build_report_message(user)
    assert message is None


@pytest.mark.asyncio
async def test_report_greets_with_fallback_name_when_missing():
    user = {**_FULL_USER, "first_name": None, "national_id": None, "driving_licence": None}
    with _patched():
        message = await _build_report_message(user)
    assert message is not None
    assert message.startswith("☀️ Good morning, there!")


# ── Licence / plate obligations sections ─────────────────────────────────────


@pytest.mark.asyncio
async def test_licence_obligations_section_included_on_success():
    units = [Obligation(unit_group=1, unit_group_label="Road Traffic Act and/or Insurance Code", obligations=[])]
    with _patched(licence=AsyncMock(return_value=units)):
        message = await _build_report_message(_FULL_USER)
    assert "🪪 <b>By driving licence:</b>" in message


@pytest.mark.asyncio
async def test_licence_check_failure_shows_error_line():
    with _patched(licence=AsyncMock(side_effect=MVRApiError("MVR API returned HTTP 500"))):
        message = await _build_report_message(_FULL_USER)
    assert "🪪 <b>By driving licence:</b>\n⚠️ Check failed: MVR API returned HTTP 500" in message


@pytest.mark.asyncio
async def test_plate_obligations_section_included_on_success():
    units = [Obligation(unit_group=1, unit_group_label="Road Traffic Act and/or Insurance Code", obligations=[])]
    with _patched(plate=AsyncMock(return_value=units)):
        message = await _build_report_message(_FULL_USER)
    assert "🚗 <b>By vehicle plate (MVR):</b>" in message


@pytest.mark.asyncio
async def test_plate_check_failure_shows_error_line():
    with _patched(plate=AsyncMock(side_effect=MVRApiError("boom"))):
        message = await _build_report_message(_FULL_USER)
    assert "🚗 <b>By vehicle plate (MVR):</b>\n⚠️ Check failed: boom" in message


# ── Vignette section ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vignette_found_valid_shows_expiry_warning():
    vignette = VignetteInfo(
        plate=PLATE,
        country="BG",
        found=True,
        status="VALID",
        validity_date_from="01.01.2026 00:00:00",
        validity_date_to=f"{_soon(5)} 23:59:59",
        vignette_type="Annual",
    )
    with _patched(vignette=AsyncMock(return_value=vignette)):
        message = await _build_report_message(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>" in message
    assert "✅ Status: Active" in message
    assert "📋 Type: Annual" in message
    assert "⚠️ Expires in 5 days!" in message


@pytest.mark.asyncio
async def test_vignette_not_found():
    with _patched():
        message = await _build_report_message(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>\n❌ No active vignette found." in message


@pytest.mark.asyncio
async def test_vignette_cloudflare_error_falls_back_to_boleron_and_finds_one():
    bv = BoleronVignetteInfo(
        found=True, active=True, valid_from="01.01.2026", valid_to="31.12.2026", validity_type="annual"
    )
    with _patched(
        vignette=AsyncMock(side_effect=CloudflareBlockedError("blocked")),
        vignette_boleron=AsyncMock(return_value=bv),
    ):
        message = await _build_report_message(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>" in message
    assert "✅ Status: Active" in message
    assert "📋 Type: Annual" in message


@pytest.mark.asyncio
async def test_vignette_bgtoll_error_falls_back_to_boleron_not_found():
    with _patched(vignette=AsyncMock(side_effect=BgtollError("connection error"))):
        message = await _build_report_message(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>\n❌ No active vignette found." in message


# ── Parking sticker / wheel clamp sections ───────────────────────────────────


@pytest.mark.asyncio
async def test_report_omits_parking_sticker_section_when_not_found():
    """No news is good news: an absent sticker shouldn't clutter the daily digest."""
    with _patched():
        message = await _build_report_message(_FULL_USER)
    assert "Parking sticker" not in message


@pytest.mark.asyncio
async def test_report_includes_parking_sticker_section_when_found():
    sticker = StickerInfo(
        plate=PLATE, found=True, status="Active", valid_from="01.01.2026", valid_to="31.12.2026", zone="A"
    )
    with _patched(sticker_and_clamp=AsyncMock(return_value=(sticker, _DEFAULT_CLAMP))):
        message = await _build_report_message(_FULL_USER)
    assert f"🅿️ <b>Parking sticker ({PLATE}):</b>" in message
    assert "✅ Status: Active" in message
    assert "📅 Valid: 01.01.2026 → 31.12.2026" in message
    assert "📍 Zone: A" in message


@pytest.mark.asyncio
async def test_report_omits_wheel_clamp_section_when_not_clamped():
    clamp = ClampInfo(plate=PLATE, found=True, clamped=False)
    with _patched(sticker_and_clamp=AsyncMock(return_value=(_DEFAULT_STICKER, clamp))):
        message = await _build_report_message(_FULL_USER)
    assert "Wheel clamp" not in message


@pytest.mark.asyncio
async def test_report_includes_wheel_clamp_section_when_clamped():
    clamp = ClampInfo(plate=PLATE, found=True, clamped=True, clamped_at="10:00", location="Main St")
    with _patched(sticker_and_clamp=AsyncMock(return_value=(_DEFAULT_STICKER, clamp))):
        message = await _build_report_message(_FULL_USER)
    assert f"🔒 <b>Wheel clamp ({PLATE}):</b>" in message
    assert "❌ Vehicle <b>IS wheel-clamped!</b>" in message


@pytest.mark.asyncio
async def test_sticker_clamp_check_skipped_silently_on_cloudflare_error():
    with _patched(sticker_and_clamp=AsyncMock(side_effect=SofiaTrafficError("blocked"))):
        message = await _build_report_message(_FULL_USER)
    assert "Parking sticker" not in message
    assert "Wheel clamp" not in message


# ── Technical Inspection (GTP) section ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gtp_found_shows_expiry_warning():
    gtp = GtpInfo(found=True, valid_to=_soon(3))
    with _patched(gtp=AsyncMock(return_value=gtp)):
        message = await _build_report_message(_FULL_USER)
    assert f"🔧 <b>Technical Inspection ({PLATE}):</b>" in message
    assert f"✅ Valid until: {_soon(3)}" in message
    assert "⚠️ Expires in 3 days!" in message


@pytest.mark.asyncio
async def test_gtp_not_found():
    with _patched():
        message = await _build_report_message(_FULL_USER)
    assert f"🔧 <b>Technical Inspection ({PLATE}):</b>\n❌ No valid inspection found." in message


@pytest.mark.asyncio
async def test_gtp_error_skips_section_without_failing_report():
    with _patched(gtp=AsyncMock(side_effect=BoleronError("boom"))):
        message = await _build_report_message(_FULL_USER)
    assert message is not None
    assert "Technical Inspection" not in message


# ── Civil Liability (MTPL) section ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_mtpl_active_with_insurer_and_expiry_warning():
    mtpl = MtplInfo(active=True, insurer="Bulstrad", valid_to=_soon(7))
    with _patched(mtpl=AsyncMock(return_value=mtpl)):
        message = await _build_report_message(_FULL_USER)
    assert f"🛡️ <b>Civil Liability / MTPL ({PLATE}):</b>" in message
    assert "✅ Active" in message
    assert "🏢 Bulstrad" in message
    assert f"📅 Valid until: {_soon(7)}" in message
    assert "⚠️ Expires in 7 days!" in message


@pytest.mark.asyncio
async def test_mtpl_inactive_shows_no_active_policy():
    with _patched():
        message = await _build_report_message(_FULL_USER)
    assert f"🛡️ <b>Civil Liability / MTPL ({PLATE}):</b>" in message
    assert "❌ No active policy" in message


@pytest.mark.asyncio
async def test_mtpl_error_skips_section_without_failing_report():
    with _patched(mtpl=AsyncMock(side_effect=BoleronError("boom"))):
        message = await _build_report_message(_FULL_USER)
    assert message is not None
    assert "Civil Liability" not in message


# ── Traffic Fines section ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fines_present_with_discount():
    fines = FinesResult(has_fines=True, count=2, total=100.0, total_discount=70.0, currency_symbol="€")
    with _patched(fines=AsyncMock(return_value=fines)):
        message = await _build_report_message(_FULL_USER)
    assert "🚔 <b>Traffic Fines:</b>" in message
    assert "❌ 2 fine(s) — Total: 100.00 €" in message
    assert "💸 With discount: 70.00 €" in message


@pytest.mark.asyncio
async def test_fines_none_are_omitted():
    with _patched():
        message = await _build_report_message(_FULL_USER)
    assert "Traffic Fines" not in message


@pytest.mark.asyncio
async def test_fines_error_skips_section_without_failing_report():
    with _patched(fines=AsyncMock(side_effect=BoleronError("boom"))):
        message = await _build_report_message(_FULL_USER)
    assert message is not None
    assert "Traffic Fines" not in message


# ── send_user_report_now ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_user_report_now_sends_and_returns_true_when_something_to_report():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    with _patched():
        sent = await send_user_report_now(context, _FULL_USER)
    assert sent is True
    context.bot.send_message.assert_awaited_once()
    call_kwargs = context.bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == _FULL_USER["user_id"]
    assert call_kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_user_report_now_returns_false_when_nothing_to_report():
    user = {"user_id": 1, "first_name": "Test", "national_id": None, "driving_licence": None, "vehicle_plate": None}
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    with _patched():
        sent = await send_user_report_now(context, user)
    assert sent is False
    context.bot.send_message.assert_not_called()


# ── daily_obligations_report dispatcher ──────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_obligations_report_schedules_one_job_per_user():
    users = [
        {"user_id": 1, "first_name": "A"},
        {"user_id": 2, "first_name": "B"},
    ]
    context = MagicMock()
    context.job_queue.run_once = MagicMock()

    with (
        patch("notify_bot.scheduler.jobs.db.get_all_approved_with_profiles", AsyncMock(return_value=users)),
        patch("notify_bot.scheduler.jobs.random.randint", return_value=300),
    ):
        await daily_obligations_report(context)

    assert context.job_queue.run_once.call_count == 2
    names = {call.kwargs["name"] for call in context.job_queue.run_once.call_args_list}
    assert names == {"report_user_1", "report_user_2"}
