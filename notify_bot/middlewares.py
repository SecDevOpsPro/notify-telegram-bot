"""Auth middleware — decorator that gates handlers to approved users only."""

from __future__ import annotations

import functools
from typing import Any, Callable

from telegram import Update
from telegram.ext import ContextTypes

from notify_bot import db


def require_approved(handler: Callable) -> Callable:
    """
    Decorator for PTB async command/message handlers.

    Allows the handler to execute only when the calling Telegram user has
    ``status='approved'`` in the database.  Otherwise a friendly message is
    sent instructing them to use /request.

    Usage::

        @require_approved
        async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            ...
    """

    @functools.wraps(handler)
    async def wrapper(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        user = update.effective_user
        if not user:
            return

        record = await db.get_user(user.id)
        if not record or record["status"] != "approved":
            # A button tap on a stale keyboard (e.g. "📝 Enroll your data" sent on
            # approval, tapped after access was revoked) would otherwise be left
            # unanswered, hanging on its loading spinner. getattr: menu buttons pass
            # a _ButtonUpdate stand-in that has no callback_query (already answered).
            if query := getattr(update, "callback_query", None):
                await query.answer()
            await update.effective_message.reply_text(
                "⛔ You don't have access to this command.\n"
                "Use /request to ask the admin for access."
            )
            return

        return await handler(update, context, *args, **kwargs)

    return wrapper
