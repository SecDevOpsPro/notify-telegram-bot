"""Tests for the async SQLite database layer (notify_bot/db.py)."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from notify_bot import db

# ── Users ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_and_upsert_user(tmp_db):
    await db.init_db()
    await db.upsert_user(123, "alice", "Alice")
    user = await db.get_user(123)

    assert user is not None
    assert user["user_id"] == 123
    assert user["username"] == "alice"
    assert user["first_name"] == "Alice"
    assert user["status"] == "pending"


@pytest.mark.asyncio
async def test_upsert_user_updates_name(tmp_db):
    await db.init_db()
    await db.upsert_user(1, "old_name", "Old")
    await db.upsert_user(1, "new_name", "New")
    user = await db.get_user(1)

    assert user["username"] == "new_name"
    assert user["first_name"] == "New"
    # Status must not be reset on update
    assert user["status"] == "pending"


@pytest.mark.asyncio
async def test_get_user_returns_none_when_missing(tmp_db):
    await db.init_db()
    assert await db.get_user(99999) is None


@pytest.mark.asyncio
async def test_set_user_status(tmp_db):
    await db.init_db()
    await db.upsert_user(456, "bob", "Bob")
    created = await db.get_user(456)
    assert created["updated_at"] == created["created_at"]

    await db.set_user_status(456, "approved")
    user = await db.get_user(456)

    assert user["status"] == "approved"
    assert user["updated_at"] > user["created_at"]


@pytest.mark.asyncio
async def test_upsert_user_name_change_keeps_updated_at(tmp_db):
    await db.init_db()
    await db.upsert_user(456, "bob", "Bob")
    await db.set_user_status(456, "approved")
    approved_at = (await db.get_user(456))["updated_at"]

    await db.upsert_user(456, "bobby", "Bobby")

    assert (await db.get_user(456))["updated_at"] == approved_at


@pytest.mark.asyncio
async def test_list_users_by_status(tmp_db):
    await db.init_db()
    await db.upsert_user(1, "u1", "U1")
    await db.upsert_user(2, "u2", "U2")
    await db.upsert_user(3, "u3", "U3")
    await db.set_user_status(1, "approved")
    await db.set_user_status(3, "denied")

    approved = await db.list_users_by_status("approved")
    pending = await db.list_users_by_status("pending")
    denied = await db.list_users_by_status("denied")

    assert len(approved) == 1 and approved[0]["user_id"] == 1
    assert len(pending) == 1 and pending[0]["user_id"] == 2
    assert len(denied) == 1 and denied[0]["user_id"] == 3


# ── Profiles ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_and_get_profile(tmp_db):
    await db.init_db()
    await db.upsert_user(789, "carol", "Carol")
    await db.upsert_profile(789, national_id="1234567890", driving_licence="123456")
    profile = await db.get_profile(789)

    assert profile["national_id"] == "1234567890"
    assert profile["driving_licence"] == "123456"
    assert profile["vehicle_plate"] is None


@pytest.mark.asyncio
async def test_get_profile_returns_none_when_missing(tmp_db):
    await db.init_db()
    assert await db.get_profile(88888) is None


@pytest.mark.asyncio
async def test_partial_profile_update_does_not_overwrite(tmp_db):
    await db.init_db()
    await db.upsert_user(100, "dave", "Dave")
    await db.upsert_profile(100, national_id="1234567890")
    await db.upsert_profile(100, driving_licence="999999")
    profile = await db.get_profile(100)

    # national_id must be preserved
    assert profile["national_id"] == "1234567890"
    assert profile["driving_licence"] == "999999"


@pytest.mark.asyncio
async def test_profile_none_fields_not_overwritten(tmp_db):
    await db.init_db()
    await db.upsert_user(200, "eve", "Eve")
    await db.upsert_profile(200, national_id="0987654321")
    await db.save_vehicle(200, "PB5678CD")
    # Passing None for national_id must NOT clear the existing value
    await db.upsert_profile(200, driving_licence="111111")
    profile = await db.get_profile(200)

    assert profile["national_id"] == "0987654321"
    assert profile["vehicle_plate"] == "PB5678CD"
    assert profile["driving_licence"] == "111111"


# ── Scheduler helper ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_all_approved_with_profiles(tmp_db):
    await db.init_db()

    # User with profile and approved status
    await db.upsert_user(10, "frank", "Frank")
    await db.set_user_status(10, "approved")
    await db.upsert_profile(10, national_id="1111111111", driving_licence="111111")

    # User with profile but still pending
    await db.upsert_user(20, "grace", "Grace")
    await db.upsert_profile(20, national_id="2222222222")

    # Approved user but no profile
    await db.upsert_user(30, "hal", "Hal")
    await db.set_user_status(30, "approved")

    rows = await db.get_all_approved_with_profiles()

    assert len(rows) == 1
    assert rows[0]["user_id"] == 10


@pytest.mark.asyncio
async def test_concurrent_profile_upserts_merge_fields(tmp_db):
    """Concurrent partial upserts for the same user must not corrupt data."""
    await db.init_db()
    await db.upsert_user(42, "user42", "User 42")

    await asyncio.gather(
        db.upsert_profile(42, national_id="1111111111"),
        db.upsert_profile(42, driving_licence="222222"),
        db.save_vehicle(42, "CB1234AB"),
        db.get_profile(42),
    )

    profile = await db.get_profile(42)
    assert profile is not None
    assert profile["national_id"] == "1111111111"
    assert profile["driving_licence"] == "222222"
    assert profile["vehicle_plate"] == "CB1234AB"


@pytest.mark.asyncio
async def test_concurrent_mixed_operations_do_not_raise(tmp_db):
    """Concurrent reads, writes, and deletes must complete without error."""
    await db.init_db()
    await db.upsert_user(99, "user99", "User 99")
    await db.upsert_profile(99, national_id="9999999999")

    await asyncio.gather(
        db.get_profile(99),
        db.save_vehicle(99, "CB1234AB", "123456"),
        db.get_user(99),
        db.list_users_by_status("pending"),
    )

    profile = await db.get_profile(99)
    assert profile is not None
    assert profile["national_id"] == "9999999999"
    vehicle = await db.get_vehicle(99, "CB1234AB")
    assert vehicle is not None and vehicle["talon_no"] == "123456"


@pytest.mark.asyncio
async def test_init_db_enables_wal_mode(tmp_db):
    await db.init_db()

    async with aiosqlite.connect(tmp_db) as conn:
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()

    assert row is not None
    assert row[0].lower() == "wal"


@pytest.mark.asyncio
async def test_init_db_creates_missing_parent_directory(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "state" / "bot.db"
    monkeypatch.setattr(db, "DATABASE_PATH", str(db_path))

    await db.init_db()

    assert db_path.exists()
    assert db_path.parent.exists()


# ── Vehicles ──────────────────────────────────────────────────────────────────


async def _user_with_vehicles(user_id: int, *plates: str) -> None:
    await db.upsert_user(user_id, f"u{user_id}", f"U{user_id}")
    for plate in plates:
        await db.save_vehicle(user_id, plate, f"00{user_id}000")


async def _plates(user_id: int) -> list[str]:
    return [v["plate"] for v in await db.list_vehicles(user_id)]


async def _preferred(user_id: int) -> str | None:
    profile = await db.get_profile(user_id)
    return profile["vehicle_plate"] if profile else None


@pytest.mark.asyncio
async def test_first_vehicle_becomes_preferred_and_later_ones_do_not(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "cb1234ab", "PB5678CD")

    assert await _preferred(1) == "CB1234AB"
    assert await _plates(1) == ["CB1234AB", "PB5678CD"]


@pytest.mark.asyncio
async def test_list_vehicles_puts_preferred_first(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB", "PB5678CD", "CA1111AA")

    assert await db.set_preferred_vehicle(1, "ca1111aa") is True
    assert await _plates(1) == ["CA1111AA", "CB1234AB", "PB5678CD"]


@pytest.mark.asyncio
async def test_save_vehicle_make_preferred(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB")
    await db.save_vehicle(1, "PB5678CD", make_preferred=True)

    assert await _preferred(1) == "PB5678CD"


@pytest.mark.asyncio
async def test_save_existing_vehicle_updates_talon_only_when_given(tmp_db):
    await db.init_db()
    await db.upsert_user(1, "u", "U")
    await db.save_vehicle(1, "CB1234AB", "111111")
    await db.save_vehicle(1, "CB1234AB")
    assert (await db.get_vehicle(1, "CB1234AB"))["talon_no"] == "111111"

    await db.save_vehicle(1, "cb1234ab", "222222")
    assert (await db.get_vehicle(1, "CB1234AB"))["talon_no"] == "222222"
    assert await _plates(1) == ["CB1234AB"]


@pytest.mark.asyncio
async def test_save_vehicle_enforces_limit_but_allows_updates(tmp_db):
    await db.init_db()
    plates = [f"CB{1000 + i}AB" for i in range(db.MAX_VEHICLES)]
    await _user_with_vehicles(1, *plates)

    with pytest.raises(db.VehicleLimitError):
        await db.save_vehicle(1, "PB5678CD")
    await db.save_vehicle(1, plates[0], "999999")  # an existing vehicle is still editable

    assert len(await db.list_vehicles(1)) == db.MAX_VEHICLES


@pytest.mark.asyncio
async def test_vehicles_are_per_user(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB")
    await _user_with_vehicles(2, "CB1234AB")

    assert await db.delete_vehicle(1, "CB1234AB") is True
    assert await _plates(2) == ["CB1234AB"]
    assert await db.set_preferred_vehicle(1, "CB1234AB") is False


@pytest.mark.asyncio
async def test_deleting_preferred_vehicle_passes_role_to_oldest_remaining(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB", "PB5678CD", "CA1111AA")
    await db.set_preferred_vehicle(1, "PB5678CD")

    assert await db.delete_vehicle(1, "PB5678CD") is True
    assert await _preferred(1) == "CB1234AB"


@pytest.mark.asyncio
async def test_deleting_other_vehicle_keeps_preferred(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB", "PB5678CD")

    await db.delete_vehicle(1, "PB5678CD")
    assert await _preferred(1) == "CB1234AB"


@pytest.mark.asyncio
async def test_deleting_last_vehicle_clears_preferred(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB")

    assert await db.delete_vehicle(1, "CB1234AB") is True
    assert await _preferred(1) is None
    assert await db.delete_vehicle(1, "CB1234AB") is False


@pytest.mark.asyncio
async def test_delete_profile_removes_vehicles(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB", "PB5678CD")

    await db.delete_profile(1)

    assert await db.list_vehicles(1) == []
    assert await db.get_profile(1) is None


@pytest.mark.asyncio
async def test_report_targets_include_vehicles_preferred_first(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB", "PB5678CD")
    await db.set_user_status(1, "approved")
    await db.set_preferred_vehicle(1, "PB5678CD")
    # Approved, only a vehicle saved — still gets a report.
    await _user_with_vehicles(2, "CA1111AA")
    await db.set_user_status(2, "approved")
    # Pending — never reported.
    await _user_with_vehicles(3, "CA2222AA")

    targets = await db.get_all_approved_with_profiles()

    assert [t["user_id"] for t in targets] == [1, 2]
    assert [v["plate"] for v in targets[0]["vehicles"]] == ["PB5678CD", "CB1234AB"]
    assert [v["plate"] for v in targets[1]["vehicles"]] == ["CA1111AA"]
    assert await db.get_report_target(2) == targets[1]
    assert await db.get_report_target(3) is None


@pytest.mark.asyncio
async def test_approved_user_whose_vehicles_were_all_removed_gets_no_report(tmp_db):
    await db.init_db()
    await _user_with_vehicles(1, "CB1234AB")
    await db.set_user_status(1, "approved")
    await db.delete_vehicle(1, "CB1234AB")

    assert await db.get_report_target(1) is None


@pytest.mark.asyncio
async def test_profile_update_keeps_created_at(tmp_db):
    await db.init_db()
    await db.upsert_user(1, "u", "U")
    await db.upsert_profile(1, national_id="1111111111")
    created = await db.get_profile(1)

    await db.upsert_profile(1, driving_licence="222222")
    await db.save_vehicle(1, "CB1234AB")  # sets the main vehicle on the profile

    profile = await db.get_profile(1)
    assert profile["created_at"] == created["created_at"]
    assert profile["updated_at"] > created["updated_at"]


@pytest.mark.asyncio
async def test_vehicle_talon_update_bumps_updated_at_only(tmp_db):
    await db.init_db()
    await db.upsert_user(1, "u", "U")
    await db.save_vehicle(1, "CB1234AB", "111111")
    created = await db.get_vehicle(1, "CB1234AB")
    assert created["updated_at"] == created["created_at"]

    await db.save_vehicle(1, "CB1234AB", "222222")

    vehicle = await db.get_vehicle(1, "CB1234AB")
    assert vehicle["created_at"] == created["created_at"]
    assert vehicle["updated_at"] > created["updated_at"]
