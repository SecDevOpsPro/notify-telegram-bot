"""Tests for /vehicles and /addvehicle (notify_bot/handlers/vehicles.py), against a real DB."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ConversationHandler

from notify_bot import db
from notify_bot.handlers.enroll import myinfo_command
from notify_bot.handlers.vehicles import (
    ASK_PLATE,
    ASK_TALON,
    add_start,
    back_to_plate,
    received_plate,
    received_talon,
    stale_add_vehicle_button,
    vehicle_button,
    vehicles_command,
)

UID = 7


@pytest.fixture(autouse=True)
async def _approved_user(tmp_db):
    await db.init_db()
    await db.upsert_user(UID, "user", "User")
    await db.set_user_status(UID, "approved")


def _command_update(text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_user.id = UID
    update.callback_query = None
    update.message.text = text
    update.message.reply_html = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    return update


def _button_update(data: str) -> MagicMock:
    update = _command_update()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


def _context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    return context


def _buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ── /vehicles ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vehicles_lists_main_first_with_manage_buttons():
    await db.save_vehicle(UID, "CB1234AB", "009999999")
    await db.save_vehicle(UID, "PB5678CD")
    update = _command_update()

    await vehicles_command(update, _context())

    text = update.message.reply_html.call_args.args[0]
    assert text.index("• <code>CB1234AB</code> —") < text.index("• <code>PB5678CD</code> —")
    assert text.endswith("Main vehicle: <code>CB1234AB</code>")
    assert "talon <code>009999999</code>" in text
    assert "no talon" in text
    assert _buttons(update.message.reply_html.call_args.kwargs["reply_markup"]) == [
        "veh:main:PB5678CD",
        "veh:ask:CB1234AB",
        "veh:ask:PB5678CD",
        "cmd:addvehicle",
    ]


@pytest.mark.asyncio
async def test_vehicles_with_none_saved_offers_add():
    update = _command_update()

    await vehicles_command(update, _context())

    assert "No vehicles saved yet" in update.message.reply_html.call_args.args[0]
    markup = update.message.reply_html.call_args.kwargs["reply_markup"]
    assert _buttons(markup) == ["cmd:addvehicle"]


@pytest.mark.asyncio
async def test_vehicles_at_limit_hides_add_button():
    for i in range(db.MAX_VEHICLES):
        await db.save_vehicle(UID, f"CB{1000 + i}AB")
    update = _command_update()

    await vehicles_command(update, _context())

    markup = update.message.reply_html.call_args.kwargs["reply_markup"]
    assert "cmd:addvehicle" not in _buttons(markup)


@pytest.mark.asyncio
async def test_make_main_button_switches_preferred_and_refreshes_list():
    await db.save_vehicle(UID, "CB1234AB")
    await db.save_vehicle(UID, "PB5678CD")
    update = _button_update("veh:main:PB5678CD")

    await vehicle_button(update, _context())

    assert (await db.get_profile(UID))["vehicle_plate"] == "PB5678CD"
    assert "now your main vehicle" in update.callback_query.answer.call_args.args[0]
    refreshed = update.callback_query.edit_message_text.call_args.args[0]
    assert refreshed.index("PB5678CD") < refreshed.index("CB1234AB")


@pytest.mark.asyncio
async def test_remove_button_asks_before_deleting():
    await db.save_vehicle(UID, "CB1234AB")
    update = _button_update("veh:ask:CB1234AB")

    await vehicle_button(update, _context())

    assert await db.get_vehicle(UID, "CB1234AB") is not None
    markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert _buttons(markup) == ["veh:del:CB1234AB", "veh:list:"]


@pytest.mark.asyncio
async def test_keep_button_leaves_vehicle_and_restores_list():
    await db.save_vehicle(UID, "CB1234AB")
    update = _button_update("veh:list:")

    await vehicle_button(update, _context())

    assert await db.get_vehicle(UID, "CB1234AB") is not None
    assert "CB1234AB" in update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_remove_button_on_last_vehicle_clears_main():
    await db.save_vehicle(UID, "CB1234AB")
    update = _button_update("veh:del:CB1234AB")

    await vehicle_button(update, _context())

    assert await db.list_vehicles(UID) == []
    assert (await db.get_profile(UID))["vehicle_plate"] is None
    assert "No vehicles saved yet" in update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_stale_button_for_removed_vehicle_says_so():
    update = _button_update("veh:del:CB1234AB")

    await vehicle_button(update, _context())

    assert "no longer one of your vehicles" in update.callback_query.answer.call_args.args[0]


# ── /addvehicle ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_vehicle_flow_saves_plate_and_talon():
    await db.save_vehicle(UID, "CB1234AB")
    context = _context()

    assert await add_start(_command_update(), context) == ASK_PLATE
    assert await received_plate(_command_update(" pb5678cd "), context) == ASK_TALON
    update = _command_update("008888888")
    assert await received_talon(update, context) == ConversationHandler.END

    vehicle = await db.get_vehicle(UID, "PB5678CD")
    assert vehicle is not None and vehicle["talon_no"] == "008888888"
    # Adding never takes over as main.
    assert (await db.get_profile(UID))["vehicle_plate"] == "CB1234AB"
    assert "<code>PB5678CD</code> saved" in update.message.reply_html.call_args.args[0]
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_add_vehicle_rejects_invalid_plate_and_talon():
    context = _context()

    assert await received_plate(_command_update("not a plate"), context) == ASK_PLATE
    await received_plate(_command_update("PB5678CD"), context)
    assert await received_talon(_command_update("12"), context) == ASK_TALON
    assert await db.list_vehicles(UID) == []


@pytest.mark.asyncio
async def test_add_vehicle_refused_at_limit():
    for i in range(db.MAX_VEHICLES):
        await db.save_vehicle(UID, f"CB{1000 + i}AB")
    update = _command_update()

    assert await add_start(update, _context()) == ConversationHandler.END
    assert "remove one with /vehicles" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_single_vehicle_is_not_labelled_main():
    await db.save_vehicle(UID, "CB1234AB", "009999999")
    update = _command_update()

    await vehicles_command(update, _context())

    assert "Main vehicle" not in update.message.reply_html.call_args.args[0]


@pytest.mark.asyncio
async def test_add_vehicle_steps_show_wizard_buttons():
    context = _context()
    start = _command_update()
    await add_start(start, context)
    plate_step = _command_update("PB5678CD")
    await received_plate(plate_step, context)

    assert _buttons(start.message.reply_html.call_args.kwargs["reply_markup"]) == ["addveh:cancel"]
    assert _buttons(plate_step.message.reply_html.call_args.kwargs["reply_markup"]) == [
        "addveh:back",
        "addveh:cancel",
    ]


@pytest.mark.asyncio
async def test_back_button_returns_to_plate_step():
    context = _context()
    await received_plate(_command_update("PB5678CD"), context)
    update = _button_update("addveh:back")

    assert await back_to_plate(update, context) == ASK_PLATE
    update.callback_query.answer.assert_awaited_once()
    assert "Step 1 of 2" in update.message.reply_html.call_args.args[0]


@pytest.mark.asyncio
async def test_stale_wizard_button_is_answered_and_cleared():
    update = _button_update("addveh:back")
    update.callback_query.edit_message_reply_markup = AsyncMock()

    await stale_add_vehicle_button(update, _context())

    assert "ended" in update.callback_query.answer.call_args.args[0]
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


# ── /myinfo ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_myinfo_shows_personal_data_and_vehicles():
    await db.upsert_profile(UID, national_id="1234567890", driving_licence="DA2123456")
    await db.save_vehicle(UID, "CB1234AB", "009999999")
    await db.save_vehicle(UID, "PB5678CD")
    update = _command_update()

    await myinfo_command(update, _context())

    text = update.message.reply_html.call_args.args[0]
    assert "<code>1234567890</code>" in text
    assert "<code>DA2123456</code>" in text
    assert "• <code>CB1234AB</code> — talon <code>009999999</code>" in text
    assert "• <code>PB5678CD</code> — ⚠️ no talon" in text
    assert text.endswith("Main vehicle: <code>CB1234AB</code>")


@pytest.mark.asyncio
async def test_myinfo_without_data_points_to_enroll():
    update = _command_update()

    await myinfo_command(update, _context())

    assert "/enroll" in update.message.reply_text.call_args.args[0]
    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert _buttons(markup) == ["cmd:enroll"]
