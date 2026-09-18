"""
Tests for notify_bot.dates
"""

from __future__ import annotations

from datetime import datetime

import pytest

from notify_bot.dates import format_date, parse_datetime


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07.06.2026 00:00:00", datetime(2026, 6, 7)),
        ("07.06.2026 23:59", datetime(2026, 6, 7, 23, 59)),
        ("07.06.2026", datetime(2026, 6, 7)),
        ("15.12.2025г.", datetime(2025, 12, 15)),
        ("14.12.2026г. 23:59:59", datetime(2026, 12, 14, 23, 59, 59)),
        ("2025-12-17T00:00:00.000", datetime(2025, 12, 17)),
        ("2026-12-16T23:59:00.000", datetime(2026, 12, 16, 23, 59)),
        ("2026-12-16 23:59:59", datetime(2026, 12, 16, 23, 59, 59)),
        ("2026-01-01", datetime(2026, 1, 1)),
    ],
)
def test_parse_datetime(raw: str, expected: datetime) -> None:
    assert parse_datetime(raw) == expected


def test_parse_datetime_drops_timezone() -> None:
    parsed = parse_datetime("2026-01-01T10:00:00Z")
    assert parsed == datetime(2026, 1, 1, 10)
    assert parsed is not None and parsed.tzinfo is None


@pytest.mark.parametrize("raw", [None, "", "not-a-date", "31.02.2026", "2025-99-99"])
def test_parse_datetime_unrecognized(raw: str | None) -> None:
    assert parse_datetime(raw) is None


def test_format_date() -> None:
    assert format_date(datetime(2026, 6, 7, 23, 59)) == "07.06.2026"
