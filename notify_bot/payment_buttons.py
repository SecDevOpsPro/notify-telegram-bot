"""Inline keyboards for paying fines: tap-to-copy bank details, and daily-report shortcuts."""

from __future__ import annotations

from collections.abc import Iterable

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from notify_bot.services.mvr import Obligation, PaymentDetails

# Bot API limit: CopyTextButton.text is 1-256 characters.
_MAX_COPY_LENGTH = 256
_BUTTONS_PER_ROW = 2


def _copy_button(label: str, value: str | None) -> InlineKeyboardButton | None:
    if not value or len(value) > _MAX_COPY_LENGTH:
        return None
    return InlineKeyboardButton(f"📋 {label}", copy_text=CopyTextButton(value))


def build_copy_keyboard(details: PaymentDetails | None) -> InlineKeyboardMarkup | None:
    """Tap-to-copy buttons (IBAN, BIC, amounts, reason) for one fine.

    Returns ``None`` when there is nothing to copy, so the result can be passed
    straight to ``reply_markup``. Reason comes last: it is the longest label.
    """
    if details is None:
        return None
    # Amounts are copied as a plain "51.13" — what a bank's amount field accepts.
    amount = f"{details.amount:.2f}" if details.amount is not None else None
    discount = f"{details.discount:.2f}" if details.discount is not None else None
    candidates = [
        _copy_button("IBAN", details.iban),
        _copy_button("BIC", details.bic),
        _copy_button(f"Amount {amount}", amount),
        _copy_button(f"Discounted {discount}", discount),
        _copy_button("Reason", details.reason),
    ]
    buttons = [button for button in candidates if button]
    rows = [buttons[i : i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(rows) if rows else None


def build_fines_keyboard(
    *, licence: Iterable[Obligation], plate: Iterable[Obligation]
) -> InlineKeyboardMarkup | None:
    """Daily-report buttons that run /driver and /plate, each shown only if its lookup found fines.

    The report itself stays compact; a tap sends the per-fine messages with their
    copy buttons. The ``cmd:`` callbacks are the /help menu's (``menu.menu_callback``).
    """
    buttons = []
    if any(unit.has_obligations for unit in licence):
        buttons.append(InlineKeyboardButton("🚗 Driver fines", callback_data="cmd:driver"))
    if any(unit.has_obligations for unit in plate):
        buttons.append(InlineKeyboardButton("🚙 Plate fines", callback_data="cmd:plate"))
    return InlineKeyboardMarkup([buttons]) if buttons else None
