"""Tests for notify_bot/handlers/enroll.py — _save_and_confirm edge cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ConversationHandler

from notify_bot.handlers.enroll import ASK_NATIONAL_ID, _save_and_confirm, enroll_start


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

    with patch(
        "notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)
    ):
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

    with patch(
        "notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=None)
    ):
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

    with patch(
        "notify_bot.handlers.enroll.db.get_profile", new=AsyncMock(return_value=profile)
    ):
        await enroll_start(update, context)

    text = update.message.reply_html.call_args[0][0]
    assert "1234567890" in text
