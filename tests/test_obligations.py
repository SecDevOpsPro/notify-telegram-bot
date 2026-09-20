"""Tests for the /driver and /plate handlers (notify_bot/handlers/obligations.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.handlers.obligations import driver_command, plate_command
from notify_bot.services.mvr import Obligation

_PROFILE = {
    "national_id": "1234567890",
    "driving_licence": "123456789",
    "vehicle_plate": "XH2856",
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
