"""Auth middleware — decorator that gates handlers to approved users only."""

from __future__ import annotations

import functools

from telegram import CallbackQuery, Update
from telegram.ext import ContextTypes

from notify_bot import db
from notify_bot.updates import HandlerCallback


def require_approved[T](handler: HandlerCallback[T]) -> HandlerCallback[T | None]:
    """
    Decorator for PTB async command/message handlers.

    Allows the handler to execute only when the calling Telegram user has
    ``status='approved'`` in the database.  Otherwise, a friendly message is
    sent instructing them to use /request.  A blocked call returns ``None``
    without running *handler*, so the wrapped handler's return type widens to
    ``T | None``.

    Usage::

        @require_approved
        async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            ...
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> T | None:
        user = update.effective_user
        message = update.effective_message
        # A button tap on a stale keyboard (e.g. "📝 Enroll your data" sent on
        # approval, tapped after access was revoked, or old enough that Telegram
        # reports its message as inaccessible) must always be answered, otherwise
        # it hangs on its loading spinner. getattr: menu buttons pass a
        # _ButtonUpdate stand-in that has no callback_query (already answered).
        query: CallbackQuery | None = getattr(update, "callback_query", None)
        if not user or not message:
            if query is not None:
                await query.answer()
            return None

        record = await db.get_user(user.id)
        if not record or record["status"] != "approved":
            if query is not None:
                await query.answer()
            await message.reply_text(
                "⛔ You don't have access to this command.\n"
                "Use /request to ask the admin for access."
            )
            return None

        return await handler(update, context)

    return wrapper
