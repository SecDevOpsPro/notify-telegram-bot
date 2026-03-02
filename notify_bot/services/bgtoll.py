"""
Async client for the Bulgarian e-Vignette check API (bgtoll.bg).

Endpoint: GET https://check.bgtoll.bg/check/vignette/plate/{country}/{plate}

⚠️  Cloudflare notice
The bgtoll.bg site is behind Cloudflare.  Requests from a server IP may receive
a 403 / Cloudflare challenge response.  The :class:`CloudflareBlockedError`
exception is raised in that case so callers can present a user-friendly message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://check.bgtoll.bg/check/vignette/plate"

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en,es;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "DNT": "1",
    "Pragma": "no-cache",
    "Referer": "https://check.bgtoll.bg/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}


# ── Exceptions ────────────────────────────────────────────────────────────────


class BgtollError(Exception):
    """Base exception for bgtoll.bg API errors."""


class CloudflareBlockedError(BgtollError):
    """
    Raised when Cloudflare returns a 403 / 503 challenge.

    The bot cannot solve a Cloudflare JS challenge.  The /vignette command
    catches this and advises the user accordingly.
    """


# ── Data class ────────────────────────────────────────────────────────────────


@dataclass
class VignetteInfo:
    """
    Vignette data returned by the API.

    All fields are optional because the shape of the response may vary and
    some may be absent when no valid vignette exists.
    """

    plate: str
    country: str
    found: bool = False

    # Fields populated when a vignette exists
    vignette_series: str | None = None
    validity_date_from: str | None = None
    validity_date_to: str | None = None
    vignette_type: str | None = None
    emission_class: str | None = None
    vehicle_type: str | None = None
    status: str | None = None
    status_boolean: bool | None = None

    # Raw payload for forward-compatibility
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_valid(self) -> bool:
        """True if the vignette is active.  Prefers the unambiguous statusBoolean field."""
        if not self.found:
            return False
        if self.status_boolean is not None:
            return self.status_boolean
        s = (self.status or "").upper()
        return s in {"VALID", "ACTIVE", "OK"}


# ── Parser ────────────────────────────────────────────────────────────────────


def _parse(plate: str, country: str, data: dict) -> VignetteInfo:
    """
    Parse the bgtoll.bg API response into a :class:`VignetteInfo`.

    The parser is intentionally lenient — it tries several known field names
    (the API has changed its shape in the past) and falls back to ``None``
    for missing keys.
    """
    # The payload may be nested under a "vignette" key or returned flat.
    # An explicit null/None value under "vignette" means "no vignette found".
    if "vignette" in data and data["vignette"] is None:
        return VignetteInfo(plate=plate, country=country, found=False, raw=data)

    payload: dict = data.get("vignette") or data.get("vignetteData") or data

    if not payload:
        return VignetteInfo(plate=plate, country=country, found=False, raw=data)

    def _get(*keys: str) -> str | None:
        for k in keys:
            v = payload.get(k)
            if v is not None:
                return str(v)
        return None

    # Prefer the pre-formatted date strings for display; fall back to ISO dates
    status_bool_raw = payload.get("statusBoolean")

    return VignetteInfo(
        plate=plate,
        country=country,
        found=True,
        vignette_series=_get("vignetteNumber", "vignetteSeries", "series", "id"),
        validity_date_from=_get(
            "validityDateFromFormated",
            "validityDateFrom",
            "validFrom",
            "from",
            "startDate",
            "dateFrom",
        ),
        validity_date_to=_get(
            "validityDateToFormated", "validityDateTo", "validTo", "to", "endDate", "dateTo"
        ),
        vignette_type=_get("vignetteType", "type", "category"),
        emission_class=_get("emissionsClass", "emissionClass", "emission", "euroClass"),
        vehicle_type=_get("vehicleType", "vehicleTypeCode", "vehicle", "vehicleCategory"),
        status=_get("status", "vignetteStatus", "state"),
        status_boolean=bool(status_bool_raw) if status_bool_raw is not None else None,
        raw=data,
    )


# ── Public API ────────────────────────────────────────────────────────────────


async def check_vignette(
    plate: str,
    country: str = "BG",
) -> VignetteInfo:
    """
    Check whether a vehicle has a valid e-vignette.

    Args:
        plate:    Vehicle registration plate (e.g. ``AA1234BB``).
        country:  ISO-3166-1 alpha-2 country code for the plate (default ``BG``).

    Returns:
        :class:`VignetteInfo` describing the vignette state.

    Raises:
        :class:`CloudflareBlockedError`: when Cloudflare intercepts the request.
        :class:`BgtollError`:            on any other HTTP or connection error.
    """
    plate = plate.upper()
    country = country.upper()
    url = f"{_BASE_URL}/{country}/{plate}"
    logger.debug("Vignette check: GET %s", url)

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=_HEADERS)
    except httpx.HTTPError as exc:
        raise BgtollError(f"Connection error: {exc}") from exc

    if resp.status_code in (403, 503):
        raise CloudflareBlockedError(
            "Cloudflare blocked the request (status %d). "
            "The bgtoll.bg site requires a browser session to pass the bot-detection challenge."
            % resp.status_code
        )

    if resp.status_code == 404:
        # 404 typically means "no vignette found for this plate"
        return VignetteInfo(plate=plate, country=country, found=False, raw={})

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise BgtollError(f"API returned HTTP {resp.status_code}") from exc

    try:
        data = resp.json()
    except Exception as exc:
        raise BgtollError("API returned non-JSON response") from exc

    return _parse(plate, country, data)
