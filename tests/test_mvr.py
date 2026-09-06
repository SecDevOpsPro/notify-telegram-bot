"""Tests for the MVR obligations service (notify_bot/services/mvr.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notify_bot.services.mvr import (
    MVRApiError,
    Obligation,
    _format_obligation,
    _parse,
    _translate_breach,
    check_by_licence,
    check_by_plate,
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

    with patch("notify_bot.services.mvr.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(get=AsyncMock(return_value=mock_resp))
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await check_by_licence("1234567890", "123456")

    assert len(result) == 1
    assert result[0].has_obligations is False


@pytest.mark.asyncio
async def test_check_by_licence_http_error():
    with patch("notify_bot.services.mvr.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(get=AsyncMock(side_effect=httpx.HTTPError("connection failed")))
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(MVRApiError, match="connection error"):
            await check_by_licence("1234567890", "123456")


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

    with patch("notify_bot.services.mvr.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(get=AsyncMock(return_value=mock_resp))
        )
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await check_by_plate("1234567890", "CB1234AB")

    assert result[0].has_obligations is True
    assert "Speeding fine" in result[0].obligations


# ── _format_obligation / render_obligations ──────────────────────────────────


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
        "expirationDate": "2026-08-25T23:59:59",
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
    assert "💸 With discount: 35.79 EUR (if paid by 25.08.2026)" in result
    assert "📄 Ticket: K 13247956" in result
    assert "🚗 Vehicle: XH2856" in result
    assert "⚖️ Violation: Art. 21, para. 2, of the Road Traffic Act (22.08.2026)" in result
    assert "<b>🏦 Pay by bank transfer:</b>" in result
    assert "Bank:   БНБ" in result
    assert "IBAN:   <code>BG64BNBG96613100147701</code>" in result
    assert "BIC:    BNBGBGSF" in result
    assert "Reason: ЕЛ.ФИШ СЕРИЯ K 13247956 24.08.2026" in result


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


# ── _translate_breach ─────────────────────────────────────────────────────────


def test_translate_breach_expands_common_abbreviations():
    assert (
        _translate_breach("чл. 21, ал. 2, от ЗДвП")
        == "Art. 21, para. 2, of the Road Traffic Act"
    )


def test_translate_breach_item_abbreviation():
    assert _translate_breach("чл. 137, ал. 1, т. 2") == "Art. 137, para. 1, item 2"


def test_translate_breach_leaves_unrecognized_text_untouched():
    assert _translate_breach("some other text") == "some other text"


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
    assert "💸 With discount: 35.79 EUR" in rendered  # no "(if paid by ...)" — no expirationDate given
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


@pytest.mark.asyncio
async def test_check_by_plate_http_status_error():
    response = MagicMock()
    response.status_code = 503

    with patch("notify_bot.services.mvr.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__ = AsyncMock(
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
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(MVRApiError, match="HTTP 503"):
            await check_by_plate("1234567890", "CB1234AB")
