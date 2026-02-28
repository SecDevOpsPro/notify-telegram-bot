"""
Tests for notify_bot.services.sofiatraffic
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notify_bot.services.sofiatraffic import (
    ClampInfo,
    CloudflareError,
    CsrfFetchError,
    SofiaTrafficError,
    StickerInfo,
    _parse_clamp,
    _parse_sticker,
    check_clamp,
    check_sticker,
)


# ── _parse_sticker ─────────────────────────────────────────────────────────────


class TestParseSticker:
    def test_explicit_null_returns_not_found(self):
        result = _parse_sticker("CB1234AB", {"sticker": None})
        assert result.found is False
        assert result.plate == "CB1234AB"

    def test_nested_sticker_object(self):
        data = {
            "sticker": {
                "validFrom": "2026-01-01",
                "validTo": "2026-12-31",
                "zone": "blue",
                "status": "VALID",
            }
        }
        result = _parse_sticker("CB1234AB", data)
        assert result.found is True
        assert result.valid_from == "2026-01-01"
        assert result.valid_to == "2026-12-31"
        assert result.zone == "blue"
        assert result.status == "VALID"

    def test_flat_response_with_zone_field(self):
        data = {"zone": "green", "valid_from": "2026-03-01", "valid_to": "2026-05-01"}
        result = _parse_sticker("CB9999ZZ", data)
        assert result.found is True
        assert result.zone == "green"
        assert result.valid_from == "2026-03-01"

    def test_empty_dict_returns_not_found(self):
        result = _parse_sticker("CB1234AB", {})
        assert result.found is False

    def test_sticker_data_key_variant(self):
        data = {"stickerData": {"status": "ACTIVE", "zone": "red"}}
        result = _parse_sticker("CB5678CD", data)
        assert result.found is True
        assert result.status == "ACTIVE"
        assert result.zone == "red"

    def test_raw_stored(self):
        raw = {"sticker": {"status": "VALID"}}
        result = _parse_sticker("AB1234CD", raw)
        assert result.raw is raw


# ── StickerInfo.is_valid ──────────────────────────────────────────────────────


class TestStickerIsValid:
    @pytest.mark.parametrize(
        "status, expected",
        [
            ("VALID", True),
            ("valid", True),
            ("ACTIVE", True),
            ("OK", True),
            ("ACTIVE_STICKER", True),
            ("EXPIRED", False),
            ("INVALID", False),
            (None, True),   # found but no status → treated as valid
        ],
    )
    def test_status_variants(self, status, expected):
        info = StickerInfo(plate="CB1234AB", found=True, status=status)
        assert info.is_valid == expected

    def test_not_found_is_never_valid(self):
        info = StickerInfo(plate="CB1234AB", found=False, status="VALID")
        assert info.is_valid is False


# ── _parse_clamp ──────────────────────────────────────────────────────────────


class TestParseClamp:
    def test_explicit_null_returns_not_clamped(self):
        result = _parse_clamp("CB1234AB", {"clamp": None})
        assert result.found is False
        assert result.clamped is False

    def test_clamped_object(self):
        data = {
            "clamp": {
                "clamped": True,
                "clampedAt": "2026-02-28 10:00",
                "location": "ul. Vitosha 1",
            }
        }
        result = _parse_clamp("CB1234AB", data)
        assert result.found is True
        assert result.clamped is True
        assert result.clamped_at == "2026-02-28 10:00"
        assert result.location == "ul. Vitosha 1"

    def test_not_clamped_object(self):
        data = {"clamp": {"clamped": False}}
        result = _parse_clamp("CB1234AB", data)
        assert result.found is True
        assert result.clamped is False

    def test_top_level_clamped_flag_true(self):
        data = {"clamped": True}
        result = _parse_clamp("CB1234AB", data)
        assert result.found is True
        assert result.clamped is True

    def test_top_level_clamped_flag_false(self):
        data = {"clamped": False}
        result = _parse_clamp("CB1234AB", data)
        assert result.clamped is False

    def test_empty_dict_returns_not_found(self):
        result = _parse_clamp("CB1234AB", {})
        assert result.found is False
        assert result.clamped is False

    def test_clamp_with_release_instructions(self):
        data = {"clamp": {"clamped": True, "instructions": "Call 0700 17 100"}}
        result = _parse_clamp("CB1234AB", data)
        assert result.release_instructions == "Call 0700 17 100"

    def test_raw_stored(self):
        raw = {"clamp": {"clamped": False}}
        result = _parse_clamp("CB9000XY", raw)
        assert result.raw is raw


# ── check_sticker (network) ───────────────────────────────────────────────────


def _make_response(status_code: int, body: dict | None = None) -> httpx.Response:
    content = json.dumps(body or {}).encode()
    resp = httpx.Response(status_code, content=content, headers={"Content-Type": "application/json"})
    resp.request = httpx.Request("GET", "https://www.sofiatraffic.bg/bg/parking/sticker/TEST")
    return resp


def _mock_csrf_client(sticker_response: httpx.Response):
    """Return a mock async context manager that simulates the full CSRF flow."""
    page_response = httpx.Response(200, content=b"<html>parking</html>")

    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    mock_client.cookies = {"XSRF-TOKEN": "test-token-value"}
    mock_client.get = AsyncMock(side_effect=[page_response, sticker_response])
    return mock_client


class TestCheckSticker:
    async def test_found_sticker(self):
        resp = _make_response(200, {"sticker": {"status": "VALID", "zone": "blue"}})
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            result = await check_sticker("cb1234ab")
        assert result.plate == "CB1234AB"
        assert result.found is True
        assert result.zone == "blue"

    async def test_404_returns_not_found(self):
        resp = _make_response(404)
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            result = await check_sticker("CB1234AB")
        assert result.found is False

    async def test_cloudflare_403_raises(self):
        resp = _make_response(403)
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            with pytest.raises(CloudflareError):
                await check_sticker("CB1234AB")

    async def test_cloudflare_503_raises(self):
        resp = _make_response(503)
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            with pytest.raises(CloudflareError):
                await check_sticker("CB1234AB")

    async def test_non_json_raises(self):
        resp = httpx.Response(200, content=b"not json", headers={"Content-Type": "text/html"})
        resp.request = httpx.Request("GET", "https://www.sofiatraffic.bg/bg/parking/sticker/TEST")
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            with pytest.raises(SofiaTrafficError, match="non-JSON"):
                await check_sticker("CB1234AB")

    async def test_plate_uppercased(self):
        resp = _make_response(200, {"sticker": None})
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            result = await check_sticker("aa1234bb")
        assert result.plate == "AA1234BB"


# ── check_clamp (network) ─────────────────────────────────────────────────────


class TestCheckClamp:
    async def test_clamped_vehicle(self):
        resp = _make_response(200, {"clamp": {"clamped": True, "clampedAt": "2026-02-28 09:00"}})
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            result = await check_clamp("CB1234AB")
        assert result.found is True
        assert result.clamped is True
        assert result.clamped_at == "2026-02-28 09:00"

    async def test_not_clamped_null(self):
        resp = _make_response(200, {"clamp": None})
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            result = await check_clamp("CB1234AB")
        assert result.found is False
        assert result.clamped is False

    async def test_404_returns_not_found(self):
        resp = _make_response(404)
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            result = await check_clamp("CB1234AB")
        assert result.found is False

    async def test_cloudflare_raises(self):
        resp = _make_response(403)
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            with pytest.raises(CloudflareError):
                await check_clamp("CB1234AB")

    async def test_plate_uppercased(self):
        resp = _make_response(200, {"clamp": None})
        with patch("notify_bot.services.sofiatraffic._get_csrf_client",
                   new_callable=AsyncMock,
                   return_value=(MagicMock(get=AsyncMock(return_value=resp), aclose=AsyncMock()),
                                 "token")):
            result = await check_clamp("aa1234bb")
        assert result.plate == "AA1234BB"


# ── _get_csrf_client edge cases ───────────────────────────────────────────────


class TestGetCsrfClient:
    async def test_cloudflare_on_page_visit(self):
        from notify_bot.services.sofiatraffic import _get_csrf_client

        page_resp = httpx.Response(403, content=b"Cloudflare")
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        mock_client.get = AsyncMock(return_value=page_resp)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            with pytest.raises(CloudflareError):
                await _get_csrf_client()

    async def test_missing_csrf_cookie(self):
        from notify_bot.services.sofiatraffic import _get_csrf_client

        page_resp = httpx.Response(200, content=b"<html>")
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        mock_client.get = AsyncMock(return_value=page_resp)
        mock_client.cookies = {}  # no XSRF-TOKEN

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            with pytest.raises(CsrfFetchError):
                await _get_csrf_client()
