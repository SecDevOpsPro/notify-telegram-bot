"""Tests for the phase-aware /help menu (notify_bot/handlers/menu.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InaccessibleMessage, InlineKeyboardButton, Message

from notify_bot.handlers import menu

# ── Helpers ───────────────────────────────────────────────────────────────────


def _labels(markup) -> list[str]:
    """Flatten an InlineKeyboardMarkup into its button labels, row by row."""
    return [button.text for row in markup.inline_keyboard for button in row]


def _callback_data(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _make_callback_update(user_id: int, data: str) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock(spec=Message)
    return update


# ── get_user_phase ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_unknown_user_is_not_approved():
    with patch("notify_bot.handlers.menu.db.get_user", new=AsyncMock(return_value=None)):
        phase = await menu.get_user_phase(1)

    assert phase["is_approved"] is False
    assert phase["has_profile"] is False


@pytest.mark.asyncio
async def test_phase_pending_user_is_not_approved():
    with patch(
        "notify_bot.handlers.menu.db.get_user",
        new=AsyncMock(return_value={"status": "pending"}),
    ):
        phase = await menu.get_user_phase(2)

    assert phase["is_approved"] is False


@pytest.mark.asyncio
async def test_phase_approved_without_profile_fields_is_not_enrolled():
    empty_profile = {
        "national_id": None,
        "driving_licence": None,
        "vehicle_plate": None,
        "talon_no": None,
    }
    with (
        patch(
            "notify_bot.handlers.menu.db.get_user",
            new=AsyncMock(return_value={"status": "approved"}),
        ),
        patch(
            "notify_bot.handlers.menu.db.get_profile",
            new=AsyncMock(return_value=empty_profile),
        ),
    ):
        phase = await menu.get_user_phase(3)

    assert phase["is_approved"] is True
    assert phase["has_profile"] is False


@pytest.mark.asyncio
async def test_phase_approved_with_a_profile_field_is_enrolled():
    profile = {
        "national_id": "1234567890",
        "driving_licence": None,
        "vehicle_plate": None,
        "talon_no": None,
    }
    with (
        patch(
            "notify_bot.handlers.menu.db.get_user",
            new=AsyncMock(return_value={"status": "approved"}),
        ),
        patch("notify_bot.handlers.menu.db.get_profile", new=AsyncMock(return_value=profile)),
    ):
        phase = await menu.get_user_phase(4)

    assert phase["has_profile"] is True


@pytest.mark.asyncio
async def test_phase_reports_admin_status():
    with (
        patch(
            "notify_bot.handlers.menu.db.get_user",
            new=AsyncMock(return_value={"status": "approved"}),
        ),
        patch("notify_bot.handlers.menu.db.get_profile", new=AsyncMock(return_value=None)),
        patch("notify_bot.handlers.menu.config.is_admin", return_value=True),
    ):
        phase = await menu.get_user_phase(5)

    assert phase["is_admin"] is True


# ── build_help_keyboard ──────────────────────────────────────────────────────


def test_keyboard_not_approved_only_shows_request_and_change():
    phase = {"is_approved": False, "has_profile": False, "is_admin": False}
    markup = menu.build_help_keyboard(phase)

    data = _callback_data(markup)
    assert data == ["cmd:request", "cmd:change", "cmd:list_commands"]


def test_keyboard_approved_not_enrolled_shows_enroll_and_change():
    phase = {"is_approved": True, "has_profile": False, "is_admin": False}
    markup = menu.build_help_keyboard(phase)

    data = _callback_data(markup)
    assert "cmd:enroll" in data
    assert "cmd:change" in data
    assert "cmd:driver" not in data


def test_keyboard_regular_enrolled_user_gets_checks_but_not_admin_block():
    phase = {"is_approved": True, "has_profile": True, "is_admin": False}
    markup = menu.build_help_keyboard(phase)

    data = _callback_data(markup)
    checks = ("driver", "plate", "vignette", "sticker", "clamp", "gtp", "mtpl", "fines", "vehicle")
    for cmd in checks:
        assert f"cmd:{cmd}" in data
    assert "cmd:unenroll" in data
    for cmd in ("pending", "users", "myip", "brief"):
        assert f"cmd:{cmd}" not in data


def test_keyboard_admin_gets_checks_plus_separate_admin_block():
    phase = {"is_approved": True, "has_profile": True, "is_admin": True}
    markup = menu.build_help_keyboard(phase)

    data = _callback_data(markup)
    for cmd in ("driver", "plate", "vignette", "unenroll", "pending", "users", "myip", "brief"):
        assert f"cmd:{cmd}" in data
    # Section headers are present as their own (non-command) buttons.
    labels = _labels(markup)
    assert any("Admin" in label for label in labels)
    assert any("Vehicle checks" in label for label in labels)


def test_keyboard_admin_not_approved_still_gets_admin_block():
    """Admin-ness is a role independent of the admin's own DB approval status
    — admin commands are gated on config.is_admin() alone (see admin.py's
    `admin` decorator), not on the caller having been /approve'd."""
    phase = {"is_approved": False, "has_profile": False, "is_admin": True}
    markup = menu.build_help_keyboard(phase)

    data = _callback_data(markup)
    assert "cmd:request" in data
    for cmd in ("pending", "users", "myip", "brief"):
        assert f"cmd:{cmd}" in data
    assert "cmd:driver" not in data


def test_keyboard_admin_approved_but_not_enrolled_still_gets_admin_block():
    """Same decoupling, but for an admin who hasn't run /enroll yet."""
    phase = {"is_approved": True, "has_profile": False, "is_admin": True}
    markup = menu.build_help_keyboard(phase)

    data = _callback_data(markup)
    assert "cmd:enroll" in data
    for cmd in ("pending", "users", "myip", "brief"):
        assert f"cmd:{cmd}" in data
    assert "cmd:driver" not in data


def test_all_keyboard_buttons_are_inline_keyboard_buttons():
    phase = {"is_approved": True, "has_profile": True, "is_admin": True}
    markup = menu.build_help_keyboard(phase)

    for row in markup.inline_keyboard:
        for button in row:
            assert isinstance(button, InlineKeyboardButton)


# ── menu_callback dispatch ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_menu_callback_answers_and_dispatches_to_matching_handler():
    fake_handler = AsyncMock()
    update = _make_callback_update(10, "cmd:driver")
    context = MagicMock()

    with patch(
        "notify_bot.handlers.menu._dispatch_table",
        return_value={"driver": fake_handler},
    ):
        await menu.menu_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    fake_handler.assert_awaited_once()
    called_update = fake_handler.call_args[0][0]
    assert called_update.message is update.callback_query.message
    assert called_update.effective_message is update.callback_query.message
    assert called_update.effective_user is update.effective_user
    assert context.args == []


@pytest.mark.asyncio
async def test_menu_callback_skips_dispatch_when_message_is_inaccessible():
    """Telegram reports an old message as inaccessible — it has no reply_* methods,
    so the handler must not be run against it (it would crash on reply_html)."""
    fake_handler = AsyncMock()
    update = _make_callback_update(10, "cmd:driver")
    update.callback_query.message = MagicMock(spec=InaccessibleMessage)

    with patch(
        "notify_bot.handlers.menu._dispatch_table",
        return_value={"driver": fake_handler},
    ):
        await menu.menu_callback(update, MagicMock())

    update.callback_query.answer.assert_awaited_once()
    assert "expired" in update.callback_query.answer.call_args.args[0]
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True
    fake_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_callback_ignores_unknown_action():
    update = _make_callback_update(11, "cmd:doesnotexist")
    context = MagicMock()

    with patch("notify_bot.handlers.menu._dispatch_table", return_value={}):
        await menu.menu_callback(update, context)

    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_noop_callback_only_answers():
    update = _make_callback_update(12, "noop")

    await menu.noop_callback(update, MagicMock())

    update.callback_query.answer.assert_awaited_once()
