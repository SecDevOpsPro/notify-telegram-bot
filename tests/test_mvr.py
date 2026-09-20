"""Tests for the MVR obligations service (notify_bot/services/mvr.py)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notify_bot.services.mvr import (
    MVRApiError,
    Obligation,
    _format_obligation,
    _parse,
    check_by_licence,
    check_by_plate,
    render_fine_messages,
    render_obligations,
)

# ── _parse (pure function) ────────────────────────────────────────────────────


def test_parse_with_obligations():
    data = {
        "obligationsData": [
            {"unitGroup": 1, "obligations": ["Fine A", "Fine B"]},
            {"unitGroup": 2, "obligations": []},
        ]
    }
    result = _parse(data)

    assert len(result) == 2
    assert result[0].unit_group == 1
    assert result[0].unit_group_label == "Road Traffic Act and/or Insurance Code"
    assert result[0].has_obligations is True
    assert result[0].obligations == ["Fine A", "Fine B"]

    assert result[1].unit_group == 2
    assert result[1].unit_group_label == "Law for Bulgarian Personal Documents"
    assert result[1].has_obligations is False


def test_parse_empty_obligationsdata():
    assert _parse({"obligationsData": []}) == []


def test_parse_missing_key():
    assert _parse({}) == []


def test_parse_unknown_unit_group():
    data = {"obligationsData": [{"unitGroup": 99, "obligations": []}]}
    result = _parse(data)
    assert result[0].unit_group_label == "Obligation group 99"


# ── Obligation dataclass ──────────────────────────────────────────────────────


def test_obligation_has_obligations_true():
    ob = Obligation(unit_group=1, unit_group_label="Test", obligations=["x"])
    assert ob.has_obligations is True


def test_obligation_has_obligations_false():
    ob = Obligation(unit_group=1, unit_group_label="Test", obligations=[])
    assert ob.has_obligations is False


# ── check_by_licence (mocked network) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_by_licence_success():
    mock_data = {
        "obligationsData": [
            {"unitGroup": 1, "obligations": []},
        ]
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_data
    mock_resp.raise_for_status = MagicMock()

    with patch("notify_bot.services.mvr.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(get=AsyncMock(return_value=mock_resp))
        )
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await check_by_licence(national_id="1234567890", licence_number="123456")

    assert len(result) == 1
    assert result[0].has_obligations is False


@pytest.mark.asyncio
async def test_check_by_licence_http_error():
    with patch("notify_bot.services.mvr.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(get=AsyncMock(side_effect=httpx.HTTPError("connection failed")))
        )
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(MVRApiError, match="connection error"):
            await check_by_licence(national_id="1234567890", licence_number="123456")


# ── check_by_plate (mocked network) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_by_plate_success():
    mock_data = {
        "obligationsData": [
            {"unitGroup": 1, "obligations": ["Speeding fine"]},
        ]
    }

    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_data
    mock_resp.raise_for_status = MagicMock()

    with patch("notify_bot.services.mvr.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(get=AsyncMock(return_value=mock_resp))
        )
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await check_by_plate(national_id="1234567890", plate_number="CB1234AB")

    assert result[0].has_obligations is True
    assert "Speeding fine" in result[0].obligations


# ── _format_obligation / render_obligations ──────────────────────────────────


def _due(days_from_today: int) -> tuple[str, str]:
    """An ``expirationDate`` the API would send, plus the ``dd.mm.yyyy`` we display for it."""
    due = date.today() + timedelta(days=days_from_today)
    return f"{due:%Y-%m-%d}T23:59:59", f"{due:%d.%m.%Y}"


def test_format_obligation_plain_string_passthrough():
    assert _format_obligation("Speeding fine") == "Speeding fine"


def test_format_obligation_full_payment_dict():
    ob = {
        "amount": 51.13,
        "discountAmount": 35.79,
        "bankName": "БНБ",
        "bic": "BNBGBGSF",
        "iban": "BG64BNBG96613100147701",
        "paymentReason": "ЕЛ.ФИШ СЕРИЯ K 13247956 24.08.2026",
        "expirationDate": _due(10)[0],
        "currency": "EUR",
        "additionalData": {
            "documentType": "TICKET",
            "documentSeries": "K",
            "documentNumber": "13247956",
            "breachOfOrder": "чл. 21, ал. 2, от ЗДвП",
            "vehicleNumber": "XH2856",
            "breachDate": "2026-08-22",
        },
    }
    result = _format_obligation(ob)

    assert "💰 Amount: 51.13 EUR" in result
    assert f"💸 With discount: 35.79 EUR (if paid by {_due(10)[1]})" in result
    assert "📄 Ticket: K 13247956" in result
    assert "🚗 Vehicle: XH2856" in result
    assert "⚖️ Violation: Art. 21, para. 2, of the Road Traffic Act (22.08.2026)" in result
    assert "<b>🏦 Pay by bank transfer:</b>" in result
    assert "Bank:   БНБ" in result
    assert "IBAN:   <code>BG64BNBG96613100147701</code>" in result
    assert "BIC:    BNBGBGSF" in result
    assert "Reason: ЕЛ.ФИШ СЕРИЯ K 13247956 24.08.2026" in result


def test_format_obligation_expired_discount_is_struck_through_and_marked_expired():
    expiration, shown = _due(-1)
    ob = {"amount": 51.13, "discountAmount": 35.79, "currency": "EUR", "expirationDate": expiration}

    result = _format_obligation(ob)

    assert f"💸 <s>With discount: 35.79 EUR</s> (expired {shown})" in result
    assert "if paid by" not in result


def test_format_obligation_discount_still_valid_on_its_last_day():
    expiration, shown = _due(0)
    ob = {"amount": 51.13, "discountAmount": 35.79, "currency": "EUR", "expirationDate": expiration}

    result = _format_obligation(ob)

    assert f"💸 With discount: 35.79 EUR (if paid by {shown})" in result
    assert "expired" not in result


def test_format_obligation_discount_without_expiration_date_is_not_marked_expired():
    ob = {"amount": 51.13, "discountAmount": 35.79, "currency": "EUR"}
    assert "💸 With discount: 35.79 EUR" in _format_obligation(ob)
    assert "expired" not in _format_obligation(ob)


def test_format_obligation_document_reference_unknown_type_falls_back_to_generic_label():
    ob = {
        "amount": 30.0,
        "currency": "EUR",
        "additionalData": {"documentType": "AKT", "documentSeries": "A", "documentNumber": "1"},
    }
    assert "📄 Document: A 1" in _format_obligation(ob)


def test_format_obligation_document_reference_number_only():
    ob = {
        "amount": 30.0,
        "currency": "EUR",
        "additionalData": {"documentNumber": "13247956"},
    }
    assert "📄 Document: 13247956" in _format_obligation(ob)


def test_format_obligation_minimal_dict_no_discount():
    ob = {"amount": 100.0, "currency": "BGN"}
    assert _format_obligation(ob) == "💰 Amount: 100.00 BGN"


def test_format_obligation_unknown_dict_shape_falls_back_to_repr():
    ob = {"foo": "bar"}
    assert _format_obligation(ob) == str(ob)


def test_format_obligation_breach_without_road_traffic_act_suffix():
    ob = {
        "amount": 30.0,
        "currency": "EUR",
        "additionalData": {"breachOfOrder": "чл. 5, ал. 3", "vehicleNumber": "XH2856"},
    }
    result = _format_obligation(ob)
    assert "🚗 Vehicle: XH2856" in result
    assert "⚖️ Violation: Art. 5, para. 3" in result


def test_render_obligations_formats_payment_dict():
    units = [
        Obligation(
            unit_group=1,
            unit_group_label="Road Traffic Act and/or Insurance Code",
            obligations=[{"amount": 51.13, "discountAmount": 35.79, "currency": "EUR"}],
        )
    ]
    rendered = render_obligations(units)

    assert "💰 Amount: 51.13 EUR" in rendered
    assert (
        "💸 With discount: 35.79 EUR" in rendered
    )  # no "(if paid by ...)" — no expirationDate given
    assert "{'amount'" not in rendered  # no raw dict repr leaking through


def test_render_obligations_blank_line_between_multiple_entries():
    units = [
        Obligation(
            unit_group=1,
            unit_group_label="Road Traffic Act and/or Insurance Code",
            obligations=[{"amount": 10.0, "currency": "EUR"}, {"amount": 20.0, "currency": "EUR"}],
        )
    ]
    rendered = render_obligations(units)

    assert "💰 Amount: 10.00 EUR\n\n  • 💰 Amount: 20.00 EUR" in rendered


# ── render_fine_messages ─────────────────────────────────────────────────────

_ROAD_LAW = "Road Traffic Act and/or Insurance Code"
_DOCS_LAW = "Law for Bulgarian Personal Documents"


def _fine(number: str, amount: float = 51.13) -> dict:
    return {
        "amount": amount,
        "iban": "BG64BNBG96613100147701",
        "bic": "BNBGBGSF",
        "paymentReason": f"ЕЛ.ФИШ СЕРИЯ K {number}",
        "currency": "EUR",
        "additionalData": {"documentSeries": "K", "documentNumber": number},
    }


def _group(*obligations, label: str = _ROAD_LAW) -> Obligation:
    return Obligation(unit_group=1, unit_group_label=label, obligations=list(obligations))


def test_fine_messages_is_one_message_when_nothing_is_payable():
    messages = render_fine_messages([_group("Speeding fine")])

    assert len(messages) == 1
    assert messages[0].payment is None
    assert messages[0].text.startswith("<b>🔎 Obligations check</b>")
    assert "Speeding fine" in messages[0].text


def test_fine_messages_is_one_message_for_a_single_fine():
    units = [_group(_fine("13247956"))]

    messages = render_fine_messages(units)

    assert len(messages) == 1
    assert messages[0].text == "<b>🔎 Obligations check</b>\n" + render_obligations(units)
    assert messages[0].payment is not None
    assert messages[0].payment.reference == "K 13247956"


def test_fine_messages_gives_every_fine_its_own_message():
    messages = render_fine_messages([_group(_fine("13247956"), _fine("13247999", 25.0))])

    assert len(messages) == 3  # summary + one per fine
    assert messages[0].payment is None
    first, second = messages[1], messages[2]
    assert "<b>Fine 1 of 2 · K 13247956</b>" in first.text
    assert "<b>Fine 2 of 2 · K 13247999</b>" in second.text
    assert first.payment is not None and first.payment.reference == "K 13247956"
    assert second.payment is not None and second.payment.reference == "K 13247999"
    # Each message carries only its own fine's details.
    assert "ЕЛ.ФИШ СЕРИЯ K 13247956" in first.text
    assert "K 13247999" not in first.text
    assert "💰 Amount: 25.00 EUR" in second.text


def test_fine_message_is_not_indented_like_a_bullet_entry():
    messages = render_fine_messages([_group(_fine("1"), _fine("2"))])

    assert "\n    " not in messages[1].text
    assert "  • " not in messages[1].text


def test_fine_summary_counts_fines_without_repeating_bank_details():
    summary = render_fine_messages([_group(_fine("1"), _fine("2"))])[0].text

    assert _ROAD_LAW in summary
    assert "💳 2 unpaid fines — sent below" in summary
    assert "IBAN" not in summary


def test_fine_summary_keeps_entries_that_are_not_payable_by_bank():
    messages = render_fine_messages([_group(_fine("1"), _fine("2"), "Missing insurance")])

    assert len(messages) == 3
    assert "Missing insurance" in messages[0].text


def test_fine_numbering_runs_across_law_groups():
    messages = render_fine_messages(
        [_group(_fine("1")), _group(_fine("2"), label=_DOCS_LAW)],
    )

    assert "Fine 1 of 2" in messages[1].text
    assert "Fine 2 of 2" in messages[2].text
    assert messages[0].text.count("💳 1 unpaid fine — sent below") == 2


def test_fine_title_omits_the_number_when_the_api_sent_none():
    bare = {"amount": 10.0, "iban": "BG11AAAA11111111111111", "currency": "EUR"}

    messages = render_fine_messages([_group(bare, dict(bare))])

    assert "<b>Fine 1 of 2</b>" in messages[1].text


@pytest.mark.asyncio
async def test_check_by_plate_http_status_error():
    response = MagicMock()
    response.status_code = 503

    with patch("notify_bot.services.mvr.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(
                get=AsyncMock(
                    side_effect=httpx.HTTPStatusError(
                        "Service Unavailable",
                        request=MagicMock(),
                        response=response,
                    )
                )
            )
        )
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(MVRApiError, match="HTTP 503"):
            await check_by_plate(national_id="1234567890", plate_number="CB1234AB")
