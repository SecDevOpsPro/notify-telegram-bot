"""
Async client for the Bulgarian MVR e-services Obligations API.

Two lookup modes are supported:
- By driving licence number (``check_by_licence``)
- By vehicle plate number  (``check_by_plate``)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://e-uslugi.mvr.bg/api/Obligations/AND"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en",
    "Referer": "https://e-uslugi.mvr.bg/en/services/obligations",
    "Origin": "https://e-uslugi.mvr.bg",
    "Content-Type": "application/json; charset=utf-8",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "pragma": "no-cache",
    "dnt": "1",
    "cache-control": "no-cache",
}

_COOKIES = {
    "EAUSessionID": "b5345242-002d-4cca-878a-991c3db0cf0e",
    "currentLang": "en",
}

#: Maps ``unitGroup`` integer values to human-readable law descriptions.
LAW_MAP: dict[int, str] = {
    1: "Road Traffic Act and/or Insurance Code",
    2: "Law for Bulgarian Personal Documents",
}


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class Obligation:
    """A single obligation group returned by the MVR API."""

    unit_group: int
    unit_group_label: str
    obligations: list[Any] = field(default_factory=list)

    @property
    def has_obligations(self) -> bool:
        return bool(self.obligations)


# ── Exceptions ───────────────────────────────────────────────────────────────


class MVRApiError(Exception):
    """Raised when the MVR API returns an unexpected response or HTTP error."""


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _fetch(params: dict[str, str]) -> dict:
    async with httpx.AsyncClient(
        timeout=60.0,
        trust_env=True,  # respects HTTP_PROXY / HTTPS_PROXY env vars
    ) as client:
        resp = await client.get(_BASE_URL, params=params, headers=_HEADERS, cookies=_COOKIES)
        resp.raise_for_status()
        return resp.json()


def _parse(data: dict) -> list[Obligation]:
    result: list[Obligation] = []
    for unit in data.get("obligationsData", []):
        ug: int = unit.get("unitGroup", 0)
        label = LAW_MAP.get(ug, f"Obligation group {ug}")
        obligations = unit.get("obligations", [])
        result.append(Obligation(unit_group=ug, unit_group_label=label, obligations=obligations))
    return result


# ── Public API ────────────────────────────────────────────────────────────────


async def check_by_licence(national_id: str, licence_number: str) -> list[Obligation]:
    """
    Check traffic/document obligations by driving licence number.

    Args:
        national_id:      10-digit Bulgarian EGN.
        licence_number:   Driving licence number (digits only).

    Returns:
        List of :class:`Obligation` objects (one per law group).

    Raises:
        :class:`MVRApiError`: on HTTP or JSON errors.
    """
    params = {
        "obligatedPersonType": "1",
        "additinalDataForObligatedPersonType": "1",
        "mode": "1",
        "obligedPersonIdent": national_id,
        "drivingLicenceNumber": licence_number,
    }
    try:
        data = await _fetch(params)
    except httpx.HTTPStatusError as exc:
        raise MVRApiError(f"MVR API returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise MVRApiError(f"MVR API connection error: {exc}") from exc

    return _parse(data)


async def check_by_plate(national_id: str, plate_number: str) -> list[Obligation]:
    """
    Check traffic/document obligations by vehicle plate number.

    Args:
        national_id:    10-digit Bulgarian EGN.
        plate_number:   Vehicle registration plate (e.g. ``CB1234AB``).

    Returns:
        List of :class:`Obligation` objects (one per law group).

    Raises:
        :class:`MVRApiError`: on HTTP or JSON errors.
    """
    params = {
        "obligatedPersonType": "1",
        "additinalDataForObligatedPersonType": "3",
        "mode": "1",
        "obligedPersonIdent": national_id,
        "foreignVehicleNumber": plate_number.upper(),
    }
    try:
        data = await _fetch(params)
    except httpx.HTTPStatusError as exc:
        raise MVRApiError(f"MVR API returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise MVRApiError(f"MVR API connection error: {exc}") from exc

    return _parse(data)
