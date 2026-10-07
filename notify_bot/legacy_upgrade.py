"""One-off upgrade of a pre-multi-vehicle database.  Delete this module once applied.

Before multi-vehicle support each profile held a single plate and talon.  This
moves them into ``user_vehicles`` and adds the ``created_at`` / ``updated_at``
columns the old schema lacked.  ``db.init_db`` calls ``upgrade`` inside its
startup transaction; removing it means deleting this file and that one call.

The old schema is recognised by ``user_profiles.talon_no``, which every
pre-multi-vehicle database has and which the upgrade drops — so it runs once,
never re-copies plates a user has since deleted, and is a no-op on a fresh
database.
"""

from __future__ import annotations

import aiosqlite


async def _is_old_schema(conn: aiosqlite.Connection) -> bool:
    async with conn.execute("PRAGMA table_info(user_profiles)") as cur:
        return any(row[1] == "talon_no" for row in await cur.fetchall())


async def upgrade(conn: aiosqlite.Connection) -> None:
    """Convert an old database in place; do nothing if it's already current."""
    if not await _is_old_schema(conn):
        return

    # The profile's plate becomes a vehicle, keeping its talon; the plate stays,
    # normalised, as the main-vehicle pointer.
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

    # Profiles only had updated_at — the best guess for when they were created.
    await conn.execute("ALTER TABLE user_profiles ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    await conn.execute("UPDATE user_profiles SET created_at = updated_at")
    # Users start with updated_at = created_at.
    await conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    await conn.execute("UPDATE users SET updated_at = created_at")
