"""
Tests for notify_bot.services.boleron
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from notify_bot.services.boleron import (
    _COLOR_FORMS,
    _COLORS,
    _ENGINE_TYPES,
    _clean_date,
    _translate,
    _translate_color,
    check_mtpl,
    check_vehicle_data,
)

# ── _clean_date ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("15.12.2025г.", "15.12.2025"),
        ("14.12.2026г. 23:59:59", "14.12.2026"),
        ("15.04.2025 00:00:00", "15.04.2025"),
        ("2025-12-17T00:00:00.000", "17.12.2025"),
        ("2026-12-16T23:59:00.000", "16.12.2026"),
        ("2026-12-16", "16.12.2026"),
    ],
)
def test_clean_date(raw: str | None, expected: str | None) -> None:
    assert _clean_date(raw) == expected


# ── check_mtpl ─────────────────────────────────────────────────────────────────


async def test_check_mtpl_iso_dates_are_shown_without_time() -> None:
    payload = {
        "hasActiveGO": True,
        "insurer": "ЗК ЛЕВИНС АД",
        "validFrom": "2025-12-17T00:00:00.000",
        "validTo": "2026-12-16T23:59:00.000",
    }
    with patch("notify_bot.services.boleron._get", AsyncMock(return_value=payload)):
        info = await check_mtpl("CB9634OP")

    assert info.valid_from == "17.12.2025"
    assert info.valid_to == "16.12.2026"


# ── _translate_color ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # every gender / plural form of a color
        ("червен", "Red"),
        ("червена", "Red"),
        ("червено", "Red"),
        ("червени", "Red"),
        ("бяла", "White"),
        ("синя", "Blue"),
        # case and surrounding whitespace are ignored
        ("  СИВ ", "Gray"),
        ("Кафява", "Brown"),
        # shade prefixes, joined, spaced or hyphenated, on any gender form
        ("тъмносин", "Dark Blue"),
        ("тъмносиньо", "Dark Blue"),
        ("тъмно зелен", "Dark Green"),
        ("светло-сива", "Light Gray"),
        ("тъмночервен", "Dark Red"),
        # metallic suffix, alone or combined with a shade
        ("сив металик", "Gray Metallic"),
        ("тъмно син - металик", "Dark Blue Metallic"),
        # colors that used to be missing
        ("лилаво", "Purple"),
        ("розов", "Pink"),
        ("бордо", "Burgundy"),
        ("злато", "Gold"),
    ],
)
def test_translate_color_known(raw: str, expected: str) -> None:
    assert _translate_color(raw) == expected


@pytest.mark.parametrize("raw", ["нещо друго", "тъмно", "металик", "тъмно нещо", "Unknown"])
def test_translate_color_unknown_is_returned_unchanged(raw: str) -> None:
    assert _translate_color(raw) == raw


@pytest.mark.parametrize("raw", [None, ""])
def test_translate_color_empty_returns_none(raw: str | None) -> None:
    assert _translate_color(raw) is None


def test_color_forms_have_no_duplicate_spellings() -> None:
    forms = [form for _, group in _COLOR_FORMS for form in group]
    assert len(forms) == len(set(forms)) == len(_COLORS)


# ── _translate (engine types) ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Бензинов", "Petrol"),
        ("бензин", "Petrol"),
        ("ДИЗЕЛОВ", "Diesel"),
        ("хибриден", "Hybrid"),
        ("LPG", "Gas/LPG"),
        ("нещо друго", "нещо друго"),
    ],
)
def test_translate_engine_types(raw: str, expected: str) -> None:
    assert _translate(raw, _ENGINE_TYPES) == expected


def test_translate_empty_returns_none() -> None:
    assert _translate(None, _ENGINE_TYPES) is None
    assert _translate("", _ENGINE_TYPES) is None


# ── check_vehicle_data ─────────────────────────────────────────────────────────


async def test_check_vehicle_data_translates_engine_and_color() -> None:
    payload = {"engine": "Дизелов", "vehicleColorName": "тъмно сива"}
    with patch("notify_bot.services.boleron._get", AsyncMock(return_value=payload)):
        vehicle = await check_vehicle_data("CB1234AB", "123456789")

    assert vehicle.engine == "Diesel"
    assert vehicle.color == "Dark Gray"


async def test_check_vehicle_data_keeps_unknown_color_untranslated() -> None:
    payload = {"vehicleColorName": "нещо друго"}
    with patch("notify_bot.services.boleron._get", AsyncMock(return_value=payload)):
        vehicle = await check_vehicle_data("CB1234AB", "123456789")

    assert vehicle.color == "нещо друго"
