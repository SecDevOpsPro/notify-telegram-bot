"""Tests for the shared Bulgarian → English helpers (notify_bot/translation.py)."""

from __future__ import annotations

import pytest

from notify_bot.translation import (
    COLOR_FORMS,
    COLORS,
    ENGINE_TYPES,
    translate,
    translate_breach,
    translate_color,
    translate_emission_class,
)

# ── translate_color ────────────────────────────────────────────────────────────


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
    assert translate_color(raw) == expected


@pytest.mark.parametrize("raw", ["нещо друго", "тъмно", "металик", "тъмно нещо", "Unknown"])
def test_translate_color_unknown_is_returned_unchanged(raw: str) -> None:
    assert translate_color(raw) == raw


@pytest.mark.parametrize("raw", [None, ""])
def test_translate_color_empty_returns_none(raw: str | None) -> None:
    assert translate_color(raw) is None


def test_color_forms_have_no_duplicate_spellings() -> None:
    forms = [form for _, group in COLOR_FORMS for form in group]
    assert len(forms) == len(set(forms)) == len(COLORS)


# ── translate (engine types) ───────────────────────────────────────────────────


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
    assert translate(raw, ENGINE_TYPES) == expected


def test_translate_empty_returns_none() -> None:
    assert translate(None, ENGINE_TYPES) is None
    assert translate("", ENGINE_TYPES) is None


# ── translate_breach ───────────────────────────────────────────────────────────


def test_translate_breach_expands_common_abbreviations() -> None:
    assert (
        translate_breach("чл. 21, ал. 2, от ЗДвП") == "Art. 21, para. 2, of the Road Traffic Act"
    )


def test_translate_breach_item_abbreviation() -> None:
    assert translate_breach("чл. 137, ал. 1, т. 2") == "Art. 137, para. 1, item 2"


def test_translate_breach_leaves_unrecognized_text_untouched() -> None:
    assert translate_breach("some other text") == "some other text"


# ── translate_emission_class ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Клас на емисиите Евро 0, Евро 1, Евро 2", "Euro 0, Euro 1, Euro 2"),
        ("Клас на емисиите: Евро 6", "Euro 6"),
        ("евро 4", "Euro 4"),
        ("Euro 5", "Euro 5"),
        (None, None),
        ("", ""),
    ],
)
def test_translate_emission_class(raw: str | None, expected: str | None) -> None:
    assert translate_emission_class(raw) == expected
