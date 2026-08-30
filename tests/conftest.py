"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def tmp_db(tmp_path, monkeypatch):
    """
    Override DATABASE_PATH for every test so we work against an isolated,
    ephemeral SQLite file rather than the production database.

    ``notify_bot.db`` keeps one long-lived connection open (on its own
    background thread) rather than one per call, so it must be closed on
    teardown — otherwise that non-daemon thread keeps the test process
    alive after the run finishes.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    import notify_bot.config as cfg
    import notify_bot.db as db_mod

    monkeypatch.setattr(cfg, "DATABASE_PATH", db_path)
    monkeypatch.setattr(db_mod, "DATABASE_PATH", db_path)
    yield db_path
    await db_mod.close_db()
