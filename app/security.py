from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app.config import settings

log = logging.getLogger("sentinelCam.security")

_ph = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hash_value: str) -> bool:
    try:
        return _ph.verify(hash_value, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_session_id() -> str:
    return secrets.token_hex(32)


def generate_csrf_token() -> str:
    return secrets.token_hex(32)


class LoginRateLimiter:
    """In-memory per-IP rate limiter: max N attempts per window_seconds."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 900) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._store: dict[str, list[float]] = {}

    def is_allowed(self, ip: str) -> bool:
        now = time.time()
        attempts = [t for t in self._store.get(ip, []) if now - t < self._window]
        self._store[ip] = attempts
        return len(attempts) < self._max

    def record_attempt(self, ip: str) -> None:
        now = time.time()
        attempts = [t for t in self._store.get(ip, []) if now - t < self._window]
        attempts.append(now)
        self._store[ip] = attempts

    def remaining(self, ip: str) -> int:
        now = time.time()
        attempts = [t for t in self._store.get(ip, []) if now - t < self._window]
        return max(0, self._max - len(attempts))


login_rate_limiter = LoginRateLimiter(
    max_attempts=settings.login_rate_limit,
    window_seconds=900,
)
