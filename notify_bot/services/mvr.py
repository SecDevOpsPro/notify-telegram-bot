"""
Async client for the Bulgarian MVR e-services Obligations API.

Two lookup modes are supported:
- By driving licence number (``check_by_licence``)
- By vehicle plate number  (``check_by_plate``)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
from jinja2 import Template

from notify_bot import config
from notify_bot.dates import parse_datetime
from notify_bot.translation import translate_breach

logger = logging.getLogger(__name__)

_BASE_URL = "https://e-uslugi.mvr.bg/api/Obligations/AND"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
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


#: One entry of an obligation group as the MVR API returns it: a dict carrying
#: payment data, or a plain string for obligation types with no payment data.
type RawObligation = dict[str, Any] | str


@dataclass(frozen=True, slots=True)
class Obligation:
    """A single obligation group returned by the MVR API."""

    unit_group: int
    unit_group_label: str
    obligations: list[RawObligation] = field(default_factory=list)

    @property
    def has_obligations(self) -> bool:
        return bool(self.obligations)


@dataclass(frozen=True, slots=True)
class _RenderedGroup:
    """An :class:`Obligation` group whose entries are already formatted for display.

    Kept separate from :class:`Obligation` so raw API entries and display strings
    can't be mixed up — ``_OBLIGATIONS_TEMPLATE`` only ever sees this type.
    """

    unit_group_label: str
    obligations: list[str]

    @property
    def has_obligations(self) -> bool:
        return bool(self.obligations)


# ── Exceptions ───────────────────────────────────────────────────────────────


class MVRApiError(Exception):
    """Raised when the MVR API returns an unexpected response or HTTP error."""


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _fetch(params: dict[str, str]) -> dict[str, Any]:
    cookies = {**_COOKIES, "EAUSessionID": config.MVR_SESSION_ID}
    async with httpx.AsyncClient(
        timeout=60.0,
        trust_env=True,  # respects HTTP_PROXY / HTTPS_PROXY env vars
    ) as client:
        resp = await client.get(_BASE_URL, params=params, headers=_HEADERS, cookies=cookies)
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        return payload


def _parse(data: dict[str, Any]) -> list[Obligation]:
    result: list[Obligation] = []
    for unit in data.get("obligationsData", []):
        ug: int = unit.get("unitGroup", 0)
        label = LAW_MAP.get(ug, f"Obligation group {ug}")
        obligations = unit.get("obligations", [])
        result.append(Obligation(unit_group=ug, unit_group_label=label, obligations=obligations))
    return result


# ── Public API ────────────────────────────────────────────────────────────────


async def check_by_licence(*, national_id: str, licence_number: str) -> list[Obligation]:
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


async def check_by_plate(*, national_id: str, plate_number: str) -> list[Obligation]:
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

#: Title line of every obligations-check message (/driver, /plate).
_CHECK_TITLE = "<b>🔎 Obligations check</b>\n"

_OBLIGATIONS_TEMPLATE: Template = Template(
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


#: Maps ``additionalData.documentType`` to a human-readable label.
_DOCUMENT_TYPE_LABELS: dict[str, str] = {
    "TICKET": "Ticket",
}


def _document_reference(extra: dict[str, Any]) -> str:
    """The fine/document identifier, e.g. ``K 13247956`` — empty if the API sent none."""
    return " ".join(
        part for part in (extra.get("documentSeries"), extra.get("documentNumber")) if part
    )


def _discount_expired(ob: dict[str, Any]) -> bool:
    """True once the last day the discounted amount can be paid (``expirationDate``) is past.

    An obligation without a usable ``expirationDate`` is treated as still discounted:
    better to offer a discount the bank may reject than to hide one that still applies.
    """
    due = parse_datetime(ob.get("expirationDate"))
    return due is not None and due.date() < date.today()


@dataclass(frozen=True, slots=True)
class PaymentDetails:
    """The values a bank transfer form asks for, for one unpaid obligation."""

    iban: str
    reference: str  # fine/document number; empty when the API sent none
    bic: str | None
    reason: str | None
    amount: float | None
    discount: float | None  # only while it still applies and differs from ``amount``


def payment_details(ob: RawObligation) -> PaymentDetails | None:
    """Extract the bank-transfer values from *ob*, or ``None`` if it can't be paid by bank.

    Mirrors what :func:`_format_obligation` prints under "Pay by bank transfer",
    so the tap-to-copy buttons offer exactly the values shown in the message.
    """
    if not isinstance(ob, dict) or not ob.get("iban"):
        return None
    amount = ob.get("amount")
    discount = ob.get("discountAmount")
    usable_discount = bool(discount) and discount != amount and not _discount_expired(ob)
    return PaymentDetails(
        iban=ob["iban"],
        reference=_document_reference(ob.get("additionalData") or {}),
        bic=ob.get("bic") or None,
        reason=ob.get("paymentReason") or None,
        amount=amount,
        discount=discount if usable_discount else None,
    )


def _obligation_lines(ob: RawObligation) -> list[str]:
    """One obligation as a human-readable payment summary, one display line per entry.

    Unpaid obligations come back from the MVR API as dicts carrying the
    amount/discount, bank transfer details, and a nested ``additionalData``
    block describing the underlying document (fine number, breached
    article, vehicle, dates). Non-dict entries (plain strings, used for
    obligation types that carry no payment data) are passed through as-is.
    """
    if not isinstance(ob, dict):
        return [str(ob)]

    extra = ob.get("additionalData") or {}
    currency = ob.get("currency", "")
    amount = ob.get("amount")
    discount = ob.get("discountAmount")
    due_by = _iso_to_bg_date(ob.get("expirationDate"))

    lines: list[str] = []

    if amount is not None:
        lines.append(f"💰 Amount: {amount:.2f} {currency}".rstrip())
    if discount and discount != amount:
        discount_text = f"With discount: {discount:.2f} {currency}".rstrip()
        if _discount_expired(ob):
            # Still shown, struck through, so it's clear the reduced amount can't be paid anymore.
            lines.append(f"💸 <s>{discount_text}</s> (expired {due_by})")
        elif due_by:
            lines.append(f"💸 {discount_text} (if paid by {due_by})")
        else:
            lines.append(f"💸 {discount_text}")

    reference = _document_reference(extra)
    if reference:
        label = _DOCUMENT_TYPE_LABELS.get(extra.get("documentType") or "", "Document")
        lines.append(f"📄 {label}: {reference}")

    breach = extra.get("breachOfOrder")
    vehicle = extra.get("vehicleNumber")
    breach_date = _iso_to_bg_date(extra.get("breachDate"))
    if vehicle:
        lines.append(f"🚗 Vehicle: {vehicle}")
    if breach:
        violation_line = f"⚖️ Violation: {translate_breach(breach)}"
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

    return lines or [str(ob)]


def _format_obligation(ob: RawObligation) -> str:
    """One obligation as a bullet entry for :func:`render_obligations` (lines indented)."""
    return "\n    ".join(_obligation_lines(ob))


def render_obligations(units: list[Obligation]) -> str:
    """Render a list of Obligation groups as an HTML Telegram message body."""
    formatted_units = [
        _RenderedGroup(
            unit_group_label=unit.unit_group_label,
            obligations=[_format_obligation(ob) for ob in unit.obligations],
        )
        for unit in units
    ]
    return _OBLIGATIONS_TEMPLATE.render(units=formatted_units)


@dataclass(frozen=True, slots=True)
class FineMessage:
    """One chat message of an obligations check."""

    text: str
    payment: PaymentDetails | None = None  # set when the message is a payable fine


def render_fine_messages(units: list[Obligation]) -> list[FineMessage]:
    """Render an obligations check as the chat messages to send, in order.

    With at most one fine payable by bank this is a single message. With several,
    each gets its own message so its bank details — and the tap-to-copy buttons the
    caller attaches from :attr:`FineMessage.payment` — sit right under it, behind a
    summary message that carries everything else.
    """
    analysed = [[(ob, payment_details(ob)) for ob in unit.obligations] for unit in units]
    fines = [(ob, pay) for group in analysed for ob, pay in group if pay]

    if len(fines) <= 1:
        text = _CHECK_TITLE + render_obligations(units)
        return [FineMessage(text, fines[0][1] if fines else None)]

    summary = [
        _RenderedGroup(
            unit_group_label=unit.unit_group_label,
            obligations=[
                *(_format_obligation(ob) for ob, pay in group if not pay),
                *(
                    [f"💳 {payable} unpaid fine{'s' if payable != 1 else ''} — sent below"]
                    if (payable := sum(1 for _, pay in group if pay))
                    else []
                ),
            ],
        )
        for unit, group in zip(units, analysed, strict=True)
    ]
    messages = [FineMessage(_CHECK_TITLE + _OBLIGATIONS_TEMPLATE.render(units=summary))]
    for n, (ob, pay) in enumerate(fines, start=1):
        title = f"Fine {n} of {len(fines)}" + (f" · {pay.reference}" if pay.reference else "")
        messages.append(FineMessage("\n".join([f"<b>{title}</b>", *_obligation_lines(ob)]), pay))
    return messages
