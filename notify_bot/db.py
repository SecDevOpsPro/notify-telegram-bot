"""Async SQLite database layer — users, per-user profiles and their vehicles.

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

Each user can save up to ``MAX_VEHICLES`` vehicles (``user_vehicles``).
``user_profiles.vehicle_plate`` names the preferred ("main") one — the
vehicle a check uses when no plate is given.  The vehicle functions here
keep it pointing at one of the user's vehicles: the first vehicle saved
becomes preferred, and deleting the preferred one hands the role to the
oldest remaining vehicle, or clears it when it was the last.

Schema changes are versioned with ``PRAGMA user_version`` — see ``_migrate``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal, TypedDict, cast

import aiosqlite

logger = logging.getLogger(__name__)

# Allow tests to override DATABASE_PATH via environment variable.
DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "/app/data/bot.db")

#: How many vehicles one user can save — each adds ~5 API calls to the daily report.
MAX_VEHICLES = 5

# ── Row types ────────────────────────────────────────────────────────────────

UserStatus = Literal["pending", "approved", "denied"]


class UserRow(TypedDict):
    """A row of the ``users`` table."""

    user_id: int
    username: str | None
    first_name: str | None
    status: UserStatus
    created_at: str
    updated_at: str  # when ``status`` last changed (e.g. approval)


ProfileField = Literal["national_id", "driving_licence", "vehicle_plate"]


class ProfileRow(TypedDict):
    """A row of the ``user_profiles`` table."""

    user_id: int
    national_id: str | None
    driving_licence: str | None
    vehicle_plate: str | None  # the preferred vehicle's plate — see the module docstring
    created_at: str
    updated_at: str


class VehicleRow(TypedDict):
    """A row of the ``user_vehicles`` table."""

    id: int
    user_id: int
    plate: str
    talon_no: str | None  # the technical inspection (GTP) check needs plate + talon
    created_at: str
    updated_at: str


class ReportTarget(TypedDict):
    """One user's data as consumed by the daily report (``scheduler.jobs``)."""

    user_id: int
    first_name: str | None
    national_id: str | None
    driving_licence: str | None
    vehicles: list[VehicleRow]  # preferred first


class VehicleLimitError(Exception):
    """Raised when saving a new vehicle would exceed ``MAX_VEHICLES``."""


# ── Schema ───────────────────────────────────────────────────────────────────

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""

_CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id          INTEGER PRIMARY KEY REFERENCES users(user_id),
    national_id      TEXT,
    driving_licence  TEXT,
    vehicle_plate    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
"""

_CREATE_VEHICLES = """
CREATE TABLE IF NOT EXISTS user_vehicles (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(user_id),
    plate       TEXT NOT NULL,
    talon_no    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (user_id, plate)
)
"""

#: Bump alongside each new step in ``_migrate``.
_SCHEMA_VERSION = 1

# Vehicles in display order: the preferred one first, then oldest first.
# Needs ``user_vehicles v`` joined with ``user_profiles p``.
_VEHICLE_ORDER = "(v.plate IS p.vehicle_plate) DESC, v.id"

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


def normalize_plate(plate: str) -> str:
    """The form plates are stored and compared in: uppercase, no surrounding whitespace."""
    return plate.strip().upper()


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


@asynccontextmanager
async def _transaction() -> AsyncIterator[aiosqlite.Connection]:
    """Yield the locked shared connection, committing when the block succeeds.

    Rolls back on failure so a raised exception never leaves an open
    transaction sitting on the long-lived connection for the next caller.
    """
    async with _locked_conn() as conn:
        try:
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise


async def _write(sql: str, params: tuple[Any, ...] = ()) -> None:
    """Execute a single write statement on the shared connection and commit."""
    async with _transaction() as conn:
        await conn.execute(sql, params)


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    """Return whether *column* exists in *table* using SQLite metadata."""
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return any(row[1] == column for row in rows)


# ── Migrations ────────────────────────────────────────────────────────────────


async def _migrate_to_vehicles(conn: aiosqlite.Connection) -> None:
    """Version 1: move each profile's single plate + talon into ``user_vehicles``.

    The profile's ``vehicle_plate`` stays, normalised, as the preferred-vehicle
    pointer.  The talon now lives only on the vehicle, so its profile column
    is dropped.  Also adds the missing ``created_at`` / ``updated_at`` columns.
    """
    # Profiles only had updated_at — the best guess for when they were created.
    if not await _column_exists(conn, "user_profiles", "created_at"):
        await conn.execute(
            "ALTER TABLE user_profiles ADD COLUMN created_at TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute("UPDATE user_profiles SET created_at = updated_at")
    # Databases from before the talon was added never got the column.
    if not await _column_exists(conn, "user_profiles", "talon_no"):
        await conn.execute("ALTER TABLE user_profiles ADD COLUMN talon_no TEXT")
    await conn.execute(
        """
        INSERT OR IGNORE INTO user_vehicles (user_id, plate, talon_no, created_at, updated_at)
        SELECT user_id, UPPER(TRIM(vehicle_plate)), talon_no, updated_at, updated_at
        FROM user_profiles
        WHERE TRIM(vehicle_plate) <> ''
        """
    )
    await conn.execute(
        "UPDATE user_profiles SET vehicle_plate = NULLIF(UPPER(TRIM(vehicle_plate)), '')"
    )
    await conn.execute("ALTER TABLE user_profiles DROP COLUMN talon_no")

    # Add users.updated_at, starting existing users at their created_at.
    # A fresh database already has it from _CREATE_USERS.
    if not await _column_exists(conn, "users", "updated_at"):
        await conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        await conn.execute("UPDATE users SET updated_at = created_at")


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Bring the schema up to ``_SCHEMA_VERSION``, one numbered step at a time.

    Each step runs exactly once per database (tracked in ``PRAGMA
    user_version``), so a data migration never re-runs on restart — e.g.
    re-copying plates would bring back vehicles a user has since deleted.
    """
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    version = row[0] if row else 0
    if version < 1:
        await _migrate_to_vehicles(conn)
    if version < _SCHEMA_VERSION:
        await conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def init_db() -> None:
    """Open the shared connection (closing any previous one), create tables and migrate.

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
            _connection = None

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
        # One explicit transaction, so a failed migration leaves the file untouched.
        await conn.execute("BEGIN")
        try:
            await conn.execute(_CREATE_USERS)
            await conn.execute(_CREATE_PROFILES)
            await conn.execute(_CREATE_VEHICLES)
            await _migrate(conn)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            await conn.close()
            raise

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
    now = _now()
    await _write(
        """
        INSERT INTO users (user_id, username, first_name, status, created_at, updated_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username   = excluded.username,
            first_name = excluded.first_name
        """,
        (user_id, username, first_name, now, now),
    )


async def get_user(user_id: int) -> UserRow | None:
    async with _locked_conn() as conn:
        async with conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return cast(UserRow, dict(row)) if row else None


async def set_user_status(user_id: int, status: UserStatus) -> None:
    """Update a user's approval status, stamping ``updated_at``."""
    await _write(
        "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
        (status, _now(), user_id),
    )


async def list_users_by_status(status: UserStatus) -> list[UserRow]:
    async with _locked_conn() as conn:
        async with conn.execute("SELECT * FROM users WHERE status = ?", (status,)) as cur:
            rows = await cur.fetchall()
            return [cast(UserRow, dict(r)) for r in rows]


# ── Profiles ──────────────────────────────────────────────────────────────────


async def get_profile(user_id: int) -> ProfileRow | None:
    async with _locked_conn() as conn:
        async with conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return cast(ProfileRow, dict(row)) if row else None


async def upsert_profile(
    user_id: int,
    *,
    national_id: str | None = None,
    driving_licence: str | None = None,
) -> None:
    """
    Insert or partially update a user profile's personal data.
    Only non-None arguments overwrite existing values.  The preferred
    vehicle is set by the vehicle functions below, never directly.

    Uses a single atomic INSERT ... ON CONFLICT so concurrent calls for the
    same user_id never race on a read-then-write.
    COALESCE keeps the existing column value when the argument is None.
    """
    now = _now()
    await _write(
        """
        INSERT INTO user_profiles
            (user_id, national_id, driving_licence, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            national_id     = COALESCE(excluded.national_id,     national_id),
            driving_licence = COALESCE(excluded.driving_licence, driving_licence),
            updated_at      = excluded.updated_at
        """,
        (user_id, national_id, driving_licence, now, now),
    )


async def delete_profile(user_id: int) -> None:
    """Remove a user's saved profile (national_id, licence) and all their vehicles."""
    async with _transaction() as conn:
        await conn.execute("DELETE FROM user_vehicles WHERE user_id = ?", (user_id,))
        await conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))


# ── Vehicles ──────────────────────────────────────────────────────────────────


async def _set_preferred(conn: aiosqlite.Connection, user_id: int, plate: str | None) -> None:
    now = _now()
    await conn.execute(
        """
        INSERT INTO user_profiles (user_id, vehicle_plate, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            vehicle_plate = excluded.vehicle_plate,
            updated_at    = excluded.updated_at
        """,
        (user_id, plate, now, now),
    )


async def _has_vehicle(conn: aiosqlite.Connection, user_id: int, plate: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM user_vehicles WHERE user_id = ? AND plate = ?", (user_id, plate)
    ) as cur:
        return await cur.fetchone() is not None


async def list_vehicles(user_id: int) -> list[VehicleRow]:
    """A user's vehicles: the preferred one first, then in the order they were saved."""
    async with _locked_conn() as conn:
        async with conn.execute(
            f"""
            SELECT v.* FROM user_vehicles v
            LEFT JOIN user_profiles p ON p.user_id = v.user_id
            WHERE v.user_id = ?
            ORDER BY {_VEHICLE_ORDER}
            """,
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [cast(VehicleRow, dict(r)) for r in rows]


async def get_vehicle(user_id: int, plate: str) -> VehicleRow | None:
    async with _locked_conn() as conn:
        async with conn.execute(
            "SELECT * FROM user_vehicles WHERE user_id = ? AND plate = ?",
            (user_id, normalize_plate(plate)),
        ) as cur:
            row = await cur.fetchone()
            return cast(VehicleRow, dict(row)) if row else None


async def save_vehicle(
    user_id: int, plate: str, talon_no: str | None = None, *, make_preferred: bool = False
) -> None:
    """Add a vehicle, or update the talon of one the user already has.

    A ``None`` talon keeps the stored one.  The vehicle becomes preferred
    when *make_preferred* is set or the user has no preferred vehicle yet.
    Raises ``VehicleLimitError`` when adding it would exceed ``MAX_VEHICLES``.
    """
    plate = normalize_plate(plate)
    async with _transaction() as conn:
        if await _has_vehicle(conn, user_id, plate):
            if talon_no is not None:
                await conn.execute(
                    "UPDATE user_vehicles SET talon_no = ?, updated_at = ? "
                    "WHERE user_id = ? AND plate = ?",
                    (talon_no, _now(), user_id, plate),
                )
        else:
            async with conn.execute(
                "SELECT COUNT(*) FROM user_vehicles WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] >= MAX_VEHICLES:
                raise VehicleLimitError(f"user {user_id} already has {MAX_VEHICLES} vehicles")
            now = _now()
            await conn.execute(
                "INSERT INTO user_vehicles (user_id, plate, talon_no, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, plate, talon_no, now, now),
            )

        async with conn.execute(
            "SELECT vehicle_plate FROM user_profiles WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if make_preferred or not (row and row[0]):
            await _set_preferred(conn, user_id, plate)


async def set_preferred_vehicle(user_id: int, plate: str) -> bool:
    """Make one of the user's vehicles the preferred one. False if they don't have it."""
    plate = normalize_plate(plate)
    async with _transaction() as conn:
        if not await _has_vehicle(conn, user_id, plate):
            return False
        await _set_preferred(conn, user_id, plate)
        return True


async def delete_vehicle(user_id: int, plate: str) -> bool:
    """Remove one of the user's vehicles. False if they don't have it.

    Deleting the preferred vehicle passes the role to the oldest remaining
    one, or clears it when it was the last.
    """
    plate = normalize_plate(plate)
    async with _transaction() as conn:
        if not await _has_vehicle(conn, user_id, plate):
            return False
        await conn.execute(
            "DELETE FROM user_vehicles WHERE user_id = ? AND plate = ?", (user_id, plate)
        )
        await conn.execute(
            """
            UPDATE user_profiles SET
                vehicle_plate = (
                    SELECT plate FROM user_vehicles WHERE user_id = ? ORDER BY id LIMIT 1
                ),
                updated_at = ?
            WHERE user_id = ? AND vehicle_plate = ?
            """,
            (user_id, _now(), user_id, plate),
        )
        return True


# ── Scheduler helpers ─────────────────────────────────────────────────────────


async def _report_targets(where: str = "", params: tuple[Any, ...] = ()) -> list[ReportTarget]:
    """Approved users (narrowed by the *where* clause) with data a report check applies to."""
    async with _locked_conn() as conn:
        async with conn.execute(
            f"""
            SELECT u.user_id, u.first_name, p.national_id, p.driving_licence
            FROM users u
            JOIN user_profiles p ON u.user_id = p.user_id
            WHERE u.status = 'approved' {where}
            ORDER BY u.user_id
            """,
            params,
        ) as cur:
            users = await cur.fetchall()
        async with conn.execute(
            f"""
            SELECT v.* FROM user_vehicles v
            JOIN users u ON u.user_id = v.user_id
            LEFT JOIN user_profiles p ON p.user_id = v.user_id
            WHERE u.status = 'approved' {where}
            ORDER BY v.user_id, {_VEHICLE_ORDER}
            """,
            params,
        ) as cur:
            vehicle_rows = await cur.fetchall()

    vehicles: dict[int, list[VehicleRow]] = {}
    for r in vehicle_rows:
        vehicles.setdefault(r["user_id"], []).append(cast(VehicleRow, dict(r)))

    targets: list[ReportTarget] = []
    for u in users:
        target: ReportTarget = {
            "user_id": u["user_id"],
            "first_name": u["first_name"],
            "national_id": u["national_id"],
            "driving_licence": u["driving_licence"],
            "vehicles": vehicles.get(u["user_id"], []),
        }
        if target["national_id"] or target["driving_licence"] or target["vehicles"]:
            targets.append(target)
    return targets


async def get_all_approved_with_profiles() -> list[ReportTarget]:
    """Return approved users who have a national ID, licence or vehicle saved."""
    return await _report_targets()


async def get_report_target(user_id: int) -> ReportTarget | None:
    """One user's report data, or None if they aren't approved or have nothing saved."""
    targets = await _report_targets("AND u.user_id = ?", (user_id,))
    return targets[0] if targets else None
