"""Tests for notify_bot/handlers/enroll.py — _save_and_confirm edge cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ConversationHandler

from notify_bot.handlers.enroll import _save_and_confirm


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
