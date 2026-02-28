"""Tests for the bgtoll.bg e-vignette service (notify_bot/services/bgtoll.py)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notify_bot.services.bgtoll import (
    BgtollError,
    CloudflareBlockedError,
    VignetteInfo,
    _parse,
    check_vignette,
)


# ── _parse (pure function) ────────────────────────────────────────────────────


def test_parse_nested_vignette_key():
    data = {
        "vignette": {
            "vignetteType": "Annual",
            "validityDateFrom": "2025-01-01",
            "validityDateTo": "2025-12-31",
            "status": "VALID",
            "emissionClass": "Euro 5",
            "vehicleType": "Car",
            "vignetteSeries": "B12345",
        }
    }
    result = _parse("CB1234AB", "BG", data)

    assert result.found is True
    assert result.is_valid is True
    assert result.vignette_type == "Annual"
    assert result.validity_date_from == "2025-01-01"
    assert result.validity_date_to == "2025-12-31"
    assert result.emission_class == "Euro 5"
    assert result.vehicle_type == "Car"
    assert result.vignette_series == "B12345"


def test_parse_flat_response():
    """API sometimes returns flat JSON without a 'vignette' wrapper."""
    data = {
        "status": "VALID",
        "validFrom": "2025-03-01",
        "validTo": "2026-02-28",
        "type": "Annual",
    }
    result = _parse("PB5678CD", "BG", data)

    assert result.found is True
    assert result.validity_date_from == "2025-03-01"
    assert result.vignette_type == "Annual"


def test_parse_empty_payload_returns_not_found():
    result = _parse("XX9999XX", "BG", {})
    assert result.found is False
    assert result.is_valid is False


def test_parse_explicit_null_vignette():
    result = _parse("XX9999XX", "BG", {"vignette": None})
    assert result.found is False


def test_parse_unknown_status_not_valid():
    data = {"vignette": {"status": "EXPIRED"}}
    result = _parse("CB1234AB", "BG", data)
    assert result.found is True
    assert result.is_valid is False


def test_parse_plate_and_country_preserved():
    result = _parse("CB1234AB", "BG", {})
    assert result.plate == "CB1234AB"
    assert result.country == "BG"


# ── VignetteInfo.is_valid ────────────────────────────────────────────────────


@pytest.mark.parametrize("status,expected", [
    ("VALID", True),
    ("valid", True),
    ("ACTIVE", True),
    ("OK", True),
    ("EXPIRED", False),
    ("INVALID", False),
    (None, False),
])
def test_vignette_info_is_valid(status, expected):
    info = VignetteInfo(plate="X", country="BG", found=True, status=status)
    assert info.is_valid is expected


def test_vignette_info_is_valid_false_when_not_found():
    info = VignetteInfo(plate="X", country="BG", found=False, status="VALID")
    assert info.is_valid is False


# ── check_vignette (mocked network) ──────────────────────────────────────────


def _mock_client(status_code: int, json_data: dict | None = None, raise_exc=None):
    """Helper: returns a mocked AsyncClient context manager."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if json_data is not None:
        mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()

    mock_get = AsyncMock(return_value=mock_resp) if not raise_exc else AsyncMock(side_effect=raise_exc)
    mock_client_instance = MagicMock(get=mock_get)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    return mock_ctx


@pytest.mark.asyncio
async def test_check_vignette_success():
    payload = {
        "vignette": {
            "status": "VALID",
            "vignetteType": "Annual",
            "validityDateFrom": "2025-01-01",
            "validityDateTo": "2025-12-31",
        }
    }
    with patch("notify_bot.services.bgtoll.httpx.AsyncClient", return_value=_mock_client(200, payload)):
        result = await check_vignette("CB1234AB")

    assert result.found is True
    assert result.is_valid is True
    assert result.vignette_type == "Annual"
    # plate should be upper-cased
    assert result.plate == "CB1234AB"


@pytest.mark.asyncio
async def test_check_vignette_plate_uppercased():
    payload = {"vignette": {"status": "VALID"}}
    with patch("notify_bot.services.bgtoll.httpx.AsyncClient", return_value=_mock_client(200, payload)):
        result = await check_vignette("cb1234ab")
    assert result.plate == "CB1234AB"


@pytest.mark.asyncio
async def test_check_vignette_404_returns_not_found():
    with patch("notify_bot.services.bgtoll.httpx.AsyncClient", return_value=_mock_client(404)):
        result = await check_vignette("NOTEXIST")

    assert result.found is False
    assert result.is_valid is False


@pytest.mark.asyncio
async def test_check_vignette_403_raises_cloudflare_error():
    with patch("notify_bot.services.bgtoll.httpx.AsyncClient", return_value=_mock_client(403)):
        with pytest.raises(CloudflareBlockedError):
            await check_vignette("CB1234AB")


@pytest.mark.asyncio
async def test_check_vignette_503_raises_cloudflare_error():
    with patch("notify_bot.services.bgtoll.httpx.AsyncClient", return_value=_mock_client(503)):
        with pytest.raises(CloudflareBlockedError):
            await check_vignette("CB1234AB")


@pytest.mark.asyncio
async def test_check_vignette_connection_error_raises_bgtoll_error():
    with patch(
        "notify_bot.services.bgtoll.httpx.AsyncClient",
        return_value=_mock_client(0, raise_exc=httpx.ConnectError("refused")),
    ):
        with pytest.raises(BgtollError, match="Connection error"):
            await check_vignette("CB1234AB")


@pytest.mark.asyncio
async def test_check_vignette_non_json_raises_bgtoll_error():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("not json")
    mock_resp.raise_for_status = MagicMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(
        return_value=MagicMock(get=AsyncMock(return_value=mock_resp))
    )
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("notify_bot.services.bgtoll.httpx.AsyncClient", return_value=mock_ctx):
        with pytest.raises(BgtollError, match="non-JSON"):
            await check_vignette("CB1234AB")
