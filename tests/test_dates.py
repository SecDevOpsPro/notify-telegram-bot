"""
Tests for notify_bot.dates
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from notify_bot.dates import (
    EXPIRY_WARN_DAYS,
    days_until,
    expiry_warning,
    format_date,
    parse_datetime,
)


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


# ── days_until / expiry_warning ──────────────────────────────────────────────


def _in(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%d.%m.%Y")


def test_days_until_none_when_no_date():
    assert days_until(None) is None


def test_days_until_bg_format():
    assert days_until(_in(5)) == 5


def test_days_until_iso_format():
    assert days_until((date.today() + timedelta(days=3)).isoformat()) == 3


def test_days_until_negative_for_past_date():
    assert days_until(_in(-2)) == -2


def test_days_until_none_when_unparseable():
    assert days_until("not-a-date") is None


def test_expiry_warning_within_threshold():
    assert expiry_warning(_in(1)) == "⚠️ Expires in 1 day!"
    assert expiry_warning(_in(3)) == "⚠️ Expires in 3 days!"
    # Not rounded up to "in 2 weeks" just under the threshold.
    assert expiry_warning(_in(EXPIRY_WARN_DAYS - 1)) == f"⚠️ Expires in {EXPIRY_WARN_DAYS - 1} days!"


def test_expiry_warning_on_last_day():
    assert expiry_warning(_in(0)) == "⚠️ Expires today!"


def test_expiry_warning_none_outside_threshold():
    assert expiry_warning(_in(EXPIRY_WARN_DAYS)) is None
    assert expiry_warning(_in(-1)) is None
    assert expiry_warning(None) is None
