"""Tests for the scheduled daily digest (notify_bot/scheduler/jobs.py)."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.scheduler.jobs import (
    _build_report,
    _Report,
    _retry,
    _send_report,
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
from notify_bot.services.sofiatraffic import ClampInfo, SofiaTrafficError

PLATE = "XH2856"
OTHER_PLATE = "CB1234AB"


def _vehicle(plate: str, talon: str | None, vehicle_id: int = 1) -> dict:
    return {
        "id": vehicle_id,
        "user_id": 1,
        "plate": plate,
        "talon_no": talon,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


_FULL_USER = {
    "user_id": 1,
    "first_name": "Test",
    "national_id": "1234567890",
    "driving_licence": "123456789",
    "vehicles": [_vehicle(PLATE, "009999999")],
}


async def _report_text(user) -> str | None:
    """The text of *user*'s report, or None when there is nothing to report."""
    report = await _build_report(user)
    return report.text if report else None


def _soon(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%d.%m.%Y")


# ── _retry ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_returns_on_first_success():
    coro_fn = AsyncMock(return_value="ok")
    assert await _retry(coro_fn) == "ok"
    assert coro_fn.await_count == 1


@pytest.mark.asyncio
async def test_retry_forwards_positional_and_keyword_arguments():
    coro_fn = AsyncMock(return_value="ok")
    assert await _retry(coro_fn, "a", national_id="1", licence_number="2") == "ok"
    coro_fn.assert_awaited_once_with("a", national_id="1", licence_number="2")


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


# ── _build_report: defaults + patch helper ──────────────────────────

_DEFAULT_VIGNETTE = VignetteInfo(plate=PLATE, country="BG", found=False)
_DEFAULT_CLAMP = ClampInfo(plate=PLATE, found=False)
_DEFAULT_GTP = GtpInfo(found=False)
_DEFAULT_MTPL = MtplInfo(active=False)
_DEFAULT_FINES = FinesResult(has_fines=False, count=0, total=0.0, total_discount=0.0)


@contextmanager
def _patched(**overrides):
    """
    Patch every external check `_build_report` calls with an
    "nothing found" default, then apply per-test overrides on top.

    ``overrides`` maps a short name (licence, plate, vignette, vignette_boleron,
    clamp, gtp, mtpl, fines) to the mock that should replace the
    default for that check.
    """
    targets = {
        "licence": ("notify_bot.scheduler.jobs.check_by_licence", AsyncMock(return_value=[])),
        "plate": ("notify_bot.scheduler.jobs.check_by_plate", AsyncMock(return_value=[])),
        "vignette": (
            "notify_bot.scheduler.jobs.check_vignette",
            AsyncMock(return_value=_DEFAULT_VIGNETTE),
        ),
        "vignette_boleron": (
            "notify_bot.scheduler.jobs.check_vignette_boleron",
            AsyncMock(return_value=BoleronVignetteInfo(found=False)),
        ),
        "clamp": (
            "notify_bot.scheduler.jobs.check_clamp",
            AsyncMock(return_value=_DEFAULT_CLAMP),
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


# ── _build_report: top-level behavior ────────────────────────────────


@pytest.mark.asyncio
async def test_report_is_none_when_user_has_no_identifiers():
    user = {
        "user_id": 1,
        "first_name": "Test",
        "national_id": None,
        "driving_licence": None,
        "vehicles": [],
    }
    with _patched():
        message = await _report_text(user)
    assert message is None


@pytest.mark.asyncio
async def test_report_greets_with_fallback_name_when_missing():
    user = {**_FULL_USER, "first_name": None, "national_id": None, "driving_licence": None}
    with _patched():
        message = await _report_text(user)
    assert message is not None
    assert message.startswith("☀️ Good morning, there!")


# ── Licence / plate obligations sections ─────────────────────────────────────


@pytest.mark.asyncio
async def test_licence_obligations_section_included_when_obligations_found():
    units = [
        Obligation(
            unit_group=1,
            unit_group_label="Road Traffic Act and/or Insurance Code",
            obligations=["Unpaid fine"],
        )
    ]
    with _patched(licence=AsyncMock(return_value=units)):
        message = await _report_text(_FULL_USER)
    assert "🪪 <b>By driving licence:</b>" in message


@pytest.mark.asyncio
async def test_licence_obligations_section_hidden_when_clean():
    units = [
        Obligation(
            unit_group=1, unit_group_label="Road Traffic Act and/or Insurance Code", obligations=[]
        )
    ]
    with _patched(licence=AsyncMock(return_value=units)):
        message = await _report_text(_FULL_USER)
    assert "🪪 <b>By driving licence:</b>" not in message


@pytest.mark.asyncio
async def test_licence_check_failure_shows_error_line():
    with _patched(licence=AsyncMock(side_effect=MVRApiError("MVR API returned HTTP 500"))):
        message = await _report_text(_FULL_USER)
    assert (
        "🪪 <b>By driving licence:</b>\n⚠️ Check failed — tap its 🔁 button below to retry."
        in message
    )


@pytest.mark.asyncio
async def test_plate_obligations_section_included_when_obligations_found():
    units = [
        Obligation(
            unit_group=1,
            unit_group_label="Road Traffic Act and/or Insurance Code",
            obligations=["Unpaid fine"],
        )
    ]
    with _patched(plate=AsyncMock(return_value=units)):
        message = await _report_text(_FULL_USER)
    assert f"🚗 <b>By vehicle plate {PLATE} (MVR):</b>" in message


@pytest.mark.asyncio
async def test_plate_obligations_section_hidden_when_clean():
    units = [
        Obligation(
            unit_group=1, unit_group_label="Road Traffic Act and/or Insurance Code", obligations=[]
        )
    ]
    with _patched(plate=AsyncMock(return_value=units)):
        message = await _report_text(_FULL_USER)
    assert "🚗 <b>By vehicle plate" not in message


@pytest.mark.asyncio
async def test_plate_check_failure_shows_error_line():
    with _patched(plate=AsyncMock(side_effect=MVRApiError("boom"))):
        message = await _report_text(_FULL_USER)
    assert (
        f"🚗 <b>By vehicle plate {PLATE} (MVR):</b>\n⚠️ Check failed — tap its 🔁 button below"
        in message
    )


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
        message = await _report_text(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>" in message
    assert "✅ Status: Active" in message
    assert "📋 Type: Annual" in message
    assert "⚠️ Expires in 5 days!" in message


@pytest.mark.asyncio
async def test_vignette_not_found():
    with _patched():
        message = await _report_text(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>\n❌ No active vignette found." in message


@pytest.mark.asyncio
async def test_vignette_cloudflare_error_falls_back_to_boleron_and_finds_one():
    bv = BoleronVignetteInfo(
        found=True,
        active=True,
        valid_from="01.01.2026",
        valid_to="31.12.2026",
        validity_type="annual",
    )
    with _patched(
        vignette=AsyncMock(side_effect=CloudflareBlockedError("blocked")),
        vignette_boleron=AsyncMock(return_value=bv),
    ):
        message = await _report_text(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>" in message
    assert "✅ Status: Active" in message
    assert "📋 Type: Annual" in message


@pytest.mark.asyncio
async def test_vignette_bgtoll_error_falls_back_to_boleron_not_found():
    with _patched(vignette=AsyncMock(side_effect=BgtollError("connection error"))):
        message = await _report_text(_FULL_USER)
    assert f"🛣️ <b>Vignette ({PLATE}):</b>\n❌ No active vignette found." in message


# ── Wheel clamp section (parking sticker is not part of the report) ──────────


@pytest.mark.asyncio
async def test_report_never_includes_parking_sticker():
    with (
        _patched(),
        patch("notify_bot.services.sofiatraffic.check_sticker", new=AsyncMock()) as sticker,
    ):
        message = await _report_text(_FULL_USER)
    assert "Parking sticker" not in message
    sticker.assert_not_called()


@pytest.mark.asyncio
async def test_report_hides_wheel_clamp_section_when_not_clamped():
    clamp = ClampInfo(plate=PLATE, found=True, clamped=False)
    with _patched(clamp=AsyncMock(return_value=clamp)):
        message = await _report_text(_FULL_USER)
    assert "Wheel clamp" not in message


@pytest.mark.asyncio
async def test_report_includes_wheel_clamp_section_when_clamped():
    clamp = ClampInfo(plate=PLATE, found=True, clamped=True, clamped_at="10:00", location="Main St")
    with _patched(clamp=AsyncMock(return_value=clamp)):
        message = await _report_text(_FULL_USER)
    assert f"🔒 <b>Wheel clamp ({PLATE}):</b>" in message
    assert "❌ Vehicle <b>IS wheel-clamped!</b>" in message


@pytest.mark.asyncio
async def test_clamp_error_shows_failed_section():
    with _patched(clamp=AsyncMock(side_effect=SofiaTrafficError("blocked"))):
        message = await _report_text(_FULL_USER)
    assert (
        f"🔒 <b>Wheel clamp ({PLATE}):</b>\n⚠️ Check failed — tap its 🔁 button below to retry."
        in message
    )
    assert message.count("Check failed") == 1


# ── Technical Inspection (GTP) section ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gtp_found_shows_expiry_warning():
    gtp = GtpInfo(found=True, valid_to=_soon(3))
    with _patched(gtp=AsyncMock(return_value=gtp)):
        message = await _report_text(_FULL_USER)
    assert f"🔧 <b>Technical Inspection ({PLATE}):</b>" in message
    assert f"✅ Valid until: {_soon(3)}" in message
    assert "⚠️ Expires in 3 days!" in message


@pytest.mark.asyncio
async def test_gtp_not_found():
    with _patched():
        message = await _report_text(_FULL_USER)
    assert f"🔧 <b>Technical Inspection ({PLATE}):</b>\n❌ No valid inspection found." in message


@pytest.mark.asyncio
async def test_gtp_error_still_shows_section():
    with _patched(gtp=AsyncMock(side_effect=BoleronError("HTTP 400"))):
        message = await _report_text(_FULL_USER)
    assert message is not None
    assert f"🔧 <b>Technical Inspection ({PLATE}):</b>\n⚠️ Check failed" in message


# ── Civil Liability (MTPL) section ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_mtpl_active_with_insurer_and_expiry_warning():
    mtpl = MtplInfo(active=True, insurer="Bulstrad", valid_to=_soon(7))
    with _patched(mtpl=AsyncMock(return_value=mtpl)):
        message = await _report_text(_FULL_USER)
    assert f"🛡️ <b>Civil Liability / MTPL ({PLATE}):</b>" in message
    assert "✅ Active" in message
    assert "🏢 Bulstrad" in message
    assert f"📅 Valid until: {_soon(7)}" in message
    assert "⚠️ Expires in 7 days!" in message


@pytest.mark.asyncio
async def test_mtpl_inactive_shows_no_active_policy():
    with _patched():
        message = await _report_text(_FULL_USER)
    assert f"🛡️ <b>Civil Liability / MTPL ({PLATE}):</b>" in message
    assert "❌ No active policy" in message


@pytest.mark.asyncio
async def test_mtpl_error_still_shows_section():
    with _patched(mtpl=AsyncMock(side_effect=BoleronError("boom"))):
        message = await _report_text(_FULL_USER)
    assert f"🛡️ <b>Civil Liability / MTPL ({PLATE}):</b>\n⚠️ Check failed" in message


# ── Traffic Fines section ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_traffic_fines_section_leads_the_report():
    with _patched():
        message = await _report_text(_FULL_USER)
    assert message.startswith("☀️ Good morning, Test!\n\n🚔 <b>Traffic Fines:</b>")


@pytest.mark.asyncio
async def test_report_sections_in_order():
    units = [Obligation(unit_group=1, unit_group_label="KAT", obligations=["Unpaid fine"])]
    clamp = ClampInfo(plate=PLATE, found=True, clamped=True)
    with _patched(
        licence=AsyncMock(return_value=units),
        plate=AsyncMock(return_value=units),
        clamp=AsyncMock(return_value=clamp),
    ):
        message = await _report_text(_FULL_USER)
    headers = [
        "🚔 <b>Traffic Fines:",
        "🪪 <b>By driving licence:",
        f"🚗 <b>By vehicle plate {PLATE} (MVR):",
        "🔧 <b>Technical Inspection",
        "🛡️ <b>Civil Liability",
        "🛣️ <b>Vignette",
        "🔒 <b>Wheel clamp",
    ]
    positions = [message.index(header) for header in headers]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_fines_present_with_discount():
    fines = FinesResult(
        has_fines=True, count=2, total=100.0, total_discount=70.0, currency_symbol="€"
    )
    with _patched(fines=AsyncMock(return_value=fines)):
        message = await _report_text(_FULL_USER)
    assert "🚔 <b>Traffic Fines:</b>" in message
    assert "❌ 2 fine(s) — Total: 100.00 €" in message
    assert "💸 With discount: 70.00 €" in message


@pytest.mark.asyncio
async def test_fines_none_are_shown():
    with _patched():
        message = await _report_text(_FULL_USER)
    assert "🚔 <b>Traffic Fines:</b>\n✅ No fines." in message


@pytest.mark.asyncio
async def test_fines_error_still_shows_section():
    with _patched(fines=AsyncMock(side_effect=BoleronError("boom"))):
        message = await _report_text(_FULL_USER)
    assert "🚔 <b>Traffic Fines:</b>\n⚠️ Check failed" in message


# ── Fine shortcut buttons (/driver, /plate) ───────────────────────────────────

_FINE_GROUP = Obligation(
    unit_group=1,
    unit_group_label="Road Traffic Act and/or Insurance Code",
    obligations=[{"amount": 51.13, "iban": "BG64BNBG96613100147701", "currency": "EUR"}],
)


async def _callbacks(**patches) -> list[str | None]:
    with _patched(**patches):
        report = await _build_report(_FULL_USER)
    assert report is not None
    return [b.callback_data for b in report.buttons]


@pytest.mark.asyncio
async def test_report_offers_driver_button_when_licence_lookup_found_fines():
    assert await _callbacks(licence=AsyncMock(return_value=[_FINE_GROUP])) == ["cmd:driver"]


@pytest.mark.asyncio
async def test_report_offers_plate_button_when_plate_lookup_found_fines():
    assert await _callbacks(plate=AsyncMock(return_value=[_FINE_GROUP])) == [f"cmd:plate:{PLATE}"]


@pytest.mark.asyncio
async def test_report_offers_both_buttons_when_both_lookups_found_fines():
    callbacks = await _callbacks(
        licence=AsyncMock(return_value=[_FINE_GROUP]),
        plate=AsyncMock(return_value=[_FINE_GROUP]),
    )
    assert callbacks == ["cmd:driver", f"cmd:plate:{PLATE}"]


@pytest.mark.asyncio
async def test_report_has_no_buttons_when_lookups_found_no_fines():
    assert await _callbacks() == []


@pytest.mark.asyncio
async def test_failed_lookup_gets_a_retry_button_instead_of_a_fines_button():
    assert await _callbacks(licence=AsyncMock(side_effect=MVRApiError("boom"))) == ["cmd:driver"]


@pytest.mark.asyncio
async def test_report_never_carries_copy_buttons():
    """Copy buttons live in the per-fine messages /driver and /plate send, not the report."""
    with _patched(licence=AsyncMock(return_value=[_FINE_GROUP])):
        report = await _build_report(_FULL_USER)
    assert report is not None and report.buttons
    assert not any(b.copy_text for b in report.buttons)


@pytest.mark.asyncio
async def test_send_user_report_now_attaches_the_shortcut_buttons():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    with _patched(licence=AsyncMock(return_value=[_FINE_GROUP])):
        await send_user_report_now(context, _FULL_USER)
    assert context.bot.send_message.call_args.kwargs["reply_markup"] is not None


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
    user = {
        "user_id": 1,
        "first_name": "Test",
        "national_id": None,
        "driving_licence": None,
        "vehicles": [],
    }
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
        patch(
            "notify_bot.scheduler.jobs.db.get_all_approved_with_profiles",
            AsyncMock(return_value=users),
        ),
        patch("notify_bot.scheduler.jobs.random.randint", return_value=300),
    ):
        await daily_obligations_report(context)

    assert context.job_queue.run_once.call_count == 2
    names = {call.kwargs["name"] for call in context.job_queue.run_once.call_args_list}
    assert names == {"report_user_1", "report_user_2"}


@pytest.mark.asyncio
async def test_gtp_passes_plate_and_talon():
    gtp = AsyncMock(return_value=GtpInfo(found=False))
    with _patched(gtp=gtp):
        await _report_text(_FULL_USER)
    gtp.assert_awaited_once_with(car_no=PLATE, talon_no="009999999")


@pytest.mark.asyncio
async def test_gtp_without_talon_shows_section_asking_for_it():
    gtp = AsyncMock()
    with _patched(gtp=gtp):
        message = await _report_text({**_FULL_USER, "vehicles": [_vehicle(PLATE, None)]})
    gtp.assert_not_awaited()
    assert (
        f"🔧 <b>Technical Inspection ({PLATE}):</b>\n"
        "⚠️ Talon number missing — save it with /vehicles." in message
    )


# ── Several vehicles ──────────────────────────────────────────────────────────

_TWO_VEHICLES = {
    **_FULL_USER,
    "vehicles": [_vehicle(PLATE, "009999999", 1), _vehicle(OTHER_PLATE, "008888888", 2)],
}


@pytest.mark.asyncio
async def test_every_vehicle_is_checked_preferred_first():
    gtp = AsyncMock(return_value=GtpInfo(found=False))
    mtpl = AsyncMock(return_value=MtplInfo(active=False))
    plate = AsyncMock(return_value=[])
    with _patched(gtp=gtp, mtpl=mtpl, plate=plate):
        message = await _report_text(_TWO_VEHICLES)
    assert [c.kwargs for c in gtp.await_args_list] == [
        {"car_no": PLATE, "talon_no": "009999999"},
        {"car_no": OTHER_PLATE, "talon_no": "008888888"},
    ]
    assert [c.args for c in mtpl.await_args_list] == [(PLATE,), (OTHER_PLATE,)]
    assert [c.kwargs["plate_number"] for c in plate.await_args_list] == [PLATE, OTHER_PLATE]
    assert message.index(f"MTPL ({PLATE})") < message.index(f"MTPL ({OTHER_PLATE})")


@pytest.mark.asyncio
async def test_failed_checks_get_retry_buttons_naming_their_plate():
    with _patched(mtpl=AsyncMock(side_effect=BoleronError("boom"))):
        report = await _build_report(_TWO_VEHICLES)
    assert report is not None
    assert [(b.text, b.callback_data) for b in report.buttons] == [
        (f"🔁 Retry MTPL {PLATE}", f"cmd:mtpl:{PLATE}"),
        (f"🔁 Retry MTPL {OTHER_PLATE}", f"cmd:mtpl:{OTHER_PLATE}"),
    ]


@pytest.mark.asyncio
async def test_report_offers_a_fines_button_per_plate_with_fines():
    async def by_plate(*, national_id, plate_number):
        return [_FINE_GROUP] if plate_number == OTHER_PLATE else []

    with _patched(plate=AsyncMock(side_effect=by_plate)):
        report = await _build_report(_TWO_VEHICLES)
    assert report is not None
    assert [b.callback_data for b in report.buttons] == [f"cmd:plate:{OTHER_PLATE}"]


@pytest.mark.asyncio
async def test_vehicle_checks_without_national_id_skip_only_the_mvr_lookup():
    plate = AsyncMock(return_value=[])
    mtpl = AsyncMock(return_value=MtplInfo(active=False))
    with _patched(plate=plate, mtpl=mtpl):
        await _report_text({**_TWO_VEHICLES, "national_id": None})
    plate.assert_not_awaited()
    assert mtpl.await_count == 2


# ── Splitting long reports ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_personal_checks_and_main_vehicle_share_the_first_message():
    with _patched(mtpl=AsyncMock(return_value=MtplInfo(active=False))):
        report = await _build_report(_TWO_VEHICLES)
    assert report is not None and len(report.entries) == 2
    first, second = report.entries
    assert first.startswith("☀️ Good morning")
    assert "Traffic Fines" in first and f"({PLATE})" in first and OTHER_PLATE not in first
    assert f"({OTHER_PLATE})" in second and PLATE not in second


@pytest.mark.asyncio
async def test_single_vehicle_report_is_one_message():
    with _patched():
        report = await _build_report(_FULL_USER)
    assert report is not None and len(report.entries) == 1


@pytest.mark.asyncio
async def test_each_message_carries_the_buttons_for_its_own_checks():
    async def by_plate(*, national_id, plate_number):
        return [_FINE_GROUP] if plate_number == OTHER_PLATE else []

    with _patched(
        licence=AsyncMock(side_effect=MVRApiError("boom")),
        plate=AsyncMock(side_effect=by_plate),
        mtpl=AsyncMock(side_effect=BoleronError("boom")),
    ):
        report = await _build_report(_TWO_VEHICLES)
    assert report is not None
    first, second = (
        [b.callback_data for row in (kb.inline_keyboard if kb else ()) for b in row]
        for kb in report.keyboards
    )
    assert first == ["cmd:driver", f"cmd:mtpl:{PLATE}"]
    assert second == [f"cmd:plate:{OTHER_PLATE}", f"cmd:mtpl:{OTHER_PLATE}"]


@pytest.mark.asyncio
async def test_send_report_sends_each_entry_with_its_own_keyboard():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    first, second = MagicMock(), MagicMock()

    await _send_report(
        context, 1, _Report(entries=("a", "b", "c"), keyboards=(first, None, second))
    )

    calls = context.bot.send_message.call_args_list
    assert [c.kwargs["text"] for c in calls] == ["a", "b", "c"]
    assert [c.kwargs["reply_markup"] for c in calls] == [first, None, second]
