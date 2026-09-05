"""Tests for the bgtoll.bg e-vignette service (notify_bot/services/bgtoll.py)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notify_bot.services.bgtoll import (
    BgtollError,
    CloudflareBlockedError,
    VignetteInfo,
    _format_validity_date,
    _parse,
    _parse_bg_datetime,
    check_vignette,
    format_validity_period,
)

# ── _format_validity_date (pure function) ────────────────────────────────────


def test_format_validity_date_strips_midnight():
    assert _format_validity_date("07.06.2026 00:00:00") == "07.06.2026"


def test_format_validity_date_marks_end_of_day_inclusive():
    assert _format_validity_date("06.06.2027 23:59:59") == "06.06.2027 (Including)"


def test_format_validity_date_passes_through_other_values():
    assert _format_validity_date("2025-01-01") == "2025-01-01"
    assert _format_validity_date("07.06.2026 12:30:00") == "07.06.2026 12:30:00"


# ── _parse_bg_datetime (pure function) ───────────────────────────────────────


def test_parse_bg_datetime_with_time():
    assert _parse_bg_datetime("07.06.2026 00:00:00") == datetime(2026, 6, 7, 0, 0, 0)


def test_parse_bg_datetime_date_only():
    assert _parse_bg_datetime("07.06.2026") == datetime(2026, 6, 7)


def test_parse_bg_datetime_unrecognized_format():
    assert _parse_bg_datetime("not-a-date") is None


# ── format_validity_period (pure function) ───────────────────────────────────


def test_format_validity_period_not_started_yet():
    # Start date far in the future relative to any real "now".
    lines = format_validity_period("01.01.2099 00:00:00", "31.12.2099 23:59:59")
    assert lines == ["📅 Validity starting: 01.01.2099 until 31.12.2099 (Including)"]


def test_format_validity_period_already_started():
    lines = format_validity_period("01.01.2000 00:00:00", "31.12.2099 23:59:59")
    assert lines == ["📅 Started: 01.01.2000", "📅 Ends: 31.12.2099 (Including)"]


def test_format_validity_period_already_started_date_only():
    # boleron-style dates carry no time component.
    lines = format_validity_period("01.01.2000", "31.12.2099")
    assert lines == ["📅 Started: 01.01.2000", "📅 Ends: 31.12.2099"]


def test_format_validity_period_already_started_no_end_date():
    assert format_validity_period("01.01.2000 00:00:00", None) == ["📅 Started: 01.01.2000"]


def test_format_validity_period_no_start_date_returns_empty():
    assert format_validity_period(None, "31.12.2099") == []


def test_format_validity_period_unparseable_start_falls_back_to_valid_line():
    lines = format_validity_period("not-a-date", "31.12.2099")
    assert lines == ["📅 Validity starting: not-a-date until 31.12.2099"]


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


def test_parse_keeps_raw_validity_dates_with_time():
    """_parse stores the raw date+time strings; formatting happens at render time."""
    data = {
        "vignette": {
            "validityDateFromFormated": "07.06.2026 00:00:00",
            "validityDateToFormated": "06.06.2027 23:59:59",
            "status": "VALID",
        }
    }
    result = _parse("XH2856", "BG", data)

    assert result.validity_date_from == "07.06.2026 00:00:00"
    assert result.validity_date_to == "06.06.2027 23:59:59"


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


@pytest.mark.parametrize(
    "status,expected",
    [
        ("VALID", True),
        ("valid", True),
        ("ACTIVE", True),
        ("OK", True),
        ("EXPIRED", False),
        ("INVALID", False),
        (None, False),
    ],
)
def test_vignette_info_is_valid(status, expected):
    info = VignetteInfo(plate="X", country="BG", found=True, status=status)
    assert info.is_valid is expected


def test_vignette_info_is_valid_false_when_not_found():
    info = VignetteInfo(plate="X", country="BG", found=False, status="VALID")
    assert info.is_valid is False


def test_status_boolean_string_false_is_treated_as_false():
    data = {"vignette": {"statusBoolean": "false", "status": "VALID"}}
    result = _parse("CB1234AB", "BG", data)

    assert result.found is True
    assert result.status_boolean is False
    assert result.is_valid is False


def test_status_boolean_string_true_is_treated_as_true():
    data = {"vignette": {"statusBoolean": "true", "status": "EXPIRED"}}
    result = _parse("CB1234AB", "BG", data)

    assert result.status_boolean is True
    assert result.is_valid is True


# ── check_vignette (mocked network) ──────────────────────────────────────────


def _mock_client(status_code: int, json_data: dict | None = None, raise_exc=None):
    """Helper: returns a mocked AsyncClient context manager."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if json_data is not None:
        mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()

    mock_get = (
        AsyncMock(return_value=mock_resp) if not raise_exc else AsyncMock(side_effect=raise_exc)
    )
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
    with patch(
        "notify_bot.services.bgtoll.httpx.AsyncClient", return_value=_mock_client(200, payload)
    ):
        result = await check_vignette("CB1234AB")

    assert result.found is True
    assert result.is_valid is True
    assert result.vignette_type == "Annual"
    # plate should be upper-cased
    assert result.plate == "CB1234AB"


@pytest.mark.asyncio
async def test_check_vignette_plate_uppercased():
    payload = {"vignette": {"status": "VALID"}}
    with patch(
        "notify_bot.services.bgtoll.httpx.AsyncClient", return_value=_mock_client(200, payload)
    ):
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
    mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(get=AsyncMock(return_value=mock_resp)))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("notify_bot.services.bgtoll.httpx.AsyncClient", return_value=mock_ctx):
        with pytest.raises(BgtollError, match="non-JSON"):
            await check_vignette("CB1234AB")
