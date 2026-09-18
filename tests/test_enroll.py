"""Tests for notify_bot/handlers/enroll.py — _save_and_confirm edge cases."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.error import BadRequest
from telegram.ext import ConversationHandler

from notify_bot.handlers.enroll import (
    ASK_LICENCE,
    ASK_NATIONAL_ID,
    _ask_licence,
    _save_and_confirm,
    back_to_licence,
    back_to_national_id,
    build_enroll_handler,
    build_stale_enroll_button_handler,
    cancel,
    enroll_start,
    skip_national_id,
    stale_enroll_button,
)


def _make_update(user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_html = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.user_data = {
        "enroll_national_id": "1234567890",
        "enroll_licence": "12345",
        "enroll_plate": "CB1234AB",
        "enroll_talon": "123456",
    }
    return context


@pytest.mark.asyncio
async def test_save_and_confirm_reports_saved_profile():
    update = _make_update()
    context = _make_context()
    saved_profile = {
        "national_id": "1234567890",
        "driving_licence": "12345",
        "vehicle_plate": "CB1234AB",
        "talon_no": "123456",
    }

    with (
        patch("notify_bot.handlers.enroll.db.upsert_profile", new=AsyncMock()),
        patch(
            "notify_bot.handlers.enroll.db.get_profile",
            new=AsyncMock(return_value=saved_profile),
        ),
    ):
        state = await _save_and_confirm(update, context)

    assert state == ConversationHandler.END
    update.message.reply_html.assert_awaited_once()
    assert "Profile saved" in update.message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_save_and_confirm_handles_db_failure():
    update = _make_update()
    context = _make_context()

    with (
        patch(
            "notify_bot.handlers.enroll.db.upsert_profile",
            new=AsyncMock(side_effect=Exception("db down")),
        ),
        patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock()),
    ):
        state = await _save_and_confirm(update, context)

    assert state == ConversationHandler.END
    update.message.reply_html.assert_awaited_once()
    assert "went wrong" in update.message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_save_and_confirm_does_not_crash_when_refetch_returns_none():
    """
    Regression test: db.upsert_profile succeeding but the immediate
    db.get_profile re-fetch coming back empty (e.g. a concurrent /unenroll)
    must not crash with AttributeError on profile.get(...).
    """
    update = _make_update()
    context = _make_context()

    with (
        patch("notify_bot.handlers.enroll.db.upsert_profile", new=AsyncMock()),
        patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)),
    ):
        state = await _save_and_confirm(update, context)

    assert state == ConversationHandler.END
    update.message.reply_text.assert_awaited_once()
    assert "couldn't confirm" in update.message.reply_text.call_args[0][0]


# ── enroll_start (command vs. menu-button entry points) ─────────────────────


@pytest.mark.asyncio
async def test_enroll_start_via_command_does_not_touch_callback_query():
    """/enroll typed as a command: no callback_query to answer."""
    update = _make_update()
    update.callback_query = None
    context = _make_context()

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        state = await enroll_start(update, context)

    assert state == ASK_NATIONAL_ID
    update.message.reply_html.assert_awaited_once()
    assert "Step 1 of 4" in update.message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_enroll_start_via_menu_button_answers_callback_and_replies():
    """The '📝 Enroll your data' button: a callback_query with no top-level
    .message — enroll_start must answer it and reply via .effective_message
    rather than crash on a None .message."""
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        state = await enroll_start(update, context)

    assert state == ASK_NATIONAL_ID
    update.callback_query.answer.assert_awaited_once()
    update.effective_message.reply_html.assert_awaited_once()
    assert "Step 1 of 4" in update.effective_message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_enroll_start_shows_current_national_id_when_profile_exists():
    update = _make_update()
    update.callback_query = None
    context = _make_context()
    profile = {"national_id": "1234567890"}

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=profile)):
        await enroll_start(update, context)

    text = update.message.reply_html.call_args[0][0]
    assert "1234567890" in text


# ── Skip / Cancel buttons ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_via_button_answers_callback_and_advances_step():
    """Tapping '⏭ Skip' is a callback_query with no top-level .message —
    the step handler must answer it and reply via .effective_message."""
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()
    context.user_data = {}
    saved_profile = {"national_id": "9999999999"}

    with patch(
        "notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=saved_profile)
    ):
        state = await skip_national_id(update, context)

    assert state == ASK_LICENCE
    update.callback_query.answer.assert_awaited_once()
    update.effective_message.reply_html.assert_awaited_once()
    assert "Step 2 of 4" in update.effective_message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_skip_keeps_value_typed_earlier_this_run_after_going_back():
    """Regression test: if the user typed a value, moved on, then came back
    via /back, that typed-this-run value must survive a Skip tap too — not
    get silently discarded in favor of a stale (or absent) DB value."""
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()
    context.user_data = {"enroll_national_id": "1234567890"}

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        state = await skip_national_id(update, context)

    assert state == ASK_LICENCE
    assert context.user_data["enroll_national_id"] == "1234567890"


@pytest.mark.asyncio
async def test_skip_rejected_when_no_saved_value():
    """Skipping a field with no session-typed value and no saved DB value
    must stay on the same step, not silently advance."""
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()
    context.user_data = {}

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        state = await skip_national_id(update, context)

    assert state == ASK_NATIONAL_ID
    update.callback_query.answer.assert_awaited_once()
    update.effective_message.reply_text.assert_awaited_once()
    assert "don't have a saved" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_cancel_via_button_answers_callback_and_clears_state():
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()

    state = await cancel(update, context)

    assert state == ConversationHandler.END
    update.callback_query.answer.assert_awaited_once()
    assert context.user_data == {}
    update.effective_message.reply_text.assert_awaited_once()
    assert "cancelled" in update.effective_message.reply_text.call_args[0][0]


# ── Stale buttons (tapped after the wizard has ended) ───────────────────────


def _button_tap(data: str) -> Update:
    user = User(id=1, first_name="T", is_bot=False)
    chat = Chat(id=1, type="private")
    message = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user)
    query = CallbackQuery(id="1", from_user=user, chat_instance="x", data=data, message=message)
    return Update(update_id=1, callback_query=query)


@pytest.mark.parametrize("data", ["enroll:cancel", "enroll:skip", "enroll:back"])
def test_wizard_button_with_no_active_conversation_reaches_stale_handler(data):
    """Regression test: after /cancel ended the wizard, tapping the old Cancel
    button matched no handler, so it was never answered and hung on its spinner.
    ConversationHandler ignores it once no conversation is active — the stale
    handler registered after it must claim it instead."""
    update = _button_tap(data)

    assert build_enroll_handler().check_update(update) is None
    assert build_stale_enroll_button_handler().check_update(update)


def test_stale_handler_ignores_non_wizard_buttons():
    assert not build_stale_enroll_button_handler().check_update(_button_tap("cmd:help"))


@pytest.mark.asyncio
async def test_stale_enroll_button_answers_and_clears_keyboard():
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()

    await stale_enroll_button(update, _make_context())

    update.callback_query.answer.assert_awaited_once()
    assert "/enroll" in update.callback_query.answer.call_args[0][0]
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_stale_enroll_button_survives_uneditable_message():
    """A message too old to edit (BadRequest) must not turn the tap into an error."""
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock(
        side_effect=BadRequest("Message can't be edited")
    )

    await stale_enroll_button(update, _make_context())

    update.callback_query.answer.assert_awaited_once()


# ── Back navigation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step1_keyboard_has_no_back_button():
    update = _make_update()
    update.callback_query = None
    context = _make_context()

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        await enroll_start(update, context)

    keyboard = update.message.reply_html.call_args.kwargs["reply_markup"]
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "enroll:back" not in buttons


@pytest.mark.asyncio
async def test_step2_keyboard_has_back_button():
    update = _make_update()
    update.callback_query = None
    context = _make_context()

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        state = await _ask_licence(update, context)

    assert state == ASK_LICENCE
    keyboard = update.message.reply_html.call_args.kwargs["reply_markup"]
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "enroll:back" in buttons


@pytest.mark.asyncio
async def test_back_to_national_id_answers_callback_and_reasks_step1():
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        state = await back_to_national_id(update, context)

    assert state == ASK_NATIONAL_ID
    update.callback_query.answer.assert_awaited_once()
    assert "Step 1 of 4" in update.effective_message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_back_to_licence_reasks_step2():
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        state = await back_to_licence(update, context)

    assert state == ASK_LICENCE
    update.callback_query.answer.assert_awaited_once()
    assert "Step 2 of 4" in update.effective_message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_back_shows_value_typed_this_run_not_stale_db_value():
    """Regression test: after typing a value and stepping forward, going
    /back to that step must show what was just typed — not the (possibly
    different, possibly absent) value still sitting in the DB."""
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()
    context.user_data = {"enroll_national_id": "1111111111"}
    stale_db_profile = {"national_id": "0000000000"}

    with patch(
        "notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=stale_db_profile)
    ):
        state = await back_to_national_id(update, context)

    assert state == ASK_NATIONAL_ID
    text = update.effective_message.reply_html.call_args[0][0]
    assert "1111111111" in text
    assert "0000000000" not in text


@pytest.mark.asyncio
async def test_unsaved_value_is_flagged_as_not_yet_saved():
    """A value typed this run (not yet persisted) must be visibly marked as
    such, so the user can tell it apart from what's actually in the DB."""
    update = _make_update()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    context = _make_context()
    context.user_data = {"enroll_national_id": "1111111111"}

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)):
        await back_to_national_id(update, context)

    text = update.effective_message.reply_html.call_args[0][0]
    assert "not yet saved" in text


@pytest.mark.asyncio
async def test_saved_db_value_is_not_flagged_as_unsaved():
    update = _make_update()
    update.callback_query = None
    context = _make_context()
    context.user_data = {}
    profile = {"national_id": "9999999999"}

    with patch("notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=profile)):
        await enroll_start(update, context)

    text = update.message.reply_html.call_args[0][0]
    assert "9999999999" in text
    assert "not yet saved" not in text
