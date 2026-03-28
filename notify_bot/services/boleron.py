"""
Async client for the Boleron.bg vehicle-data APIs.

The API is protected by Firebase anonymous authentication (project boleron-50414).
A token is obtained once and cached until near-expiry (~1 hour TTL), then refreshed.

Endpoints used:
  GET /boleron/external/gtp?carNo=<plate>           — technical inspection validity
  GET /boleron/external/goAutoService?carNo=<plate> — MTPL civil liability insurance
  GET /boleron/external/vignette?carNo=<plate>      — road e-vignette
  GET /boleron/external/fines?driverLicenseNo=<l>&egn=<e> — traffic fines
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from notify_bot import config

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

_FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signUp?key="
    + config.BOLERON_FIREBASE_API_KEY
)
_API_BASE = "https://api.boleron.bg/boleron/external"
_CARUTILS_BASE = "https://api.boleron.bg/boleron/carutils"

# ── Translation maps (Cyrillic → Latin) ──────────────────────────────────────

_ENGINE_TYPES: dict[str, str] = {
    "бензинов": "Petrol",
    "дизелов": "Diesel",
    "електрически": "Electric",
    "хибрид": "Hybrid",
    "газов": "Gas/LPG",
    "lpg": "Gas/LPG",
    "cng": "CNG",
}

_COLORS: dict[str, str] = {
    "бял": "White",
    "бяло": "White",
    "черен": "Black",
    "черно": "Black",
    "червен": "Red",
    "червено": "Red",
    "син": "Blue",
    "синьо": "Blue",
    "тъмносин": "Dark Blue",
    "тъмносиньо": "Dark Blue",
    "сребрист": "Silver",
    "сребристо": "Silver",
    "сив": "Gray",
    "сиво": "Gray",
    "зелен": "Green",
    "зелено": "Green",
    "жълт": "Yellow",
    "жълто": "Yellow",
    "кафяв": "Brown",
    "кафяво": "Brown",
    "оранжев": "Orange",
    "оранжево": "Orange",
    "виолетов": "Violet",
    "виолетово": "Violet",
    "бежов": "Beige",
    "бежово": "Beige",
    "тъмночервен": "Dark Red",
    "тъмночервено": "Dark Red",
    "злато": "Gold",
    "златист": "Gold",
}


def _translate(value: str | None, table: dict[str, str]) -> str | None:
    """Return table lookup for value (case-insensitive) or the original string."""
    if not value:
        return None
    return table.get(value.strip().lower(), value)

_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en,es;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "DNT": "1",
    "Origin": "https://www.boleron.bg",
    "Pragma": "no-cache",
    "Referer": "https://www.boleron.bg/en/fine-check-result/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}

# ── Token cache ───────────────────────────────────────────────────────────────

_token: str | None = None
_token_expires_at: float = 0.0
_TOKEN_REFRESH_BUFFER = 120.0  # refresh 2 min before actual expiry

# ── Exceptions ────────────────────────────────────────────────────────────────


class BoleronError(Exception):
    """Base exception for boleron.bg API errors."""


class BoleronNotFoundError(BoleronError):
    """Raised when the API returns 500 (vehicle not found in boleron database)."""


# ── Auth ──────────────────────────────────────────────────────────────────────


async def _get_token() -> str:
    """Return a valid Firebase anonymous bearer token, refreshing if expired."""
    global _token, _token_expires_at

    if _token and time.monotonic() < _token_expires_at - _TOKEN_REFRESH_BUFFER:
        return _token

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _FIREBASE_SIGN_IN_URL,
            json={"returnSecureToken": True},
        )

    if resp.status_code != 200:
        raise BoleronError(f"Firebase sign-in failed: {resp.status_code}")

    data = resp.json()
    _token = data["idToken"]
    expires_in = int(data.get("expiresIn", 3600))
    _token_expires_at = time.monotonic() + expires_in
    logger.debug("Boleron Firebase token refreshed (expires in %ds)", expires_in)
    return _token


async def _get(path: str, params: dict, *, base: str = _API_BASE) -> dict:
    """GET `base/path` with auto-refreshed bearer auth. Returns parsed JSON."""
    token = await _get_token()
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=20.0) as client:
            resp = await client.get(
                f"{base}/{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.RequestError as exc:
        raise BoleronError(f"Request error: {exc}") from exc

    if resp.status_code == 500:
        raise BoleronNotFoundError(f"Vehicle not found in boleron database ({path})")
    if resp.status_code != 200:
        raise BoleronError(f"HTTP {resp.status_code} from {path}")

    try:
        return resp.json()
    except Exception as exc:
        raise BoleronError(f"Non-JSON response from {path}") from exc


# ── Helpers ───────────────────────────────────────────────────────────────────

_TIME_SUFFIX_RE = re.compile(r"\s+\d{1,2}:\d{2}:\d{2}[^\d]*$")
_NON_DIGIT_TAIL_RE = re.compile(r"[^\d]+$")


def _clean_date(value: str | None) -> str | None:
    """Return just the date portion, stripping Bulgarian suffixes and time.

    Handles:
      "15.12.2025г."         → "15.12.2025"
      "14.12.2026г. 23:59:59" → "14.12.2026"
      "15.04.2025 00:00:00"  → "15.04.2025"
    """
    if not value:
        return None
    cleaned = value.strip()
    cleaned = _TIME_SUFFIX_RE.sub("", cleaned).strip()   # drop HH:MM:SS
    cleaned = _NON_DIGIT_TAIL_RE.sub("", cleaned).strip()  # drop г., ч., …
    return cleaned or None


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class GtpInfo:
    """Technical inspection (ГТП) result."""

    found: bool
    valid_to: str | None = None  # formatted, e.g. "08.04.2026"


@dataclass
class MtplInfo:
    """Motor Third Party Liability (Гражданска отговорност) result."""

    active: bool
    insurer: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


@dataclass
class BoleronVignetteInfo:
    """Vignette result from boleron.bg API."""

    found: bool
    active: bool = False
    vignette_id: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    price: str | None = None
    validity_type: str | None = None


@dataclass
class FineDetail:
    """A single fine entry."""

    anpp_number: str | None = None
    description: str | None = None
    amount: float = 0.0
    discount_amount: float = 0.0
    is_served: bool = False


@dataclass
class VehicleData:
    """Vehicle registration data from vehicleDataServices."""

    car_no: str
    talon_no: str
    make: str | None = None
    model: str | None = None
    make_model: str | None = None
    first_reg_date: str | None = None
    build_year: int | None = None
    vin: str | None = None
    engine: str | None = None          # translated to Latin
    engine_cc: int | None = None
    power_kw: int | None = None
    color: str | None = None           # translated to Latin
    vehicle_class: str | None = None
    seats: int | None = None
    leasing: bool = False


@dataclass
class FinesResult:
    """Traffic fines check result."""

    has_fines: bool
    count: int
    total: float
    total_discount: float
    currency_symbol: str = "€"
    details: list[FineDetail] = field(default_factory=list)


# ── Public API calls ──────────────────────────────────────────────────────────


async def check_gtp(car_no: str) -> GtpInfo:
    """Check technical inspection validity for `car_no`."""
    data = await _get("gtp", {"carNo": car_no})
    if not data.get("result"):
        return GtpInfo(found=False)
    return GtpInfo(
        found=True,
        valid_to=_clean_date(data.get("validToFormated") or data.get("validTo")),
    )


async def check_mtpl(car_no: str) -> MtplInfo:
    """Check active civil liability (MTPL) insurance for `car_no`."""
    data = await _get("goAutoService", {"carNo": car_no})
    active: bool = bool(data.get("hasActiveGO"))
    return MtplInfo(
        active=active,
        insurer=data.get("insurer", "").strip() or None,
        valid_from=_clean_date(data.get("validFromFormated") or data.get("validFrom")),
        valid_to=_clean_date(data.get("validToFormated") or data.get("validTo")),
    )


async def check_vignette_boleron(car_no: str) -> BoleronVignetteInfo:
    """Check road e-vignette via boleron.bg for `car_no`."""
    data = await _get("vignette", {"carNo": car_no})
    if not data:
        return BoleronVignetteInfo(found=False)
    status_raw = (data.get("vignetteStatus") or "").lower()
    active = "актив" in status_raw or status_raw == "active"
    return BoleronVignetteInfo(
        found=True,
        active=active,
        vignette_id=data.get("vignetteId"),
        valid_from=data.get("validityStartFormatted", "").split(" ")[0] or None,
        valid_to=data.get("validityEndFormatted", "").split(" ")[0] or None,
        price=data.get("vignettePrice"),
        validity_type=data.get("validityType"),
    )


async def check_vehicle_data(car_no: str, talon_no: str) -> VehicleData:
    """Fetch vehicle registration data using plate + talon (small registration card) number."""
    data = await _get(
        "vehicleDataServices",
        {"carNo": car_no, "talonNo": talon_no},
        base=_CARUTILS_BASE,
    )
    return VehicleData(
        car_no=data.get("carNo", car_no),
        talon_no=data.get("talonNo", talon_no),
        make=data.get("make"),
        model=data.get("model"),
        make_model=data.get("makeModel"),
        first_reg_date=data.get("firstRegDate"),
        build_year=data.get("buildYear"),
        vin=data.get("vin"),
        engine=_translate(data.get("engine"), _ENGINE_TYPES),
        engine_cc=data.get("engineDisplacement") or data.get("engineVolume"),
        power_kw=data.get("power"),
        color=_translate(data.get("vehicleColorName"), _COLORS),
        vehicle_class=data.get("vehicleClass"),
        seats=data.get("seatsDetail"),
        leasing=bool(data.get("leasing")),
    )


async def check_fines(driver_licence_no: str, egn: str) -> FinesResult:
    """Check traffic fines for the given driver licence + EGN."""
    data = await _get("fines", {"driverLicenseNo": driver_licence_no, "egn": egn})
    count: int = int(data.get("countFines", 0))
    details: list[FineDetail] = []
    for raw in data.get("finesDetails") or []:
        details.append(
            FineDetail(
                anpp_number=raw.get("ANPPNumber"),
                description=raw.get("description"),
                amount=float(raw.get("amount", 0)),
                discount_amount=float(raw.get("discountAmount", 0)),
                is_served=bool(raw.get("isServed")),
            )
        )
    return FinesResult(
        has_fines=count > 0,
        count=count,
        total=float(data.get("sumFines", 0)),
        total_discount=float(data.get("sumFinesDiscount", 0)),
        currency_symbol=data.get("currencySymbol", "€"),
        details=details,
    )
