"""Tests for /brief (notify_bot/handlers/admin.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.handlers.admin import brief_cmd

_APPROVED_USER = {"user_id": 555, "first_name": "Ivo", "status": "approved"}
_PROFILE = {
    "user_id": 555,
    "national_id": "1234567890",
    "driving_licence": "123456789",
    "vehicle_plate": "CA1234AB",
}


def _make_update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_html = AsyncMock()
    return update


def _make_context(args: list[str] | None = None) -> MagicMock:
    context = MagicMock()
    context.args = args or []
    return context


def _last_reply(update: MagicMock) -> str:
    return update.message.reply_text.call_args_list[-1][0][0]


@pytest.mark.asyncio
async def test_non_admin_is_silently_ignored():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=False),
        patch("notify_bot.handlers.admin.send_user_report_now") as mock_send,
    ):
        await brief_cmd(update, context)

    update.message.reply_text.assert_not_awaited()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_non_integer_id():
    update = _make_update(1)
    context = _make_context(["not-an-id"])

    with patch("notify_bot.handlers.admin.config.is_admin", return_value=True):
        await brief_cmd(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "Invalid" in _last_reply(update)


@pytest.mark.asyncio
async def test_defaults_to_caller_when_no_args():
    update = _make_update(555)
    context = _make_context([])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.db.get_user", AsyncMock(return_value=_APPROVED_USER)),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=_PROFILE)),
        patch(
            "notify_bot.handlers.admin.send_user_report_now", AsyncMock(return_value=True)
        ) as mock_send,
    ):
        await brief_cmd(update, context)

    mock_send.assert_called_once()
    sent_user = mock_send.call_args[0][1]
    assert sent_user["user_id"] == 555


@pytest.mark.asyncio
async def test_rejects_unapproved_user():
    update = _make_update(1)
    context = _make_context(["555"])
    pending_user = {**_APPROVED_USER, "status": "pending"}

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.db.get_user", AsyncMock(return_value=pending_user)),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=_PROFILE)),
        patch("notify_bot.handlers.admin.send_user_report_now") as mock_send,
    ):
        await brief_cmd(update, context)

    assert "not approved" in _last_reply(update)
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_unknown_user():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.db.get_user", AsyncMock(return_value=None)),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=None)),
        patch("notify_bot.handlers.admin.send_user_report_now") as mock_send,
    ):
        await brief_cmd(update, context)

    assert "not approved" in _last_reply(update)
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_user_with_no_profile_fields():
    update = _make_update(1)
    context = _make_context(["555"])
    empty_profile = {
        "user_id": 555,
        "national_id": None,
        "driving_licence": None,
        "vehicle_plate": None,
    }

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.db.get_user", AsyncMock(return_value=_APPROVED_USER)),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=empty_profile)),
        patch("notify_bot.handlers.admin.send_user_report_now") as mock_send,
    ):
        await brief_cmd(update, context)

    assert "no profile data" in _last_reply(update)
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_db_error_is_reported_not_raised():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch(
            "notify_bot.handlers.admin.db.get_user",
            AsyncMock(side_effect=RuntimeError("db locked")),
        ),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=_PROFILE)),
        patch("notify_bot.handlers.admin.send_user_report_now") as mock_send,
    ):
        await brief_cmd(update, context)

    update.message.reply_html.assert_awaited_once()
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_report_error_is_reported_not_raised():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.db.get_user", AsyncMock(return_value=_APPROVED_USER)),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=_PROFILE)),
        patch(
            "notify_bot.handlers.admin.send_user_report_now",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await brief_cmd(update, context)

    update.message.reply_html.assert_awaited_once()
    assert "wrong" in update.message.reply_html.call_args[0][0]


@pytest.mark.asyncio
async def test_sends_report_and_confirms():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.db.get_user", AsyncMock(return_value=_APPROVED_USER)),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=_PROFILE)),
        patch(
            "notify_bot.handlers.admin.send_user_report_now", AsyncMock(return_value=True)
        ) as mock_send,
    ):
        await brief_cmd(update, context)

    mock_send.assert_called_once()
    sent_context, sent_user = mock_send.call_args[0]
    assert sent_context is context
    assert sent_user == {
        "user_id": 555,
        "first_name": "Ivo",
        "national_id": "1234567890",
        "driving_licence": "123456789",
        "vehicle_plate": "CA1234AB",
    }
    assert "✅" in _last_reply(update)
    assert "555" in _last_reply(update)


@pytest.mark.asyncio
async def test_reports_nothing_to_report_when_report_is_empty():
    update = _make_update(1)
    context = _make_context(["555"])

    with (
        patch("notify_bot.handlers.admin.config.is_admin", return_value=True),
        patch("notify_bot.handlers.admin.db.get_user", AsyncMock(return_value=_APPROVED_USER)),
        patch("notify_bot.handlers.admin.db.get_profile", AsyncMock(return_value=_PROFILE)),
        patch("notify_bot.handlers.admin.send_user_report_now", AsyncMock(return_value=False)),
    ):
        await brief_cmd(update, context)

    assert "Nothing to report" in _last_reply(update)
