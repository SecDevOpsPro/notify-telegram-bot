"""Async client for the CambioCuba informal exchange-rate API."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.cambiocuba.money/api/v1/x-rates-by-date-range"
_PHOTO_URL = "https://wa.cambiocuba.money/trmi.png?trmi=true&cur={cur}"

_HEADERS = {
    "authority": "api.cambiocuba.money",
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://img.cambiocuba.money",
    "referer": "https://img.cambiocuba.money/",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
    ),
}


async def get_rates(
    currency: str = "ECU",
    period: str = "7D",
) -> tuple[list[dict], str]:
    """
    Fetch informal exchange rates for a given currency and time period.

    Args:
        currency: Three-letter currency code (default ``ECU`` for EUR/CUP).
        period:   Period string understood by the API (``7D``, ``30D``, etc.).

    Returns:
        A tuple of ``(data_list, photo_url)`` where ``data_list`` is the raw
        JSON array from the API and ``photo_url`` is the matching chart image.

    Raises:
        ``httpx.HTTPStatusError``: on non-2xx responses.
        ``httpx.HTTPError``: on connection errors.
    """
    params = {"trmi": "true", "cur": currency, "period": period}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_BASE_URL, params=params, headers=_HEADERS)
        resp.raise_for_status()

    photo_url = _PHOTO_URL.format(cur=currency, period=period)
    return resp.json(), photo_url
