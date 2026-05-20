"""Camera and worker token management.

Tokens look like ``sc-cam-<id>-<32_hex>`` for cameras and
``sc-wrk-<id>-<32_hex>`` for workers. The integer id is parsed first to
short-circuit the DB lookup; the random suffix is the actual secret and is
stored in the database as an argon2 hash.

Plaintext tokens are returned exactly once — when the admin issues them.
After that they only exist on the device that consumes them.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.database import get_db


log = logging.getLogger("sentinelCam.tokens")


_TOKEN_RE = re.compile(r"^sc-(cam|wrk)-(\d+)-([0-9a-f]{32,})$")
_ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)


def _hash(secret: str) -> str:
    return _ph.hash(secret)


def _verify(stored_hash: str, secret: str) -> bool:
    try:
        return _ph.verify(stored_hash, secret)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _generate_secret() -> str:
    return secrets.token_hex(16)


def _parse(token: str) -> Optional[tuple[str, int, str]]:
    match = _TOKEN_RE.match((token or "").strip())
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


# ---------------------------------------------------------------------------
#  Camera tokens
# ---------------------------------------------------------------------------

async def issue_camera_token(name: str) -> tuple[int, str]:
    name = (name or "").strip()
    if not name:
        raise ValueError("camera name is required")
    if len(name) > 80:
        raise ValueError("camera name is too long (max 80 chars)")

    secret = _generate_secret()
    token_hash = _hash(secret)

    async with get_db() as conn:
        cursor = await conn.execute(
            "INSERT INTO cameras (name, token_hash) VALUES (?, ?) RETURNING id",
            (name, token_hash),
        )
        row = await cursor.fetchone()
    if not row:
        raise RuntimeError("failed to insert camera row")
    cam_id = int(row["id"])
    return cam_id, f"sc-cam-{cam_id}-{secret}"


async def revoke_camera(camera_id: int) -> bool:
    async with get_db() as conn:
        cursor = await conn.execute(
            "UPDATE cameras SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (time.time(), int(camera_id)),
        )
    return cursor.rowcount > 0


async def list_cameras() -> list[dict[str, object]]:
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id, name, created_at, revoked_at, last_frame_at, last_frame_size_bytes, total_frames "
            "FROM cameras ORDER BY id"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def verify_camera_token(token: str) -> Optional[int]:
    parsed = _parse(token)
    if not parsed or parsed[0] != "cam":
        return None
    _, cam_id, secret = parsed
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT token_hash, revoked_at FROM cameras WHERE id = ?",
            (cam_id,),
        )
        row = await cursor.fetchone()
    if not row or row["revoked_at"] is not None:
        return None
    if not _verify(row["token_hash"], secret):
        return None
    return cam_id


async def record_camera_frame(camera_id: int, size_bytes: int) -> None:
    async with get_db() as conn:
        await conn.execute(
            "UPDATE cameras SET last_frame_at = ?, last_frame_size_bytes = ?, total_frames = total_frames + 1 "
            "WHERE id = ?",
            (time.time(), int(size_bytes), int(camera_id)),
        )


# ---------------------------------------------------------------------------
#  Worker tokens
# ---------------------------------------------------------------------------

async def issue_worker_token(name: str) -> tuple[int, str]:
    name = (name or "").strip()
    if not name:
        raise ValueError("worker name is required")
    if len(name) > 80:
        raise ValueError("worker name is too long (max 80 chars)")

    secret = _generate_secret()
    token_hash = _hash(secret)

    async with get_db() as conn:
        cursor = await conn.execute(
            "INSERT INTO workers (name, token_hash) VALUES (?, ?) RETURNING id",
            (name, token_hash),
        )
        row = await cursor.fetchone()
    if not row:
        raise RuntimeError("failed to insert worker row")
    worker_id = int(row["id"])
    return worker_id, f"sc-wrk-{worker_id}-{secret}"


async def revoke_worker(worker_id: int) -> bool:
    async with get_db() as conn:
        cursor = await conn.execute(
            "UPDATE workers SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (time.time(), int(worker_id)),
        )
    return cursor.rowcount > 0


async def list_workers() -> list[dict[str, object]]:
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id, name, created_at, revoked_at, last_seen_at, last_status FROM workers ORDER BY id"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def verify_worker_token(token: str) -> Optional[int]:
    parsed = _parse(token)
    if not parsed or parsed[0] != "wrk":
        return None
    _, worker_id, secret = parsed
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT token_hash, revoked_at FROM workers WHERE id = ?",
            (worker_id,),
        )
        row = await cursor.fetchone()
    if not row or row["revoked_at"] is not None:
        return None
    if not _verify(row["token_hash"], secret):
        return None
    return worker_id


async def update_worker_status(worker_id: int, status_json: str) -> None:
    async with get_db() as conn:
        await conn.execute(
            "UPDATE workers SET last_seen_at = ?, last_status = ? WHERE id = ?",
            (time.time(), status_json[:2000], int(worker_id)),
        )
