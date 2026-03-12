from __future__ import annotations

import asyncio
import getpass
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from app.config import settings

log = logging.getLogger("sentinelCam.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'viewer')) DEFAULT 'viewer',
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until REAL,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
    last_login REAL
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id BLOB NOT NULL UNIQUE,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT 'Passkey',
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
    expires_at REAL NOT NULL,
    ip_address TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('image', 'video')),
    filename TEXT NOT NULL,
    overlay_filename TEXT,
    raw_filename TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    shared INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_recordings_user_created ON recordings(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON webauthn_credentials(user_id);
"""


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        yield conn


async def init_db() -> None:
    db_path = Path(settings.database_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA)
        # Migration: add shared column if missing
        cursor = await conn.execute("PRAGMA table_info(recordings)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "shared" not in columns:
            await conn.execute("ALTER TABLE recordings ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
        log.info("Database schema initialized at %s", db_path)

    await _ensure_initial_admin()


async def _ensure_initial_admin() -> None:
    from app.security import hash_password

    async with get_db() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        if row and row[0] > 0:
            return

    username = settings.initial_admin_user.strip()
    password = settings.initial_admin_password.strip()

    if not username or not password:
        if os.isatty(0):
            print("\n=== sentinelCam: No admin user found. Creating initial admin. ===")
            while not username:
                username = input("Admin username: ").strip()
            while len(password) < settings.min_password_length:
                password = getpass.getpass(
                    f"Admin password (min {settings.min_password_length} chars): "
                )
                if len(password) < settings.min_password_length:
                    print(f"Password too short (min {settings.min_password_length} chars).")
        else:
            log.warning(
                "No users in database and INITIAL_ADMIN_USER/INITIAL_ADMIN_PASSWORD not set. "
                "Set these env vars to auto-create admin on first start."
            )
            return

    if len(password) < settings.min_password_length:
        log.error(
            "INITIAL_ADMIN_PASSWORD too short (min %d chars). Admin not created.",
            settings.min_password_length,
        )
        return

    password_hash = hash_password(password)
    async with get_db() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            (username, password_hash),
        )
        await conn.commit()
    log.info("Initial admin user '%s' created", username)
