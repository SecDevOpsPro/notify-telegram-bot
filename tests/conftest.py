"""Pytest configuration and shared fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """
    Override DATABASE_PATH for every test so we work against an isolated,
    ephemeral SQLite file rather than the production database.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    import notify_bot.config as cfg
    import notify_bot.db as db_mod

    monkeypatch.setattr(cfg, "DATABASE_PATH", db_path)
    monkeypatch.setattr(db_mod, "DATABASE_PATH", db_path)
    return db_path
