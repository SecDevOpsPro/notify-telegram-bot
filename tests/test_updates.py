"""Tests for the ``require`` narrowing helper (notify_bot/updates.py)."""

from __future__ import annotations

import pytest

from notify_bot.updates import MissingUpdateFieldError, require


def test_require_returns_value_unchanged():
    value = object()
    assert require(value, "thing") is value


def test_require_keeps_falsy_values():
    """Only None counts as missing — 0, "" and {} are legitimate values."""
    assert require(0, "count") == 0
    assert require("", "text") == ""
    assert require({}, "user_data") == {}


def test_require_raises_naming_the_missing_field():
    with pytest.raises(MissingUpdateFieldError, match="update has no message"):
        require(None, "message")
