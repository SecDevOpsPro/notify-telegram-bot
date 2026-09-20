"""
Offline Bulgarian → English translation helpers.

Lookups are dictionary/regex-only: no network calls. Anything not covered here is
returned to the caller unchanged, so a gap means Cyrillic in a message, never an error.
"""

from __future__ import annotations

import re

# ── Lookup tables ─────────────────────────────────────────────────────────────

ENGINE_TYPES: dict[str, str] = {
    "бензинов": "Petrol",
    "бензин": "Petrol",
    "дизелов": "Diesel",
    "дизел": "Diesel",
    "електрически": "Electric",
    "електро": "Electric",
    "хибрид": "Hybrid",
    "хибриден": "Hybrid",
    "газов": "Gas/LPG",
    "lpg": "Gas/LPG",
    "cng": "CNG",
}

# Bulgarian color adjectives agree with the noun, and the API isn't consistent
# about which form it returns, so each color lists (masculine, feminine, neuter, plural).
COLOR_FORMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("White", ("бял", "бяла", "бяло", "бели")),
    ("Black", ("черен", "черна", "черно", "черни")),
    ("Red", ("червен", "червена", "червено", "червени")),
    ("Blue", ("син", "синя", "синьо", "сини")),
    ("Silver", ("сребрист", "сребриста", "сребристо", "сребристи")),
    ("Gray", ("сив", "сива", "сиво", "сиви")),
    ("Green", ("зелен", "зелена", "зелено", "зелени")),
    ("Yellow", ("жълт", "жълта", "жълто", "жълти")),
    ("Brown", ("кафяв", "кафява", "кафяво", "кафяви")),
    ("Orange", ("оранжев", "оранжева", "оранжево", "оранжеви")),
    ("Violet", ("виолетов", "виолетова", "виолетово", "виолетови")),
    ("Purple", ("лилав", "лилава", "лилаво", "лилави")),
    ("Pink", ("розов", "розова", "розово", "розови")),
    ("Beige", ("бежов", "бежова", "бежово", "бежови", "беж")),
    ("Gold", ("златист", "златиста", "златисто", "златисти", "златен", "златна", "злато")),
    ("Turquoise", ("тюркоазен", "тюркоазена", "тюркоазено", "тюркоазени")),
    ("Cream", ("кремав", "кремава", "кремаво", "кремави")),
    ("Bronze", ("бронзов", "бронзова", "бронзово", "бронзови", "бронз")),
    ("Burgundy", ("бордо",)),
    ("Champagne", ("шампанско",)),
    ("Graphite", ("графит", "графитен", "графитена", "графитено", "графитени")),
    ("Cherry", ("вишнев", "вишнева", "вишнево", "вишневи")),
    ("Pearl", ("перлен", "перлена", "перлено", "перлени", "перла")),
    ("Khaki", ("каки",)),
)

COLORS: dict[str, str] = {form: english for english, forms in COLOR_FORMS for form in forms}

# "тъмно зелен", "тъмнозелен", "светло-сива" → "Dark Green", "Light Gray".
_SHADES: dict[str, str] = {"тъмно": "Dark", "светло": "Light"}
_SHADE_RE = re.compile(r"^(тъмно|светло)[\s-]*(\S.*)$")

# "сив металик", "тъмно син - металик" → "... Metallic".
_METALLIC_RE = re.compile(r"^(.+?)[\s-]+металик$")

# "Клас на емисиите Евро 0, Евро 1" → "Euro 0, Euro 1".
_EMISSION_LABEL_RE = re.compile(r"^\s*клас\s+на\s+емисиите\s*:?\s*", re.IGNORECASE)
_EURO_RE = re.compile(r"\bевро\b", re.IGNORECASE)

# (pattern, replacement) pairs applied in order.
_LEGAL_ABBR: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"от\s+ЗДвП\b", re.IGNORECASE), "of the Road Traffic Act"),
    (re.compile(r"\bчл\.\s*"), "Art. "),
    (re.compile(r"\bал\.\s*"), "para. "),
    (re.compile(r"\bт\.\s*"), "item "),
)


# ── Translators ───────────────────────────────────────────────────────────────


def translate(value: str | None, table: dict[str, str]) -> str | None:
    """Return table lookup for value (case-insensitive) or the original string."""
    if not value:
        return None
    return table.get(value.strip().lower(), value)


def translate_color(value: str | None) -> str | None:
    """Translate a Bulgarian color name, handling shade prefixes and "металик".

    Returns the original string when the base color isn't in ``COLORS``.
    """
    if not value:
        return None
    text = value.strip().lower()

    metallic = _METALLIC_RE.match(text)
    if metallic:
        text = metallic.group(1)

    shade = None
    shaded = _SHADE_RE.match(text)
    if shaded:
        shade, text = _SHADES[shaded.group(1)], shaded.group(2)

    base = COLORS.get(text)
    if base is None:
        return value

    words = [shade, base, "Metallic" if metallic else None]
    return " ".join(w for w in words if w)


def translate_breach(text: str) -> str:
    """Expand known Bulgarian legal-citation abbreviations (чл./ал./...) to English.

    Any part of ``text`` that doesn't match a known abbreviation is left unchanged.
    """
    for pattern, replacement in _LEGAL_ABBR:
        text = pattern.sub(replacement, text)
    return text


def translate_emission_class(value: str | None) -> str | None:
    """Translate the vignette API's Bulgarian emission class to English.

    "Клас на емисиите Евро 0, Евро 1, Евро 2" -> "Euro 0, Euro 1, Euro 2".
    Values that are already Latin ("Euro 5") pass through unchanged.
    """
    if not value:
        return value
    return _EURO_RE.sub("Euro", _EMISSION_LABEL_RE.sub("", value)).strip()
