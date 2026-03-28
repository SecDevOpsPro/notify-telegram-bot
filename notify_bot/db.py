"""Async SQLite database layer — users and per-user profiles."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

# Allow tests to override DATABASE_PATH via environment variable.
DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "/app/data/bot.db")

# ── Schema ───────────────────────────────────────────────────────────────────

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL
)
"""

_CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id          INTEGER PRIMARY KEY REFERENCES users(user_id),
    national_id      TEXT,
    driving_licence  TEXT,
    vehicle_plate    TEXT,
    talon_no         TEXT,
    updated_at       TEXT NOT NULL
)
"""

_ADD_TALON_COLUMN = """
ALTER TABLE user_profiles ADD COLUMN talon_no TEXT
"""

# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    """Re-read at call time so tests can monkeypatch the module attribute."""
    import notify_bot.db as _self

    return _self.DATABASE_PATH


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def init_db() -> None:
    """Create tables on first run."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(_CREATE_USERS)
        await db.execute(_CREATE_PROFILES)
        # Migrate: add talon_no if it doesn't exist yet (idempotent)
        try:
            await db.execute(_ADD_TALON_COLUMN)
        except Exception:
            pass  # Column already exists
        await db.commit()


# ── Users ─────────────────────────────────────────────────────────────────────


async def upsert_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
) -> None:
    """Insert a new user with status=pending, or update name/username if they exist."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, username, first_name, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name, _now()),
        )
        await db.commit()


async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_user_status(user_id: int, status: str) -> None:
    """Update a user's approval status ('pending' | 'approved' | 'denied')."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
        await db.commit()


async def list_users_by_status(status: str) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE status = ?", (status,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Profiles ──────────────────────────────────────────────────────────────────


async def get_profile(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_profile(
    user_id: int,
    *,
    national_id: str | None = None,
    driving_licence: str | None = None,
    vehicle_plate: str | None = None,
    talon_no: str | None = None,
) -> None:
    """
    Insert or partially update a user profile.
    Only non-None arguments overwrite existing values.

    Uses a single atomic INSERT ... ON CONFLICT so concurrent calls for the
    same user_id never race on a read-then-write.
    COALESCE keeps the existing column value when the argument is None.
    """
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO user_profiles
                (user_id, national_id, driving_licence, vehicle_plate, talon_no, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                national_id     = COALESCE(excluded.national_id,     national_id),
                driving_licence = COALESCE(excluded.driving_licence, driving_licence),
                vehicle_plate   = COALESCE(excluded.vehicle_plate,   vehicle_plate),
                talon_no        = COALESCE(excluded.talon_no,        talon_no),
                updated_at      = excluded.updated_at
            """,
            (user_id, national_id, driving_licence, vehicle_plate, talon_no, _now()),
        )
        await db.commit()


async def delete_profile(user_id: int) -> None:
    """Remove a user's saved profile (national_id, licence, plate)."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
        await db.commit()


# ── Scheduler helpers ─────────────────────────────────────────────────────────


async def get_all_approved_with_profiles() -> list[dict]:
    """Return approved users who have at least one profile field populated."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                u.user_id,
                u.first_name,
                p.national_id,
                p.driving_licence,
                p.vehicle_plate,
                p.talon_no
            FROM users u
            JOIN user_profiles p ON u.user_id = p.user_id
            WHERE u.status = 'approved'
              AND (
                    p.national_id      IS NOT NULL
                 OR p.driving_licence  IS NOT NULL
                 OR p.vehicle_plate    IS NOT NULL
              )
            """
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
