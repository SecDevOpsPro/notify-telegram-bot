"""Tests for notify_bot/config.py — admin and in-memory debug-user checks."""

from __future__ import annotations

import notify_bot.config as config


def test_is_admin_true_only_for_configured_id(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 42)
    assert config.is_admin(42) is True
    assert config.is_admin(1) is False


def test_is_admin_false_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 0)
    assert config.is_admin(0) is False


def test_add_and_remove_debug_user(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 0)
    monkeypatch.setattr(config, "DEBUG_USER_IDS", set())

    assert config.is_debug_user(7) is False

    config.add_debug_user(7)
    assert config.is_debug_user(7) is True

    removed = config.remove_debug_user(7)
    assert removed is True
    assert config.is_debug_user(7) is False


def test_remove_debug_user_not_present_returns_false(monkeypatch):
    monkeypatch.setattr(config, "DEBUG_USER_IDS", set())
    assert config.remove_debug_user(999) is False


def test_admin_is_always_a_debug_user(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 42)
    monkeypatch.setattr(config, "DEBUG_USER_IDS", set())
    assert config.is_debug_user(42) is True
