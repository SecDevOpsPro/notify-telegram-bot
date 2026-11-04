"""Tests for /alerts (notify_bot/handlers/alerts.py), against a real DB."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notify_bot import db
from notify_bot.handlers.alerts import alerts_button, alerts_command
from notify_bot.report_checks import PERSONAL_SCOPE

UID = 7
PLATE = "CB1234AB"
OTHER = "PB5678CD"


@pytest.fixture(autouse=True)
async def _approved_user(tmp_db):
    await db.init_db()
    await db.upsert_user(UID, "user", "User")
    await db.set_user_status(UID, "approved")


def _command_update() -> MagicMock:
    update = MagicMock()
    update.effective_user.id = UID
    update.callback_query = None
    update.message.reply_html = AsyncMock()
    update.effective_message = update.message
    return update


def _button_update(data: str) -> MagicMock:
    update = _command_update()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def _tap(data: str) -> MagicMock:
    update = _button_update(data)
    await alerts_button(update, MagicMock())
    return update.callback_query


def _edited_keyboard(query: MagicMock):
    return query.edit_message_text.await_args.kwargs["reply_markup"]


@pytest.mark.asyncio
async def test_alerts_shows_a_choice_of_personal_and_each_vehicle():
    await db.upsert_profile(UID, national_id="1234567890", driving_licence="123456789")
    await db.save_vehicle(UID, PLATE, "009999999")
    await db.save_vehicle(UID, OTHER, "009999998")
    await db.set_check_enabled(UID, OTHER, "sticker", enabled=False)

    update = _command_update()
    await alerts_command(update, MagicMock())

    keyboard = update.message.reply_html.await_args.kwargs["reply_markup"]
    assert _labels(keyboard) == [
        "👤 Personal · 2/2",
        f"🚗 {PLATE} · 6/6",
        f"🚗 {OTHER} · 5/6",
    ]


@pytest.mark.asyncio
async def test_alerts_skips_the_choice_when_only_one_option_applies():
    await db.save_vehicle(UID, PLATE, "009999999")

    update = _command_update()
    await alerts_command(update, MagicMock())

    labels = _labels(update.message.reply_html.await_args.kwargs["reply_markup"])
    # No national ID: no personal checks and no MVR plate check.  Nothing to go back to.
    assert labels == ["✅ Inspection", "✅ MTPL", "✅ Vignette", "✅ Sticker", "✅ Clamp"]


@pytest.mark.asyncio
async def test_alerts_with_no_data_points_to_enroll():
    update = _command_update()
    await alerts_command(update, MagicMock())

    assert "/enroll" in update.message.reply_html.await_args.args[0]
    assert update.message.reply_html.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_toggle_switches_a_check_off_and_back_on():
    await db.save_vehicle(UID, PLATE, "009999999")

    query = await _tap(f"alerts:t:{PLATE}:sticker")
    assert await db.list_opt_outs(UID) == {PLATE: {"sticker"}}
    assert "❌ Sticker" in _labels(_edited_keyboard(query))

    query = await _tap(f"alerts:t:{PLATE}:sticker")
    assert await db.list_opt_outs(UID) == {}
    assert "✅ Sticker" in _labels(_edited_keyboard(query))


@pytest.mark.asyncio
async def test_toggle_personal_check_uses_the_personal_scope():
    await db.upsert_profile(UID, national_id="1234567890", driving_licence="123456789")

    await _tap("alerts:t::fines")

    assert await db.list_opt_outs(UID) == {PERSONAL_SCOPE: {"fines"}}


@pytest.mark.asyncio
async def test_back_button_returns_to_the_choice():
    await db.save_vehicle(UID, PLATE, "009999999")
    await db.save_vehicle(UID, OTHER, "009999998")

    query = await _tap(f"alerts:open:{PLATE}")
    assert "⬅ Back" in _labels(_edited_keyboard(query))

    query = await _tap("alerts:menu")
    assert _labels(_edited_keyboard(query)) == [f"🚗 {PLATE} · 5/5", f"🚗 {OTHER} · 5/5"]


@pytest.mark.asyncio
async def test_toggle_for_a_removed_vehicle_saves_nothing_and_says_so():
    await db.save_vehicle(UID, PLATE, "009999999")
    await db.save_vehicle(UID, OTHER, "009999998")
    await db.delete_vehicle(UID, OTHER)

    query = await _tap(f"alerts:t:{OTHER}:sticker")

    assert await db.list_opt_outs(UID) == {}
    query.answer.assert_awaited_once_with("That's no longer available.")


@pytest.mark.asyncio
async def test_toggle_ignores_a_check_that_does_not_apply():
    await db.save_vehicle(UID, PLATE, "009999999")

    await _tap(f"alerts:t:{PLATE}:fines")

    assert await db.list_opt_outs(UID) == {}
