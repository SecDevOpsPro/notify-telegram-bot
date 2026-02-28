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
