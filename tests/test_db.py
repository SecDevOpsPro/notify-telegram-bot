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
    await db.set_user_status(456, "approved")
    user = await db.get_user(456)

    assert user["status"] == "approved"


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
    await db.upsert_profile(
        789,
        national_id="1234567890",
        driving_licence="123456",
        vehicle_plate="CB1234AB",
    )
    profile = await db.get_profile(789)

    assert profile["national_id"] == "1234567890"
    assert profile["driving_licence"] == "123456"
    assert profile["vehicle_plate"] == "CB1234AB"


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
    await db.upsert_profile(200, national_id="0987654321", vehicle_plate="PB5678CD")
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
        db.upsert_profile(42, vehicle_plate="CB1234AB"),
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
        db.upsert_profile(99, talon_no="123456"),
        db.get_user(99),
        db.list_users_by_status("pending"),
    )

    profile = await db.get_profile(99)
    assert profile is not None
    assert profile["national_id"] == "9999999999"
    assert profile["talon_no"] == "123456"


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
