"""Tests for notify_bot/errors.py — verbose-for-admin, terse-for-everyone-else."""

from __future__ import annotations

from unittest.mock import patch

from notify_bot.errors import format_error


def test_regular_user_only_sees_base_message():
    with patch("notify_bot.errors.config.is_debug_user", return_value=False):
        text = format_error(1, "⚠️ Something went wrong.", ValueError("secret detail"))

    assert text == "⚠️ Something went wrong."
    assert "secret detail" not in text


def test_admin_sees_exception_type_and_message():
    with patch("notify_bot.errors.config.is_debug_user", return_value=True):
        text = format_error(1, "⚠️ Something went wrong.", ValueError("secret detail"))

    assert text.startswith("⚠️ Something went wrong.")
    assert "ValueError" in text
    assert "secret detail" in text


def test_exception_detail_is_html_escaped_for_admin():
    with patch("notify_bot.errors.config.is_debug_user", return_value=True):
        text = format_error(1, "⚠️ Something went wrong.", ValueError("<script>steal()</script>"))

    assert "<script>" not in text
    assert "&lt;script&gt;" in text
