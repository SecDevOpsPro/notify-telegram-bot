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
    updated_at       TEXT NOT NULL
)
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
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_user_status(user_id: int, status: str) -> None:
    """Update a user's approval status ('pending' | 'approved' | 'denied')."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "UPDATE users SET status = ? WHERE user_id = ?", (status, user_id)
        )
        await db.commit()


async def list_users_by_status(status: str) -> list[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE status = ?", (status,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Profiles ──────────────────────────────────────────────────────────────────


async def get_profile(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_profile(
    user_id: int,
    *,
    national_id: str | None = None,
    driving_licence: str | None = None,
    vehicle_plate: str | None = None,
) -> None:
    """
    Insert or partially update a user profile.
    Only non-None arguments overwrite existing values.
    """
    existing = await get_profile(user_id)

    if not existing:
        async with aiosqlite.connect(_db_path()) as db:
            await db.execute(
                """
                INSERT INTO user_profiles
                    (user_id, national_id, driving_licence, vehicle_plate, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, national_id, driving_licence, vehicle_plate, _now()),
            )
            await db.commit()
        return

    # Partial update — only touch explicitly provided fields
    updates: dict[str, str] = {}
    if national_id is not None:
        updates["national_id"] = national_id
    if driving_licence is not None:
        updates["driving_licence"] = driving_licence
    if vehicle_plate is not None:
        updates["vehicle_plate"] = vehicle_plate

    if not updates:
        return

    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    values = list(updates.values()) + [user_id]

    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            f"UPDATE user_profiles SET {set_clause} WHERE user_id = ?", values
        )
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
                p.vehicle_plate
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
