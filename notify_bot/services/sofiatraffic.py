"""
Async client for the Sofia Traffic parking API (sofiatraffic.bg).

Endpoints used:
  GET /bg/parking/sticker/{plate} — Check parking e-vignette sticker
  GET /bg/parking/clamp/{plate}   — Check wheel-clamp status

⚠️  CSRF notice
The site is a Laravel application that requires a valid XSRF-TOKEN before
accepting XHR requests.  Each call must first visit the parking landing page to
obtain a session cookie and the XSRF-TOKEN, then pass the decoded token as the
``X-XSRF-TOKEN`` header on the actual API request.

⚠️  Cloudflare notice
Like many Bulgarian government services, the site may be behind Cloudflare.
A :class:`CloudflareError` is raised when a 403 / 503 challenge response is
detected so callers can present a user-friendly fallback.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_PARKING_PAGE_URL = "https://www.sofiatraffic.bg/bg/parking"
_API_BASE = "https://www.sofiatraffic.bg/bg/parking"

_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,es;q=0.9",
    "Cache-Control": "no-cache",
    "DNT": "1",
    "Pragma": "no-cache",
    "Referer": "https://www.sofiatraffic.bg/en/parking",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

_XHR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en,es;q=0.9",
    "Cache-Control": "no-cache",
    "DNT": "1",
    "Pragma": "no-cache",
    "Referer": "https://www.sofiatraffic.bg/en/parking",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


# ── Exceptions ────────────────────────────────────────────────────────────────


class SofiaTrafficError(Exception):
    """Base exception for Sofia Traffic API errors."""


class CloudflareError(SofiaTrafficError):
    """
    Raised when Cloudflare returns a 403 / 503 challenge.

    The bot cannot solve a Cloudflare JS challenge.  Callers should present
    a user-friendly message with a direct link to the website.
    """


class CsrfFetchError(SofiaTrafficError):
    """Raised when the XSRF-TOKEN cookie cannot be obtained from the parking page."""


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class StickerInfo:
    """
    Parking e-vignette sticker data returned by the API.

    ``found=False`` means no sticker is registered for the plate.
    All optional fields may be ``None`` when the sticker is not found or the
    API does not return them.
    """

    plate: str
    found: bool = False

    valid_from: str | None = None
    valid_to: str | None = None
    zone: str | None = None
    sticker_type: str | None = None
    status: str | None = None

    # Raw payload for forward-compatibility
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_valid(self) -> bool:
        """True when the sticker is present and reports an active/valid status."""
        if not self.found:
            return False
        s = (self.status or "").upper()
        return s in {"VALID", "ACTIVE", "OK", "ACTIVE_STICKER"} or (self.found and not self.status)


@dataclass
class ClampInfo:
    """
    Wheel-clamp status data returned by the API.

    ``clamped=True`` means the vehicle is currently wheel-clamped.
    ``found=False`` means no clamp record was found (i.e. not clamped).
    """

    plate: str
    found: bool = False
    clamped: bool = False

    clamped_at: str | None = None
    location: str | None = None
    release_instructions: str | None = None

    # Raw payload for forward-compatibility
    raw: dict = field(default_factory=dict, compare=False, repr=False)


# ── Parsers ───────────────────────────────────────────────────────────────────


def _parse_sticker(plate: str, data: dict) -> StickerInfo:
    """
    Parse the /sticker API response into a :class:`StickerInfo`.

    The parser is intentionally lenient — it tries several known field names
    and falls back to ``None`` for missing keys.
    """
    # Explicit null at top level → not found
    if "sticker" in data and data["sticker"] is None:
        return StickerInfo(plate=plate, found=False, raw=data)

    payload: dict = data.get("sticker") or data.get("stickerData") or data.get("data") or data

    if (
        not payload
        or payload is data
        and not any(k in data for k in ("validFrom", "valid_from", "zone", "status", "plateNumber"))
    ):
        return StickerInfo(plate=plate, found=False, raw=data)

    def _get(*keys: str) -> str | None:
        for k in keys:
            v = payload.get(k)
            if v is not None:
                return str(v)
        return None

    return StickerInfo(
        plate=plate,
        found=True,
        valid_from=_get("validFrom", "valid_from", "from", "startDate", "dateFrom"),
        valid_to=_get("validTo", "valid_to", "to", "endDate", "dateTo"),
        zone=_get("zone", "parkingZone", "zoneCode", "area"),
        sticker_type=_get("type", "stickerType", "category"),
        status=_get("status", "stickerStatus", "state"),
        raw=data,
    )


def _parse_clamp(plate: str, data: dict) -> ClampInfo:
    """
    Parse the /clamp API response into a :class:`ClampInfo`.

    The parser is intentionally lenient.
    """
    # Explicit null → not clamped
    if "clamp" in data and data["clamp"] is None:
        return ClampInfo(plate=plate, found=False, clamped=False, raw=data)

    payload: dict = data.get("clamp") or data.get("clampData") or data.get("data") or {}

    if not payload:
        # Check for a boolean "clamped" field at top level
        clamped_flag = data.get("clamped") or data.get("isClamped") or data.get("is_clamped")
        if clamped_flag is not None:
            return ClampInfo(
                plate=plate,
                found=True,
                clamped=bool(clamped_flag),
                raw=data,
            )
        return ClampInfo(plate=plate, found=False, clamped=False, raw=data)

    def _get(*keys: str) -> str | None:
        for k in keys:
            v = payload.get(k)
            if v is not None:
                return str(v)
        return None

    clamped = bool(
        payload.get("clamped")
        or payload.get("isClamped")
        or payload.get("is_clamped")
        or payload.get("clampedAt")
        or payload.get("clamped_at")
    )

    return ClampInfo(
        plate=plate,
        found=True,
        clamped=clamped,
        clamped_at=_get("clampedAt", "clamped_at", "clampDate", "clamp_date", "date"),
        location=_get("location", "address", "clampLocation", "clamp_location", "street"),
        release_instructions=_get(
            "releaseInstructions", "release_instructions", "instructions", "info"
        ),
        raw=data,
    )


# ── Internal CSRF helper ──────────────────────────────────────────────────────


async def _get_csrf_client() -> tuple[httpx.AsyncClient, str]:
    """
    Return a new *open* httpx client and the decoded XSRF token.

    The caller is responsible for closing the client (use ``async with``).

    Raises:
        :class:`CloudflareError`: when Cloudflare challenges the page visit.
        :class:`CsrfFetchError`: when the XSRF-TOKEN cookie is absent.
        :class:`SofiaTrafficError`: on connection failures.
    """
    client = httpx.AsyncClient(
        follow_redirects=True,
        headers=_BROWSER_HEADERS,
        timeout=20.0,
    )
    try:
        r = await client.get(_PARKING_PAGE_URL)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise SofiaTrafficError(f"Connection error fetching parking page: {exc}") from exc

    if r.status_code in (403, 503):
        await client.aclose()
        raise CloudflareError(
            "Cloudflare blocked the request (status %d) while fetching the parking page."
            % r.status_code
        )

    xsrf_raw = client.cookies.get("XSRF-TOKEN")
    if not xsrf_raw:
        await client.aclose()
        raise CsrfFetchError(
            "Could not obtain XSRF-TOKEN cookie from the Sofia Traffic parking page. "
            "The site may have changed its session mechanism."
        )

    xsrf = urllib.parse.unquote(xsrf_raw)
    return client, xsrf


# ── Public API ────────────────────────────────────────────────────────────────


async def check_sticker(plate: str) -> StickerInfo:
    """
    Check whether a vehicle has a registered parking e-vignette sticker in Sofia.

    Args:
        plate: Vehicle registration plate (e.g. ``AA1234BB``).

    Returns:
        :class:`StickerInfo` describing the sticker state.

    Raises:
        :class:`CloudflareError`:  when Cloudflare challenges any request.
        :class:`CsrfFetchError`:   when the CSRF token cannot be obtained.
        :class:`SofiaTrafficError`: on any other HTTP or connection error.
    """
    plate = plate.upper()
    url = f"{_API_BASE}/sticker/{plate}"
    logger.debug("Parking sticker check: GET %s", url)

    client, xsrf = await _get_csrf_client()
    try:
        resp = await client.get(
            url,
            headers={**_XHR_HEADERS, "X-XSRF-TOKEN": xsrf},
        )
    except httpx.HTTPError as exc:
        raise SofiaTrafficError(f"Connection error: {exc}") from exc
    finally:
        await client.aclose()

    if resp.status_code in (403, 503):
        raise CloudflareError(
            "Cloudflare blocked the sticker API request (status %d)." % resp.status_code
        )

    if resp.status_code == 404:
        return StickerInfo(plate=plate, found=False, raw={})

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SofiaTrafficError(f"API returned HTTP {resp.status_code}") from exc

    try:
        data = resp.json()
    except Exception as exc:
        raise SofiaTrafficError("API returned non-JSON response") from exc

    return _parse_sticker(plate, data)


async def check_clamp(plate: str) -> ClampInfo:
    """
    Check whether a vehicle is currently wheel-clamped in Sofia.

    Args:
        plate: Vehicle registration plate (e.g. ``AA1234BB``).

    Returns:
        :class:`ClampInfo` describing the clamp state.

    Raises:
        :class:`CloudflareError`:  when Cloudflare challenges any request.
        :class:`CsrfFetchError`:   when the CSRF token cannot be obtained.
        :class:`SofiaTrafficError`: on any other HTTP or connection error.
    """
    plate = plate.upper()
    url = f"{_API_BASE}/clamp/{plate}"
    logger.debug("Wheel clamp check: GET %s", url)

    client, xsrf = await _get_csrf_client()
    try:
        resp = await client.get(
            url,
            headers={**_XHR_HEADERS, "X-XSRF-TOKEN": xsrf},
        )
    except httpx.HTTPError as exc:
        raise SofiaTrafficError(f"Connection error: {exc}") from exc
    finally:
        await client.aclose()

    if resp.status_code in (403, 503):
        raise CloudflareError(
            "Cloudflare blocked the clamp API request (status %d)." % resp.status_code
        )

    if resp.status_code == 404:
        return ClampInfo(plate=plate, found=False, clamped=False, raw={})

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SofiaTrafficError(f"API returned HTTP {resp.status_code}") from exc

    try:
        data = resp.json()
    except Exception as exc:
        raise SofiaTrafficError("API returned non-JSON response") from exc

    return _parse_clamp(plate, data)
