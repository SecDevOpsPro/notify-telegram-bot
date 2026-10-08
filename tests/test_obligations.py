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
from notify_bot.services.sofiatraffic import ClampInfo, SofiaTrafficError, StickerInfo

_PROFILE = {
    "national_id": "1234567890",
    "driving_licence": "123456789",
    "vehicle_plate": "XH2856",
}


def _vehicle(plate: str, talon: str | None, vehicle_id: int = 1) -> dict:
    return {
        "id": vehicle_id,
        "user_id": 42,
        "plate": plate,
        "talon_no": talon,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


_VEHICLES = [_vehicle("XH2856", "009999999")]


@pytest.fixture(autouse=True)
def _saved_vehicles():
    """Every test's user has one saved (main) vehicle unless it patches its own."""
    with patch(
        "notify_bot.handlers.obligations.db.list_vehicles", new=AsyncMock(return_value=_VEHICLES)
    ) as mock:
        yield mock


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
        await handler(update, MagicMock(args=[]))
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


# ── /gtp plate + talon resolution ────────────────────────────────────────────


async def _run_gtp(args: list[str], vehicles: list[dict]) -> tuple[MagicMock, AsyncMock]:
    update = _update()
    check = AsyncMock(return_value=GtpInfo(found=False))
    with (
        patch(
            "notify_bot.middlewares.db.get_user", new=AsyncMock(return_value={"status": "approved"})
        ),
        patch(
            "notify_bot.handlers.obligations.db.list_vehicles", new=AsyncMock(return_value=vehicles)
        ),
        patch("notify_bot.handlers.obligations.check_gtp", new=check),
    ):
        await gtp_command(update, MagicMock(args=args))
    return update, check


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], ("XH2856", "009999999")),  # main vehicle's plate + talon
        (["xh2856"], ("XH2856", "009999999")),  # own plate → stored talon
        (["cb2222bb"], ("CB2222BB", "008888888")),  # another saved vehicle → its talon
        (["CB1111AA", "001234567"], ("CB1111AA", "001234567")),  # ad-hoc pair
    ],
)
@pytest.mark.asyncio
async def test_gtp_resolves_plate_and_talon(args, expected):
    _, check = await _run_gtp(args, [*_VEHICLES, _vehicle("CB2222BB", "008888888", 2)])
    check.assert_awaited_once_with(car_no=expected[0], talon_no=expected[1])


@pytest.mark.parametrize(
    ("args", "vehicles"),
    [
        (["CB1111AA"], _VEHICLES),  # someone else's plate, no talon given
        ([], [_vehicle("XH2856", None)]),  # saved without a talon
        ([], []),  # no saved vehicles at all
    ],
)
@pytest.mark.asyncio
async def test_gtp_asks_for_talon_when_missing(args, vehicles):
    update, check = await _run_gtp(args, vehicles)
    check.assert_not_awaited()
    assert "talon" in update.message.reply_html.call_args.args[0]


# ── /sticker not-found result ────────────────────────────────────────────────


async def test_sticker_not_found_warns_source_is_unreliable():
    text = await _run_ok(sticker_command, "check_sticker", StickerInfo(plate="XH2856"))
    assert "No active parking sticker found." in text
    assert "unreliable" in text
    assert "Check manually" not in text  # the site itself gives the same wrong answer


async def test_clamp_not_clamped_warns_source_is_unreliable():
    text = await _run_ok(clamp_command, "check_clamp", ClampInfo(plate="XH2856"))
    assert "not</b> wheel-clamped" in text
    assert "unreliable" in text
    assert "Check manually" not in text


# ── Choosing the vehicle ─────────────────────────────────────────────────────


async def _run_mtpl(args: list[str]) -> tuple[MagicMock, AsyncMock]:
    update = _update()
    check = AsyncMock(return_value=MtplInfo(active=False))
    with (
        patch(
            "notify_bot.middlewares.db.get_user", new=AsyncMock(return_value={"status": "approved"})
        ),
        patch("notify_bot.handlers.obligations.check_mtpl", new=check),
    ):
        await mtpl_command(update, MagicMock(args=args))
    return update, check


@pytest.mark.asyncio
async def test_plate_command_without_args_uses_main_vehicle_and_offers_the_others(_saved_vehicles):
    _saved_vehicles.return_value = [*_VEHICLES, _vehicle("CB2222BB", None, 2)]
    update, check = await _run_mtpl([])
    check.assert_awaited_once_with("XH2856")
    progress = update.message.reply_text.call_args
    assert "XH2856" in progress.args[0]
    keyboard = progress.kwargs["reply_markup"]
    assert [(b.text, b.callback_data) for row in keyboard.inline_keyboard for b in row] == [
        ("🚘 Lookup CB2222BB", "cmd:mtpl:CB2222BB")
    ]


@pytest.mark.asyncio
async def test_plate_command_with_one_vehicle_offers_no_others():
    update, _ = await _run_mtpl([])
    assert update.message.reply_text.call_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_plate_argument_wins_over_main_vehicle():
    _, check = await _run_mtpl([" cb2222bb "])
    check.assert_awaited_once_with("CB2222BB")


@pytest.mark.asyncio
async def test_invalid_plate_argument_is_rejected_before_checking():
    update, check = await _run_mtpl(["A<B"])
    check.assert_not_awaited()
    assert "Invalid plate" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_plate_command_without_vehicles_asks_for_a_plate(_saved_vehicles):
    _saved_vehicles.return_value = []
    update, check = await _run_mtpl([])
    check.assert_not_awaited()
    assert "No plate found" in update.message.reply_html.call_args.args[0]
