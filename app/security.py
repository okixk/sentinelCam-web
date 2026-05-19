from __future__ import annotations

import logging
import math
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

# Pre-computed dummy hash used to keep the cost of a "user not found" path
# indistinguishable from a "wrong password" path, blocking timing oracles.
_DUMMY_HASH = _ph.hash("sentinelcam-dummy-password-for-timing-safety")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hash_value: str) -> bool:
    try:
        return _ph.verify(hash_value, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify() -> None:
    """Burn one argon2 verify so timing matches the real path."""
    try:
        _ph.verify(_DUMMY_HASH, "wrong-password")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return


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
        self._last_sweep = 0.0

    def configure(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max(1, int(max_attempts))
        self._window = max(1, int(window_seconds))
        self._sweep(time.time())

    def _prune_attempts(self, ip: str, now: float) -> list[float]:
        attempts = [t for t in self._store.get(ip, []) if now - t < self._window]
        if attempts:
            self._store[ip] = attempts
        else:
            self._store.pop(ip, None)
        return attempts

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < min(60, self._window):
            return
        cutoff = now - self._window
        for ip, attempts in list(self._store.items()):
            remaining = [t for t in attempts if t >= cutoff]
            if remaining:
                self._store[ip] = remaining
            else:
                self._store.pop(ip, None)
        self._last_sweep = now

    def is_allowed(self, ip: str) -> bool:
        now = time.time()
        self._sweep(now)
        attempts = self._prune_attempts(ip, now)
        return len(attempts) < self._max

    def record_attempt(self, ip: str) -> None:
        now = time.time()
        self._sweep(now)
        attempts = self._prune_attempts(ip, now)
        attempts.append(now)
        self._store[ip] = attempts

    def remaining(self, ip: str) -> int:
        now = time.time()
        self._sweep(now)
        attempts = self._prune_attempts(ip, now)
        return max(0, self._max - len(attempts))

    def blocked_ips(self) -> list[dict[str, object]]:
        now = time.time()
        self._sweep(now)
        blocked: list[dict[str, object]] = []
        for ip in list(self._store.keys()):
            attempts = self._prune_attempts(ip, now)
            if len(attempts) < self._max:
                continue
            blocked_until = min(attempts) + self._window
            remaining_seconds = max(0, math.ceil(blocked_until - now))
            blocked.append(
                {
                    "ip": ip,
                    "attempts": len(attempts),
                    "limit": self._max,
                    "window_seconds": self._window,
                    "blocked_until": blocked_until,
                    "remaining_seconds": remaining_seconds,
                }
            )
        blocked.sort(key=lambda item: (item["remaining_seconds"], item["ip"]), reverse=True)
        return blocked

    def unblock(self, ip: str) -> bool:
        return self._store.pop(ip, None) is not None


login_rate_limiter = LoginRateLimiter(
    max_attempts=settings.login_rate_limit,
    window_seconds=900,
)
