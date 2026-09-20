"""Guards for the type-level design of the service layer (notify_bot/services/).

- Functions taking two same-typed credential strings are keyword-only, so a
  swapped pair (e.g. EGN and licence number) can't slip through silently.
- Result dataclasses are frozen: they are read-only snapshots of an API reply.
"""

from __future__ import annotations

import pytest

from notify_bot.services import bgtoll, boleron, mvr, sofiatraffic

# ── Keyword-only credentials ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fn",
    [
        mvr.check_by_licence,
        mvr.check_by_plate,
        boleron.check_fines,
        boleron.check_vehicle_data,
    ],
)
@pytest.mark.asyncio
async def test_two_string_credential_functions_reject_positional_arguments(fn):
    with pytest.raises(TypeError, match="positional"):
        await fn("1234567890", "123456")


# ── Frozen result types ──────────────────────────────────────────────────────

_RESULT_TYPES = [
    bgtoll.VignetteInfo,
    boleron.GtpInfo,
    boleron.MtplInfo,
    boleron.BoleronVignetteInfo,
    boleron.FineDetail,
    boleron.VehicleData,
    boleron.FinesResult,
    mvr.Obligation,
    sofiatraffic.StickerInfo,
    sofiatraffic.ClampInfo,
]


@pytest.mark.parametrize("cls", _RESULT_TYPES, ids=lambda c: c.__name__)
def test_result_dataclasses_are_frozen_and_slotted(cls):
    params = cls.__dataclass_params__
    assert params.frozen, f"{cls.__name__} should be frozen"
    assert hasattr(cls, "__slots__"), f"{cls.__name__} should use slots"
