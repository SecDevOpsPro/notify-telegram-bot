"""The daily report's checks: their names, labels, and which ones apply to a user.

Shared by ``db`` (opt-outs), ``scheduler.jobs`` (the report and its "Hidden" footer)
and ``handlers.alerts`` (the toggles), so all three agree on what a user can switch off.
"""

from __future__ import annotations

from typing import Literal

ReportCheck = Literal["fines", "licence", "plate", "gtp", "mtpl", "vignette", "sticker", "clamp"]

#: The opt-out scope of the personal checks; a vehicle's scope is its plate.
PERSONAL_SCOPE = ""

#: Display names, in the order the report runs the checks.
LABELS: dict[ReportCheck, str] = {
    "fines": "Fines",
    "licence": "Licence",
    "plate": "MVR plate",
    "gtp": "Inspection",
    "mtpl": "MTPL",
    "vignette": "Vignette",
    "sticker": "Sticker",
    "clamp": "Clamp",
}


def personal_checks(national_id: str | None, licence: str | None) -> tuple[ReportCheck, ...]:
    """The personal checks a user's data allows — both need the ID and the licence."""
    return ("fines", "licence") if national_id and licence else ()


def vehicle_checks(national_id: str | None) -> tuple[ReportCheck, ...]:
    """The checks that apply to each vehicle — only the MVR plate check needs the ID."""
    plate: tuple[ReportCheck, ...] = ("plate",) if national_id else ()
    return (*plate, "gtp", "mtpl", "vignette", "sticker", "clamp")
