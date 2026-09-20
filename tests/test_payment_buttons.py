"""Tests for the fine-payment keyboards (notify_bot/payment_buttons.py)."""

from __future__ import annotations

from datetime import date, timedelta

from telegram import InlineKeyboardMarkup

from notify_bot.payment_buttons import build_copy_keyboard, build_fines_keyboard
from notify_bot.services.mvr import Obligation, payment_details

_FINE = {
    "amount": 51.13,
    "discountAmount": 35.79,
    "bankName": "БНБ",
    "bic": "BNBGBGSF",
    "iban": "BG64BNBG96613100147701",
    "paymentReason": "ЕЛ.ФИШ СЕРИЯ K 13247956 24.08.2026",
    "currency": "EUR",
    "additionalData": {"documentSeries": "K", "documentNumber": "13247956"},
}


def _expiring(days_from_today: int) -> str:
    return f"{date.today() + timedelta(days=days_from_today):%Y-%m-%d}T23:59:59"


def _copy_keyboard(ob: dict) -> InlineKeyboardMarkup | None:
    return build_copy_keyboard(payment_details(ob))


def _units(*obligations) -> list[Obligation]:
    return [Obligation(unit_group=1, unit_group_label="x", obligations=list(obligations))]


def _labels(keyboard: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def _copied(keyboard: InlineKeyboardMarkup) -> dict[str, str]:
    """Button label -> the text it copies."""
    return {
        button.text: button.copy_text.text
        for row in keyboard.inline_keyboard
        for button in row
        if button.copy_text
    }


# ── payment_details ───────────────────────────────────────────────────────────


def test_payment_details_extracts_transfer_values():
    details = payment_details(_FINE)

    assert details is not None
    assert details.iban == "BG64BNBG96613100147701"
    assert details.bic == "BNBGBGSF"
    assert details.reason == "ЕЛ.ФИШ СЕРИЯ K 13247956 24.08.2026"
    assert details.reference == "K 13247956"
    assert (details.amount, details.discount) == (51.13, 35.79)


def test_payment_details_drops_discount_equal_to_amount():
    details = payment_details({**_FINE, "discountAmount": 51.13})
    assert details is not None
    assert details.discount is None


def test_payment_details_keeps_discount_through_its_last_day():
    details = payment_details({**_FINE, "expirationDate": _expiring(0)})
    assert details is not None
    assert details.discount == 35.79


def test_payment_details_drops_expired_discount():
    details = payment_details({**_FINE, "expirationDate": _expiring(-1)})
    assert details is not None
    assert details.discount is None
    assert details.amount == 51.13


def test_payment_details_none_without_iban_or_for_plain_strings():
    assert payment_details({"amount": 10.0, "currency": "EUR"}) is None
    assert payment_details("Speeding fine") is None


# ── build_copy_keyboard ───────────────────────────────────────────────────────


def test_copy_keyboard_offers_every_transfer_value():
    keyboard = _copy_keyboard(_FINE)

    assert keyboard is not None
    assert _copied(keyboard) == {
        "📋 IBAN": "BG64BNBG96613100147701",
        "📋 BIC": "BNBGBGSF",
        "📋 Reason": "ЕЛ.ФИШ СЕРИЯ K 13247956 24.08.2026",
        "📋 Amount 51.13": "51.13",
        "📋 Discounted 35.79": "35.79",
    }


def test_copy_keyboard_puts_reason_last_two_per_row():
    keyboard = _copy_keyboard(_FINE)

    assert keyboard is not None
    assert [[b.text for b in row] for row in keyboard.inline_keyboard] == [
        ["📋 IBAN", "📋 BIC"],
        ["📋 Amount 51.13", "📋 Discounted 35.79"],
        ["📋 Reason"],
    ]


def test_copy_keyboard_hides_discounted_button_once_the_discount_expired():
    keyboard = _copy_keyboard({**_FINE, "expirationDate": _expiring(-1)})

    assert keyboard is not None
    assert not any(label.startswith("📋 Discounted") for label in _labels(keyboard))
    assert "📋 Amount 51.13" in _labels(keyboard)  # the full amount is still payable


def test_copy_keyboard_shows_discounted_button_while_the_discount_applies():
    keyboard = _copy_keyboard({**_FINE, "expirationDate": _expiring(5)})

    assert keyboard is not None
    assert "📋 Discounted 35.79" in _labels(keyboard)


def test_copy_keyboard_skips_values_the_api_did_not_send():
    keyboard = _copy_keyboard({"iban": "BG64BNBG96613100147701", "amount": 5})

    assert keyboard is not None
    assert _copied(keyboard) == {
        "📋 IBAN": "BG64BNBG96613100147701",
        "📋 Amount 5.00": "5.00",
    }


def test_copy_keyboard_omits_values_over_the_copy_limit():
    keyboard = _copy_keyboard({**_FINE, "paymentReason": "x" * 257})

    assert keyboard is not None
    assert "📋 Reason" not in _labels(keyboard)
    assert "📋 IBAN" in _labels(keyboard)


def test_copy_keyboard_is_none_without_payment_details():
    assert build_copy_keyboard(None) is None


# ── build_fines_keyboard (daily report shortcuts) ─────────────────────────────


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str | None]:
    return [b.callback_data for row in keyboard.inline_keyboard for b in row]


def test_fines_keyboard_offers_driver_and_plate_when_both_found_fines():
    keyboard = build_fines_keyboard(licence=_units(_FINE), plate=_units(_FINE))

    assert keyboard is not None
    assert _callbacks(keyboard) == ["cmd:driver", "cmd:plate"]


def test_fines_keyboard_only_offers_the_lookup_that_found_fines():
    only_licence = build_fines_keyboard(licence=_units(_FINE), plate=_units())
    only_plate = build_fines_keyboard(licence=[], plate=_units(_FINE))

    assert only_licence is not None and only_plate is not None
    assert _callbacks(only_licence) == ["cmd:driver"]
    assert _callbacks(only_plate) == ["cmd:plate"]


def test_fines_keyboard_is_none_when_no_fines():
    assert build_fines_keyboard(licence=_units(), plate=[]) is None
    assert build_fines_keyboard(licence=[], plate=[]) is None
