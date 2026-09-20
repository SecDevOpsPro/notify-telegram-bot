"""Typing helpers for python-telegram-bot handlers.

PTB types nearly every ``Update`` field as optional (``message``,
``effective_user``, ``callback_query``, …) because the same class models every
kind of update Telegram can send.  A handler registered for a specific update
type knows those fields are present, so :func:`require` narrows them in one
call and fails with a clear error — instead of an opaque ``AttributeError`` on
``None`` — if that assumption is ever wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

#: Shape of a PTB handler callback: ``async def handler(update, context) -> T``.
type HandlerCallback[T] = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, T]]


class MissingUpdateFieldError(Exception):
    """An ``Update`` (or context) lacked a field the handler relies on."""


def require[T](value: T | None, name: str) -> T:
    """Return *value*, or raise :class:`MissingUpdateFieldError` if it is ``None``.

    Usage::

        message = require(update.message, "message")
        await message.reply_text("hi")
    """
    if value is None:
        raise MissingUpdateFieldError(f"update has no {name}")
    return value
