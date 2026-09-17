"""Tests for common handlers (notify_bot/handlers/common.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notify_bot.handlers.common import help_command, list_commands_command, request_access, start

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
    update.effective_message.reply_html = AsyncMock()
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

    update.effective_message.reply_html.assert_awaited_once()
    assert "Could not reach the admin" in update.effective_message.reply_html.call_args[0][0]
    update.effective_message.reply_text.assert_not_awaited()
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

    call = update.message.reply_text.call_args
    msg = call[0][0]
    assert "/enroll" in msg
    keyboard = call.kwargs["reply_markup"]
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any(b.callback_data == "cmd:enroll" for b in buttons)


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

    call = update.message.reply_text.call_args
    msg = call[0][0]
    assert "/enroll" not in msg
    assert "/help" in msg
    keyboard = call.kwargs["reply_markup"]
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cmd:driver" in buttons
    assert "cmd:enroll" not in buttons
    assert "cmd:pending" not in buttons


@pytest.mark.asyncio
async def test_start_approved_with_profile_and_admin_also_gets_admin_buttons():
    update = _make_update(999)
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
        patch("notify_bot.handlers.common.config.ADMIN_TELEGRAM_ID", 999),
    ):
        await start(update, context)

    call = update.message.reply_text.call_args
    keyboard = call.kwargs["reply_markup"]
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cmd:driver" in buttons
    assert "cmd:pending" in buttons


@pytest.mark.asyncio
async def test_start_approved_profile_lookup_failure_is_reported():
    """
    Regression test: db.get_profile raising for an approved user must be
    caught and reported, not left to propagate past start() unhandled.
    """
    update = _make_update(15)
    context = _make_context()

    with (
        patch("notify_bot.handlers.common.db.upsert_user", new=AsyncMock()),
        patch(
            "notify_bot.handlers.common.db.get_user",
            new=AsyncMock(return_value={"status": "approved"}),
        ),
        patch(
            "notify_bot.handlers.common.db.get_profile",
            new=AsyncMock(side_effect=Exception("db down")),
        ),
    ):
        await start(update, context)

    update.effective_message.reply_html.assert_awaited_once()
    assert "went wrong" in update.effective_message.reply_html.call_args[0][0]
    update.message.reply_text.assert_not_awaited()


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


# ── /help ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_no_effective_user_is_silently_ignored():
    update = _make_update(20)
    update.effective_user = None
    context = _make_context()

    with patch(
        "notify_bot.handlers.common.menu.get_user_phase", new=AsyncMock()
    ) as mock_phase:
        await help_command(update, context)

    mock_phase.assert_not_awaited()
    update.effective_message.reply_html.assert_not_awaited()


@pytest.mark.asyncio
async def test_help_list_commands_arg_bypasses_phase_menu():
    """/help list-commands must show the full static reference instead of
    the phase-appropriate button menu."""
    update = _make_update(20)
    context = _make_context()
    context.args = ["list-commands"]

    with patch(
        "notify_bot.handlers.common.menu.get_user_phase", new=AsyncMock()
    ) as mock_phase:
        await help_command(update, context)

    mock_phase.assert_not_awaited()
    update.effective_message.reply_html.assert_awaited_once()
    text = update.effective_message.reply_html.call_args[0][0]
    assert "All Commands" in text
    assert "/driver" in text


@pytest.mark.asyncio
async def test_list_commands_command_hides_admin_section_for_non_admin():
    update = _make_update(20)
    context = _make_context()

    with patch("notify_bot.handlers.common.config.is_admin", return_value=False):
        await list_commands_command(update, context)

    text = update.effective_message.reply_html.call_args[0][0]
    assert "/driver" in text
    assert "Admin only" not in text


@pytest.mark.asyncio
async def test_list_commands_command_shows_admin_section_for_admin():
    update = _make_update(999)
    context = _make_context()

    with patch("notify_bot.handlers.common.config.is_admin", return_value=True):
        await list_commands_command(update, context)

    text = update.effective_message.reply_html.call_args[0][0]
    assert "Admin only" in text
    assert "/approve" in text


@pytest.mark.asyncio
async def test_help_not_approved_shows_request_subtitle_and_keyboard():
    update = _make_update(21)
    context = _make_context()
    phase = {"is_approved": False, "has_profile": False, "is_admin": False}
    sentinel_keyboard = object()

    with (
        patch(
            "notify_bot.handlers.common.menu.get_user_phase",
            new=AsyncMock(return_value=phase),
        ),
        patch(
            "notify_bot.handlers.common.menu.build_help_keyboard",
            return_value=sentinel_keyboard,
        ) as mock_build,
    ):
        await help_command(update, context)

    mock_build.assert_called_once_with(phase)
    update.effective_message.reply_html.assert_awaited_once()
    text, kwargs = (
        update.effective_message.reply_html.call_args[0][0],
        update.effective_message.reply_html.call_args.kwargs,
    )
    assert "tap below to request access" in text
    assert kwargs["reply_markup"] is sentinel_keyboard


@pytest.mark.asyncio
async def test_help_approved_not_enrolled_shows_enroll_subtitle():
    update = _make_update(22)
    context = _make_context()
    phase = {"is_approved": True, "has_profile": False, "is_admin": False}

    with (
        patch(
            "notify_bot.handlers.common.menu.get_user_phase",
            new=AsyncMock(return_value=phase),
        ),
        patch("notify_bot.handlers.common.menu.build_help_keyboard", return_value=object()),
    ):
        await help_command(update, context)

    text = update.effective_message.reply_html.call_args[0][0]
    assert "enroll your data" in text


@pytest.mark.asyncio
async def test_help_regular_enrolled_user_gets_no_admin_note():
    update = _make_update(23)
    context = _make_context()
    phase = {"is_approved": True, "has_profile": True, "is_admin": False}

    with (
        patch(
            "notify_bot.handlers.common.menu.get_user_phase",
            new=AsyncMock(return_value=phase),
        ),
        patch("notify_bot.handlers.common.menu.build_help_keyboard", return_value=object()),
    ):
        await help_command(update, context)

    text = update.effective_message.reply_html.call_args[0][0]
    assert "Tap a button below to run a check." in text
    assert "text-only" not in text


@pytest.mark.asyncio
async def test_help_admin_gets_status_subtitle_plus_admin_note():
    update = _make_update(24)
    context = _make_context()
    phase = {"is_approved": True, "has_profile": True, "is_admin": True}

    with (
        patch(
            "notify_bot.handlers.common.menu.get_user_phase",
            new=AsyncMock(return_value=phase),
        ),
        patch("notify_bot.handlers.common.menu.build_help_keyboard", return_value=object()),
    ):
        await help_command(update, context)

    text = update.effective_message.reply_html.call_args[0][0]
    assert "Tap a button below to run a check." in text
    assert "Admin tools" in text
    assert "/approve" in text  # the ID-argument commands note


@pytest.mark.asyncio
async def test_help_admin_not_approved_still_gets_admin_note():
    """Admin-ness is a role, independent of the admin's own DB approval
    status — the admin note (and, separately, the admin keyboard block)
    must still show even when the admin hasn't been /approve'd themselves."""
    update = _make_update(25)
    context = _make_context()
    phase = {"is_approved": False, "has_profile": False, "is_admin": True}

    with (
        patch(
            "notify_bot.handlers.common.menu.get_user_phase",
            new=AsyncMock(return_value=phase),
        ),
        patch("notify_bot.handlers.common.menu.build_help_keyboard", return_value=object()),
    ):
        await help_command(update, context)

    text = update.effective_message.reply_html.call_args[0][0]
    assert "tap below to request access" in text
    assert "Admin tools" in text
