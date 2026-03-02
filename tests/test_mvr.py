"""Tests for the MVR obligations service (notify_bot/services/mvr.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notify_bot.services.mvr import (
    MVRApiError,
    Obligation,
    _parse,
    check_by_licence,
    check_by_plate,
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
