"""Tests for the shared message layout helpers (notify_bot/formatting.py)."""

from __future__ import annotations

import string

from notify_bot.formatting import _GLYPH_WIDTHS, align_fields, text_width

_SPACE = _GLYPH_WIDTHS[" "]

_VEHICLE_FIELDS = [
    ("Year", "2015"),
    ("First reg", "01.02.2015"),
    ("VIN", "<code>TMBJJ7NE0F0123456</code>"),
    ("Engine", "Diesel / 1968 cc / 110 kW"),
    ("Color", "Black"),
    ("Class", "M1"),
    ("Seats", "5"),
]


def _padding(row: str) -> int:
    rest = row.split(":", 1)[1]
    return len(rest) - len(rest.lstrip(" "))


def _value_x(row: str) -> int:
    """Estimated x-position where the value starts: width of ``label:`` + padding."""
    label = row.split(":", 1)[0]
    return text_width(f"{label}:") + _padding(row) * _SPACE


def test_glyph_table_covers_every_letter_digit_and_common_label_punctuation():
    """A new label must never hit the fallback width just because a letter is missing."""
    needed = string.ascii_letters + string.digits + " :.,-/()&'%#"

    assert [ch for ch in needed if ch not in _GLYPH_WIDTHS] == []


def test_values_start_at_the_same_x_position_despite_different_label_widths():
    positions = [_value_x(row) for row in align_fields(_VEHICLE_FIELDS)]

    assert max(positions) - min(positions) <= _SPACE


def test_new_labels_align_without_touching_the_glyph_table():
    fields = [*_VEHICLE_FIELDS, ("Owner", "Ivan"), ("Mileage", "120000"), ("Weight", "1400")]

    positions = [_value_x(row) for row in align_fields(fields)]

    assert max(positions) - min(positions) <= _SPACE


def test_shorter_labels_get_more_spaces():
    rows = dict(zip((label for label, _ in _VEHICLE_FIELDS), align_fields(_VEHICLE_FIELDS)))

    assert _padding(rows["VIN"]) > _padding(rows["First reg"]) >= 2


def test_gap_widens_the_column():
    narrow = align_fields([("Year", "2015")], gap=1)
    wide = align_fields([("Year", "2015")], gap=4)

    assert _padding(wide[0]) - _padding(narrow[0]) == 3


def test_accepts_any_iterable_and_keeps_values_verbatim():
    rows = align_fields(iter([("VIN", "<code>X1</code>"), ("Seats", "5")]))

    assert rows[0].endswith("<code>X1</code>")
    assert rows[1].endswith("5")


def test_no_fields_gives_no_rows():
    assert align_fields([]) == []
