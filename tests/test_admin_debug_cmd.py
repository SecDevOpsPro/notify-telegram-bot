"""Tests for /debug and /undebug (notify_bot/handlers/admin.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.handlers.admin import debug_cmd, undebug_cmd


def _make_update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    context = MagicMock()
    context.args = args or []
    return context


@pytest.mark.asyncio
async def test_non_admin_is_silently_ignored():
    update = _make_update(1)
    context = _make_context(["1"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=False),
        patch("notify_bot.handlers.admin.config.add_debug_user") as mock_add,
    ):
        await debug_cmd(update, context)

    update.message.reply_text.assert_not_awaited()
    mock_add.assert_not_called()


@pytest.mark.asyncio
async def test_debug_cmd_requires_an_argument():
    update = _make_update(1)
    context = _make_context([])

    with patch("notify_bot.handlers.admin.config.is_admin", return_value=True):
        await debug_cmd(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "Usage" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_debug_cmd_rejects_non_integer_id():
    update = _make_update(1)
    context = _make_context(["not-an-id"])

    with patch("notify_bot.handlers.admin.config.is_admin", return_value=True):
        await debug_cmd(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "Invalid" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_debug_cmd_adds_target_user():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.config.add_debug_user") as mock_add,
    ):
        await debug_cmd(update, context)

    mock_add.assert_called_once_with(555)
    update.message.reply_text.assert_awaited_once()
    assert "555" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_undebug_cmd_removes_present_user():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.config.remove_debug_user", return_value=True) as mock_rm,
    ):
        await undebug_cmd(update, context)

    mock_rm.assert_called_once_with(555)
    assert "no longer" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_undebug_cmd_reports_when_user_not_present():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.config.remove_debug_user", return_value=False),
    ):
        await undebug_cmd(update, context)

    assert "wasn't in the debug list" in update.message.reply_text.call_args[0][0]
