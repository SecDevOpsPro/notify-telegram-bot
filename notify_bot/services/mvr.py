"""
Async client for the Bulgarian MVR e-services Obligations API.

Two lookup modes are supported:
- By driving licence number (``check_by_licence``)
- By vehicle plate number  (``check_by_plate``)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from jinja2 import Template

from notify_bot import config

logger = logging.getLogger(__name__)

_BASE_URL = "https://e-uslugi.mvr.bg/api/Obligations/AND"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
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
    cookies = {**_COOKIES, "EAUSessionID": config.MVR_SESSION_ID}
    async with httpx.AsyncClient(
        timeout=60.0,
        trust_env=True,  # respects HTTP_PROXY / HTTPS_PROXY env vars
    ) as client:
        resp = await client.get(_BASE_URL, params=params, headers=_HEADERS, cookies=cookies)
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
        licence_number:   Driving licence number (digits only, or 2 letters + 7 digits).

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


# ── Formatting ────────────────────────────────────────────────────────────────

_OBLIGATIONS_TEMPLATE = Template(
    "{% for unit in units %}\n"
    "<b>{{ unit.unit_group_label }}</b>\n"
    "{% if unit.has_obligations %}"
    "{% for ob in unit.obligations %}  • {{ ob }}\n{% if not loop.last %}\n{% endif %}{% endfor %}"
    "{% else %}  ✅ No obligations found\n"
    "{% endif %}{% endfor %}"
)


def _iso_to_bg_date(value: str | None) -> str | None:
    """Convert an ISO ``yyyy-mm-dd[Thh:mm:ss]`` string to ``dd.mm.yyyy``."""
    if not value:
        return None
    try:
        y, m, d = value[:10].split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return value


# (pattern, replacement) pairs applied in order.
_BG_LEGAL_ABBR: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"от\s+ЗДвП\b", re.IGNORECASE), "of the Road Traffic Act"),
    (re.compile(r"\bчл\.\s*"), "Art. "),
    (re.compile(r"\bал\.\s*"), "para. "),
    (re.compile(r"\bт\.\s*"), "item "),
)


def _translate_breach(text: str) -> str:
    """Expand known Bulgarian legal-citation abbreviations (чл./ал./...) to English.

    Any part of ``text`` that doesn't match a known abbreviation is left unchanged.
    """
    for pattern, replacement in _BG_LEGAL_ABBR:
        text = pattern.sub(replacement, text)
    return text


#: Maps ``additionalData.documentType`` to a human-readable label.
_DOCUMENT_TYPE_LABELS: dict[str, str] = {
    "TICKET": "Ticket",
}


def _format_obligation(ob: Any) -> str:
    """Render one obligation as a human-readable payment summary.

    Unpaid obligations come back from the MVR API as dicts carrying the
    amount/discount, bank transfer details, and a nested ``additionalData``
    block describing the underlying document (fine number, breached
    article, vehicle, dates). Non-dict entries (plain strings, used for
    obligation types that carry no payment data) are passed through as-is.
    """
    if not isinstance(ob, dict):
        return str(ob)

    extra = ob.get("additionalData") or {}
    currency = ob.get("currency", "")
    amount = ob.get("amount")
    discount = ob.get("discountAmount")
    due_by = _iso_to_bg_date(ob.get("expirationDate"))

    lines: list[str] = []

    if amount is not None:
        lines.append(f"💰 Amount: {amount:.2f} {currency}".rstrip())
    if discount and discount != amount:
        discount_line = f"💸 With discount: {discount:.2f} {currency}".rstrip()
        if due_by:
            discount_line += f" (if paid by {due_by})"
        lines.append(discount_line)

    doc_series = extra.get("documentSeries")
    doc_number = extra.get("documentNumber")
    if doc_series or doc_number:
        label = _DOCUMENT_TYPE_LABELS.get(extra.get("documentType"), "Document")
        reference = " ".join(part for part in (doc_series, doc_number) if part)
        lines.append(f"📄 {label}: {reference}")

    breach = extra.get("breachOfOrder")
    vehicle = extra.get("vehicleNumber")
    breach_date = _iso_to_bg_date(extra.get("breachDate"))
    if vehicle:
        lines.append(f"🚗 Vehicle: {vehicle}")
    if breach:
        violation_line = f"⚖️ Violation: {_translate_breach(breach)}"
        if breach_date:
            violation_line += f" ({breach_date})"
        lines.append(violation_line)

    iban = ob.get("iban")
    if iban:
        # One labeled field per line — everything a bank transfer form asks
        # for, so there's no need to decode a compact one-liner to pay.
        fields = []
        if ob.get("bankName"):
            fields.append(("Bank", ob["bankName"]))
        fields.append(("IBAN", f"<code>{iban}</code>"))
        if ob.get("bic"):
            fields.append(("BIC", ob["bic"]))
        reason = ob.get("paymentReason")
        if reason:
            fields.append(("Reason", reason))

        label_width = max(len(label) for label, _ in fields) + 2  # ":" + 1 space
        lines.append("<b>🏦 Pay by bank transfer:</b>")
        lines.extend(f"   {(label + ':').ljust(label_width)}{value}" for label, value in fields)

    return "\n    ".join(lines) if lines else str(ob)


def render_obligations(units: list[Obligation]) -> str:
    """Render a list of Obligation groups as an HTML Telegram message body."""
    formatted_units = [
        Obligation(
            unit_group=unit.unit_group,
            unit_group_label=unit.unit_group_label,
            obligations=[_format_obligation(ob) for ob in unit.obligations],
        )
        for unit in units
    ]
    return _OBLIGATIONS_TEMPLATE.render(units=formatted_units)
