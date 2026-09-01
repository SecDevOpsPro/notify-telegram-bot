"""Async SQLite database layer — users and per-user profiles.

Uses a single, long-lived ``aiosqlite`` connection rather than opening one
per call.  ``aiosqlite`` runs each connection on its own dedicated
background thread (sqlite3 is synchronous), so opening a fresh connection
for every query meant spawning and tearing down an OS thread per query.
Reusing one connection keeps that to a single thread for the process.  A
single ``sqlite3`` connection also means a single implicit transaction can
span several ``await`` points (e.g. between ``execute()`` and ``commit()``),
so every access — reads included — goes through ``_connection_lock`` to stop
one coroutine's statements (or its rollback-on-error) from interleaving with
another's before it commits.  WAL mode + a busy timeout are set so any
external/concurrent access to the file doesn't raise "database is locked"
immediately.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite

logger = logging.getLogger(__name__)

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

# ── Shared connection ────────────────────────────────────────────────────────

_connection: aiosqlite.Connection | None = None
_connection_lock = asyncio.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    """Re-read at call time so tests can monkeypatch the module attribute."""
    import notify_bot.db as _self

    return _self.DATABASE_PATH


def _conn() -> aiosqlite.Connection:
    """Return the shared connection. Raises if ``init_db()`` hasn't run yet."""
    if _connection is None:
        raise RuntimeError("Database not initialised — call init_db() first.")
    return _connection


@asynccontextmanager
async def _locked_conn() -> AsyncIterator[aiosqlite.Connection]:
    """Yield the shared connection while holding ``_connection_lock``.

    Every caller — reads and writes alike — goes through this, so a whole
    logical operation (e.g. execute+commit) always completes before another
    coroutine's operation can touch the connection.
    """
    async with _connection_lock:
        yield _conn()


async def _write(sql: str, params: tuple = ()) -> None:
    """Execute a write statement on the shared connection and commit.

    Rolls back on failure so a raised exception never leaves an open
    transaction sitting on the long-lived connection for the next caller.
    """
    async with _locked_conn() as conn:
        try:
            await conn.execute(sql, params)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    """Return whether *column* exists in *table* using SQLite metadata."""
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(row[1] == column for row in rows)

# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def init_db() -> None:
    """Open the shared connection (closing any previous one) and create tables.

    Call once at process startup (e.g. from ``run_bot._post_init``).  Do not
    call again while the bot is serving traffic — ``init_db`` closes any
    existing connection immediately, which will break in-flight queries.
    """
    global _connection, _connection_lock

    Path(_db_path()).parent.mkdir(parents=True, exist_ok=True)

    # A fresh, never-yet-acquired lock: an ``asyncio.Lock`` permanently binds
    # to whichever event loop first contends on it, and raises if later
    # acquired from a different loop (e.g. across pytest's per-test loops).
    # Recreating it alongside the connection keeps both scoped to "this
    # process/test's" loop.
    _connection_lock = asyncio.Lock()

    async with _connection_lock:
        if _connection is not None:
            await _connection.close()

        conn = await aiosqlite.connect(_db_path())
        conn.row_factory = aiosqlite.Row
        async with conn.execute("PRAGMA journal_mode=WAL") as cur:
            row = await cur.fetchone()
        journal_mode = (row[0] if row else "").lower()
        if journal_mode != "wal":
            logger.warning(
                "Could not enable WAL journal mode (got %r); "
                "concurrent external access may hit 'database is locked' sooner",
                journal_mode or None,
            )
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute(_CREATE_USERS)
        await conn.execute(_CREATE_PROFILES)
        # Migrate: add talon_no if it doesn't exist yet (idempotent)
        if not await _column_exists(conn, "user_profiles", "talon_no"):
            await conn.execute(_ADD_TALON_COLUMN)
        await conn.commit()

        _connection = conn


async def close_db() -> None:
    """Close the shared connection. Call on process shutdown."""
    global _connection
    async with _connection_lock:
        if _connection is not None:
            await _connection.close()
            _connection = None


# ── Users ─────────────────────────────────────────────────────────────────────


async def upsert_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
) -> None:
    """Insert a new user with status=pending, or update name/username if they exist."""
    await _write(
        """
        INSERT INTO users (user_id, username, first_name, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username   = excluded.username,
            first_name = excluded.first_name
        """,
        (user_id, username, first_name, _now()),
    )


async def get_user(user_id: int) -> Optional[dict]:
    async with _locked_conn() as conn:
        async with conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_user_status(user_id: int, status: str) -> None:
    """Update a user's approval status ('pending' | 'approved' | 'denied')."""
    await _write("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))


async def list_users_by_status(status: str) -> list[dict]:
    async with _locked_conn() as conn:
        async with conn.execute("SELECT * FROM users WHERE status = ?", (status,)) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Profiles ──────────────────────────────────────────────────────────────────


async def get_profile(user_id: int) -> Optional[dict]:
    async with _locked_conn() as conn:
        async with conn.execute(
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
    talon_no: str | None = None,
) -> None:
    """
    Insert or partially update a user profile.
    Only non-None arguments overwrite existing values.

    Uses a single atomic INSERT ... ON CONFLICT so concurrent calls for the
    same user_id never race on a read-then-write.
    COALESCE keeps the existing column value when the argument is None.
    """
    await _write(
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


async def delete_profile(user_id: int) -> None:
    """Remove a user's saved profile (national_id, licence, plate)."""
    await _write("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))


# ── Scheduler helpers ─────────────────────────────────────────────────────────


async def get_all_approved_with_profiles() -> list[dict]:
    """Return approved users who have at least one profile field populated."""
    async with _locked_conn() as conn:
        async with conn.execute(
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
