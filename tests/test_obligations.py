"""Tests for the /driver and /plate handlers (notify_bot/handlers/obligations.py)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.handlers.obligations import (
    clamp_command,
    driver_command,
    fines_command,
    gtp_command,
    mtpl_command,
    plate_command,
    sticker_command,
    vehicle_command,
    vignette_command,
)
from notify_bot.services.bgtoll import CloudflareBlockedError, VignetteInfo
from notify_bot.services.boleron import (
    BoleronError,
    BoleronVignetteInfo,
    GtpInfo,
    MtplInfo,
)
from notify_bot.services.mvr import MVRApiError, Obligation
from notify_bot.services.sofiatraffic import SofiaTrafficError

_PROFILE = {
    "national_id": "1234567890",
    "driving_licence": "123456789",
    "vehicle_plate": "XH2856",
    "talon_no": "009999999",
}


def _fine(number: str) -> dict:
    return {
        "amount": 51.13,
        "iban": "BG64BNBG96613100147701",
        "bic": "BNBGBGSF",
        "paymentReason": f"ЕЛ.ФИШ СЕРИЯ K {number}",
        "currency": "EUR",
        "additionalData": {"documentSeries": "K", "documentNumber": number},
    }


def _units(*obligations) -> list[Obligation]:
    return [Obligation(unit_group=1, unit_group_label="x", obligations=list(obligations))]


def _update() -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 42
    update.message.reply_text = AsyncMock()
    update.message.reply_html = AsyncMock()
    update.effective_message = update.message
    return update


def _copied(reply_html_call) -> list[str]:
    markup = reply_html_call.kwargs["reply_markup"]
    return [b.copy_text.text for row in markup.inline_keyboard for b in row]


async def _run(handler, units: list[Obligation]) -> MagicMock:
    update = _update()
    with (
        patch(
            "notify_bot.middlewares.db.get_user", new=AsyncMock(return_value={"status": "approved"})
        ),
        patch(
            "notify_bot.handlers.obligations.db.get_profile", new=AsyncMock(return_value=_PROFILE)
        ),
        patch(
            "notify_bot.handlers.obligations.check_by_licence", new=AsyncMock(return_value=units)
        ),
        patch("notify_bot.handlers.obligations.check_by_plate", new=AsyncMock(return_value=units)),
    ):
        await handler(update, MagicMock())
    return update


@pytest.mark.parametrize("handler", [driver_command, plate_command])
@pytest.mark.asyncio
async def test_no_fines_sends_one_message_without_buttons(handler):
    update = await _run(handler, _units())

    update.message.reply_html.assert_awaited_once()
    assert update.message.reply_html.call_args.kwargs["reply_markup"] is None


@pytest.mark.parametrize("handler", [driver_command, plate_command])
@pytest.mark.asyncio
async def test_one_fine_sends_one_message_with_copy_buttons(handler):
    update = await _run(handler, _units(_fine("13247956")))

    update.message.reply_html.assert_awaited_once()
    assert "BG64BNBG96613100147701" in _copied(update.message.reply_html.call_args)


@pytest.mark.parametrize("handler", [driver_command, plate_command])
@pytest.mark.asyncio
async def test_several_fines_send_a_summary_then_one_message_each(handler):
    update = await _run(handler, _units(_fine("13247956"), _fine("13247999")))

    calls = update.message.reply_html.await_args_list
    assert len(calls) == 3
    assert calls[0].kwargs["reply_markup"] is None  # the summary carries no buttons
    for call, number in zip(calls[1:], ("13247956", "13247999"), strict=True):
        assert f"K {number}" in call.args[0]
        assert f"ЕЛ.ФИШ СЕРИЯ K {number}" in _copied(call)


# ── Error visibility ─────────────────────────────────────────────────────────


async def _run_failing(handler, check: str, exc: Exception, *, debug: bool) -> str:
    """Run *handler* with *check* raising *exc*; return the final reply text."""
    update = _update()
    with (
        patch(
            "notify_bot.middlewares.db.get_user", new=AsyncMock(return_value={"status": "approved"})
        ),
        patch(
            "notify_bot.handlers.obligations.db.get_profile", new=AsyncMock(return_value=_PROFILE)
        ),
        patch(f"notify_bot.handlers.obligations.{check}", new=AsyncMock(side_effect=exc)),
        patch("notify_bot.errors.config.is_debug_user", return_value=debug),
    ):
        await handler(update, MagicMock(args=[]))
    return update.message.reply_html.call_args.args[0]


_FAILING = [
    (driver_command, "check_by_licence", MVRApiError("HTTP 400")),
    (plate_command, "check_by_plate", MVRApiError("HTTP 400")),
    (gtp_command, "check_gtp", BoleronError("HTTP 400")),
    (mtpl_command, "check_mtpl", BoleronError("HTTP 400")),
    (fines_command, "check_fines", BoleronError("HTTP 400")),
    (sticker_command, "check_sticker", SofiaTrafficError("HTTP 400")),
    (clamp_command, "check_clamp", SofiaTrafficError("HTTP 400")),
    (vehicle_command, "check_vehicle_data", BoleronError("HTTP 400")),
]


@pytest.mark.parametrize(("handler", "check", "exc"), _FAILING)
@pytest.mark.asyncio
async def test_check_failure_is_terse_for_regular_users(handler, check, exc):
    text = await _run_failing(handler, check, exc, debug=False)
    assert "⚠️ Check failed" in text
    assert "HTTP 400" not in text


@pytest.mark.parametrize(("handler", "check", "exc"), _FAILING)
@pytest.mark.asyncio
async def test_check_failure_shows_detail_to_debug_users(handler, check, exc):
    text = await _run_failing(handler, check, exc, debug=True)
    assert "⚠️ Check failed" in text
    assert f"{type(exc).__name__}: HTTP 400" in text


# ── /vignette ────────────────────────────────────────────────────────────────


async def _run_vignette(*, bgtoll: AsyncMock, boleron: AsyncMock, debug: bool) -> str:
    update = _update()
    with (
        patch(
            "notify_bot.middlewares.db.get_user", new=AsyncMock(return_value={"status": "approved"})
        ),
        patch(
            "notify_bot.handlers.obligations.db.get_profile", new=AsyncMock(return_value=_PROFILE)
        ),
        patch("notify_bot.handlers.obligations.check_vignette", new=bgtoll),
        patch("notify_bot.handlers.obligations.check_vignette_boleron", new=boleron),
        patch("notify_bot.handlers.obligations.config.is_debug_user", return_value=debug),
        patch("notify_bot.errors.config.is_debug_user", return_value=debug),
    ):
        await vignette_command(update, MagicMock(args=[]))
    return update.message.reply_html.call_args.args[0]


_BLOCKED = AsyncMock(side_effect=CloudflareBlockedError("blocked"))
_BV = BoleronVignetteInfo(found=True, active=True, valid_to="31.12.2099")


@pytest.mark.asyncio
async def test_vignette_both_sources_failing_is_terse_for_regular_users():
    text = await _run_vignette(
        bgtoll=_BLOCKED, boleron=AsyncMock(side_effect=BoleronError("HTTP 400")), debug=False
    )
    assert "⚠️ Check failed" in text
    assert "check.bgtoll.bg" in text
    assert "HTTP 400" not in text
    assert "blocked" not in text


@pytest.mark.asyncio
async def test_vignette_both_sources_failing_shows_both_errors_to_debug_users():
    text = await _run_vignette(
        bgtoll=_BLOCKED, boleron=AsyncMock(side_effect=BoleronError("HTTP 400")), debug=True
    )
    assert "BoleronError: HTTP 400" in text
    assert "CloudflareBlockedError: blocked" in text


@pytest.mark.asyncio
async def test_vignette_fallback_result_hides_note_from_regular_users():
    text = await _run_vignette(bgtoll=_BLOCKED, boleron=AsyncMock(return_value=_BV), debug=False)
    assert "✅ Status: Active" in text
    assert "boleron.bg" not in text


@pytest.mark.asyncio
async def test_vignette_fallback_result_notes_source_for_debug_users():
    text = await _run_vignette(bgtoll=_BLOCKED, boleron=AsyncMock(return_value=_BV), debug=True)
    assert "✅ Status: Active" in text
    assert "Via boleron.bg" in text


# ── Expiry warnings ──────────────────────────────────────────────────────────


def _soon(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%d.%m.%Y")


async def _run_ok(handler, check: str, result) -> str:
    update = _update()
    with (
        patch(
            "notify_bot.middlewares.db.get_user", new=AsyncMock(return_value={"status": "approved"})
        ),
        patch(
            "notify_bot.handlers.obligations.db.get_profile", new=AsyncMock(return_value=_PROFILE)
        ),
        patch(f"notify_bot.handlers.obligations.{check}", new=AsyncMock(return_value=result)),
    ):
        await handler(update, MagicMock(args=[]))
    return update.message.reply_html.call_args.args[0]


@pytest.mark.parametrize(
    ("handler", "check", "result"),
    [
        (gtp_command, "check_gtp", GtpInfo(found=True, valid_to=_soon(3))),
        (
            mtpl_command,
            "check_mtpl",
            MtplInfo(active=True, valid_from="01.01.2026", valid_to=_soon(3)),
        ),
        (
            vignette_command,
            "check_vignette",
            VignetteInfo(
                plate="XH2856",
                country="BG",
                found=True,
                status="VALID",
                validity_date_to=f"{_soon(3)} 23:59:59",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_command_warns_about_upcoming_expiry(handler, check, result):
    text = await _run_ok(handler, check, result)
    assert "⚠️ Expires in 3 days!" in text


@pytest.mark.asyncio
async def test_gtp_no_warning_when_far_from_expiry():
    text = await _run_ok(gtp_command, "check_gtp", GtpInfo(found=True, valid_to=_soon(60)))
    assert "Expires in" not in text
