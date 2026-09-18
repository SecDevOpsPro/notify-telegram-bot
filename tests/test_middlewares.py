"""Tests for the @require_approved auth middleware (notify_bot/middlewares.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.middlewares import require_approved

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None
    return update


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approved_user_passes_through():
    handler = AsyncMock(return_value="result")
    decorated = require_approved(handler)
    update = _make_update(42)

    with patch(
        "notify_bot.middlewares.db.get_user",
        new=AsyncMock(return_value={"status": "approved"}),
    ):
        result = await decorated(update, MagicMock())

    handler.assert_awaited_once()
    assert result == "result"


@pytest.mark.asyncio
async def test_pending_user_is_blocked():
    handler = AsyncMock()
    decorated = require_approved(handler)
    update = _make_update(99)

    with patch(
        "notify_bot.middlewares.db.get_user",
        new=AsyncMock(return_value={"status": "pending"}),
    ):
        await decorated(update, MagicMock())

    handler.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_denied_user_is_blocked():
    handler = AsyncMock()
    decorated = require_approved(handler)
    update = _make_update(77)

    with patch(
        "notify_bot.middlewares.db.get_user",
        new=AsyncMock(return_value={"status": "denied"}),
    ):
        await decorated(update, MagicMock())

    handler.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_user_is_blocked():
    """User not in the database at all should be blocked."""
    handler = AsyncMock()
    decorated = require_approved(handler)
    update = _make_update(55)

    with patch(
        "notify_bot.middlewares.db.get_user",
        new=AsyncMock(return_value=None),
    ):
        await decorated(update, MagicMock())

    handler.assert_not_awaited()
    update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_button_tap_answers_callback_query():
    """Regression test: a blocked user tapping a button (e.g. the stale
    "📝 Enroll your data" keyboard) must have the tap answered, otherwise the
    button hangs on its loading spinner."""
    handler = AsyncMock()
    decorated = require_approved(handler)
    update = _make_update(77)
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()

    with patch(
        "notify_bot.middlewares.db.get_user",
        new=AsyncMock(return_value={"status": "denied"}),
    ):
        await decorated(update, MagicMock())

    handler.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()
    update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_user_via_button_stand_in_without_callback_query():
    """menu_callback passes a _ButtonUpdate with no callback_query attribute
    (it has already answered the tap) — the block path must not trip on that."""

    class _NoCallbackQuery:
        effective_user = MagicMock(id=77)
        effective_message = MagicMock(reply_text=AsyncMock())

    update = _NoCallbackQuery()
    decorated = require_approved(AsyncMock())

    with patch("notify_bot.middlewares.db.get_user", new=AsyncMock(return_value=None)):
        await decorated(update, MagicMock())

    update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_effective_user_is_silently_ignored():
    """Updates without a user (e.g. channel posts) must not raise."""
    handler = AsyncMock()
    decorated = require_approved(handler)

    update = MagicMock()
    update.effective_user = None

    with patch("notify_bot.middlewares.db.get_user", new=AsyncMock()) as mock_get:
        await decorated(update, MagicMock())

    mock_get.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_decorator_preserves_function_name():
    """functools.wraps should preserve the wrapped function's metadata."""

    async def my_handler(update, context):
        pass

    decorated = require_approved(my_handler)
    assert decorated.__name__ == "my_handler"
