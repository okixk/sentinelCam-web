from __future__ import annotations

import asyncio
import getpass
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable, Optional, Sequence

import asyncpg

from app.config import settings

log = logging.getLogger("sentinelCam.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'viewer')) DEFAULT 'viewer',
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM clock_timestamp()),
    last_login DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id BYTEA NOT NULL UNIQUE,
    public_key BYTEA NOT NULL,
    sign_count BIGINT NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT 'Passkey',
    created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM clock_timestamp())
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM clock_timestamp()),
    expires_at DOUBLE PRECISION NOT NULL,
    ip_address TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS recordings (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('image', 'video')),
    filename TEXT NOT NULL,
    overlay_filename TEXT,
    raw_filename TEXT,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    duration_seconds DOUBLE PRECISION,
    shared INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM clock_timestamp()),
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM clock_timestamp())
);

CREATE TABLE IF NOT EXISTS cameras (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM clock_timestamp()),
    revoked_at DOUBLE PRECISION,
    last_frame_at DOUBLE PRECISION,
    last_frame_size_bytes INTEGER NOT NULL DEFAULT 0,
    total_frames BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workers (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM clock_timestamp()),
    revoked_at DOUBLE PRECISION,
    last_seen_at DOUBLE PRECISION,
    last_status TEXT
);

ALTER TABLE recordings ADD COLUMN IF NOT EXISTS shared INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recordings ADD COLUMN IF NOT EXISTS metadata TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_recordings_user_created ON recordings(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON webauthn_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_cameras_active ON cameras(revoked_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workers_active ON workers(revoked_at) WHERE revoked_at IS NULL;
"""


_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


def _translate_placeholders(sql: str, params: Sequence[Any] | None) -> tuple[str, tuple[Any, ...]]:
    """Convert SQLite-style `?` placeholders to PostgreSQL `$N` placeholders.

    The codebase has no `?` inside string literals, so a straight pass works.
    """
    if not params:
        return sql, ()

    out: list[str] = []
    counter = 0
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
        elif ch == "?" and not in_single and not in_double:
            counter += 1
            out.append(f"${counter}")
        else:
            out.append(ch)
        i += 1

    args = tuple(params) if not isinstance(params, tuple) else params
    return "".join(out), args


_RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)


def _produces_rows(sql: str) -> bool:
    head = sql.lstrip().upper()
    if head.startswith("SELECT") or head.startswith("WITH") or head.startswith("SHOW"):
        return True
    return bool(_RETURNING_RE.search(sql))


_AFFECTED_RE = re.compile(r"^(?:INSERT|UPDATE|DELETE|MERGE)\b.*?(\d+)$", re.IGNORECASE)


def _parse_affected(status: str) -> int:
    if not status:
        return 0
    match = _AFFECTED_RE.match(status.strip())
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return 0
    return 0


class _Cursor:
    """aiosqlite-compatible cursor over an asyncpg execution."""

    __slots__ = ("_rows", "_rowcount", "_idx")

    def __init__(self) -> None:
        self._rows: list[asyncpg.Record] = []
        self._rowcount: int = 0
        self._idx = 0

    async def fetchone(self) -> Optional[asyncpg.Record]:
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    async def fetchall(self) -> list[asyncpg.Record]:
        remaining = self._rows[self._idx:]
        self._idx = len(self._rows)
        return remaining

    @property
    def rowcount(self) -> int:
        return self._rowcount


class _ConnWrapper:
    """Thin shim that mirrors the bits of aiosqlite.Connection the codebase uses."""

    __slots__ = ("_conn",)

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> _Cursor:
        translated, args = _translate_placeholders(sql, params)
        cursor = _Cursor()
        if _produces_rows(translated):
            rows = await self._conn.fetch(translated, *args)
            cursor._rows = list(rows)
            cursor._rowcount = len(rows)
        else:
            status = await self._conn.execute(translated, *args)
            cursor._rowcount = _parse_affected(status)
        return cursor

    async def executescript(self, script: str) -> None:
        # asyncpg accepts multi-statement strings only when no args are passed.
        await self._conn.execute(script)

    async def commit(self) -> None:
        # Commits happen when the get_db() transaction context exits.
        return None


async def _make_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        database=settings.postgres_db,
        min_size=settings.postgres_min_pool,
        max_size=settings.postgres_max_pool,
        command_timeout=30.0,
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            attempt = 0
            while True:
                attempt += 1
                try:
                    _pool = await _make_pool()
                    break
                except (OSError, asyncpg.PostgresError) as exc:
                    if attempt >= 30:
                        raise
                    log.warning("Postgres not ready (attempt %d): %s", attempt, exc)
                    await asyncio.sleep(2.0)
    return _pool


@asynccontextmanager
async def get_db() -> AsyncIterator[_ConnWrapper]:
    pool = await get_pool()
    async with pool.acquire() as raw_conn:
        async with raw_conn.transaction():
            yield _ConnWrapper(raw_conn)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as raw_conn:
        await raw_conn.execute(SCHEMA)
        await raw_conn.execute("DELETE FROM sessions WHERE expires_at < $1", time.time())
    log.info(
        "Database schema initialized on %s:%d/%s",
        settings.postgres_host,
        settings.postgres_port,
        settings.postgres_db,
    )

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
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin') ON CONFLICT (username) DO NOTHING",
            (username, password_hash),
        )
    log.info("Initial admin user '%s' created", username)
