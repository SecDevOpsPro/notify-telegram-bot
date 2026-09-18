"""
Tests for notify_bot.services.boleron
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from notify_bot.services.boleron import _clean_date, check_mtpl

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
