"""Tests for common handlers (notify_bot/handlers/common.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.handlers.common import request_access, start

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_update(
    user_id: int, first_name: str = "Alice", username: str | None = "alice"
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = first_name
    update.effective_user.username = username
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.message = update.effective_message
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_approved_user_is_told_so():
    update = _make_update(1)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "approved"}),
        ),
        patch("notify_bot.handlers.common.db.set_user_status", new=AsyncMock()) as mock_set,
    ):
        await request_access(update, context)

    update.effective_message.reply_text.assert_awaited_once()
    assert "already have access" in update.effective_message.reply_text.call_args[0][0]
    context.bot.send_message.assert_not_awaited()
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_admin_configured():
    update = _make_update(2)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "pending"}),
        ),
        patch("notify_bot.handlers.common.config.ADMIN_TELEGRAM_ID", 0),
    ):
        await request_access(update, context)

    update.effective_message.reply_text.assert_awaited_once()
    assert "No admin is configured" in update.effective_message.reply_text.call_args[0][0]
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_request_notifies_admin_and_sets_pending():
    update = _make_update(3, first_name="Bob", username="bob")
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "pending"}),
        ),
        patch("notify_bot.handlers.common.db.set_user_status", new=AsyncMock()) as mock_set,
        patch("notify_bot.handlers.common.config.ADMIN_TELEGRAM_ID", 999),
    ):
        await request_access(update, context)

    context.bot.send_message.assert_awaited_once()
    mock_set.assert_awaited_once_with(3, "pending")
    update.effective_message.reply_text.assert_awaited_once()
    assert "sent to the admin" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_html_special_characters_in_name_are_escaped():
    """Regression test: a first_name containing '&', '<', '>' must not break
    the HTML-formatted admin notification (previously caused a silent
    'Could not reach the admin' failure for these users)."""
    update = _make_update(4, first_name="Tom & <Jerry>", username=None)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "pending"}),
        ),
        patch("notify_bot.handlers.common.db.set_user_status", new=AsyncMock()) as mock_set,
        patch("notify_bot.handlers.common.config.ADMIN_TELEGRAM_ID", 999),
    ):
        await request_access(update, context)

    context.bot.send_message.assert_awaited_once()
    sent_text = context.bot.send_message.call_args.kwargs["text"]
    assert "Tom &amp; &lt;Jerry&gt;" in sent_text
    assert "<Jerry>" not in sent_text

    mock_set.assert_awaited_once_with(4, "pending")
    update.effective_message.reply_text.assert_awaited_once()
    assert "sent to the admin" in update.effective_message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_unreachable_does_not_set_pending():
    update = _make_update(5)
    context = _make_context()
    context.bot.send_message.side_effect = Exception("boom")

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "pending"}),
        ),
        patch("notify_bot.handlers.common.db.set_user_status", new=AsyncMock()) as mock_set,
        patch("notify_bot.handlers.common.config.ADMIN_TELEGRAM_ID", 999),
    ):
        await request_access(update, context)

    update.effective_message.reply_text.assert_awaited_once()
    assert "Could not reach the admin" in update.effective_message.reply_text.call_args[0][0]
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_effective_user_is_silently_ignored():
    update = _make_update(6)
    update.effective_user = None
    context = _make_context()

    with patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()) as mock_upsert:
        await request_access(update, context)

    mock_upsert.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_effective_message_is_silently_ignored():
    update = _make_update(7)
    update.effective_message = None
    context = _make_context()

    with patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()) as mock_upsert:
        await request_access(update, context)

    mock_upsert.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()


# ── /start ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_new_user_points_to_request_then_enroll():
    update = _make_update(10)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch("notify_bot.handlers.common.db.get_user", new=AsyncMock(return_value=None)),
    ):
        await start(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "/request" in msg
    assert "/enroll" in msg


@pytest.mark.asyncio
async def test_start_pending_user_points_to_enroll_after_approval():
    update = _make_update(11)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "pending"}),
        ),
    ):
        await start(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "pending" in msg
    assert "/enroll" in msg


@pytest.mark.asyncio
async def test_start_approved_without_profile_suggests_enroll():
    update = _make_update(12)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "approved"}),
        ),
        patch("notify_bot.handlers.common.db.get_profile", new=AsyncMock(return_value=None)),
    ):
        await start(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "/enroll" in msg


@pytest.mark.asyncio
async def test_start_approved_with_profile_skips_enroll_prompt():
    update = _make_update(13)
    context = _make_context()
    profile = {
        "national_id": "1234567890",
        "driving_licence": None,
        "vehicle_plate": None,
        "talon_no": None,
    }

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "approved"}),
        ),
        patch("notify_bot.handlers.common.db.get_profile", new=AsyncMock(return_value=profile)),
    ):
        await start(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "/enroll" not in msg
    assert "/help" in msg


@pytest.mark.asyncio
async def test_start_denied_user_message_unchanged():
    update = _make_update(14)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "denied"}),
        ),
    ):
        await start(update, context)

    msg = update.message.reply_text.call_args[0][0]
    assert "denied" in msg
