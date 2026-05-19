from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Mapping

from app.config import settings
from app.database import get_db


@dataclass(frozen=True)
class SecurityConfig:
    login_rate_limit: int
    login_rate_limit_window_minutes: int
    lockout_threshold: int
    lockout_duration_minutes: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


_BOUNDS: dict[str, tuple[int, int]] = {
    "login_rate_limit": (1, 100),
    "login_rate_limit_window_minutes": (1, 1440),
    "lockout_threshold": (1, 100),
    "lockout_duration_minutes": (1, 1440),
}

_cache: SecurityConfig | None = None
_lock = asyncio.Lock()


def _defaults() -> dict[str, int]:
    return {
        "login_rate_limit": int(settings.login_rate_limit),
        "login_rate_limit_window_minutes": int(settings.login_rate_limit_window_minutes),
        "lockout_threshold": int(settings.lockout_threshold),
        "lockout_duration_minutes": int(settings.lockout_duration_minutes),
    }


def _coerce_values(values: Mapping[str, object]) -> SecurityConfig:
    data = _defaults()
    for key in data:
        if key not in values:
            continue
        try:
            value = int(values[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        low, high = _BOUNDS[key]
        if value < low or value > high:
            raise ValueError(f"{key} must be between {low} and {high}")
        data[key] = value
    return SecurityConfig(**data)


def get_security_config() -> SecurityConfig:
    return _cache or _coerce_values({})


async def load_security_config() -> SecurityConfig:
    global _cache
    async with _lock:
        defaults = _defaults()
        now = time.time()
        async with get_db() as conn:
            for key, value in defaults.items():
                await conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO NOTHING",
                    (key, str(value), now),
                )
            cursor = await conn.execute(
                "SELECT key, value FROM app_settings WHERE key IN (?, ?, ?, ?)",
                tuple(defaults.keys()),
            )
            rows = await cursor.fetchall()
            await conn.commit()

        _cache = _coerce_values({row["key"]: row["value"] for row in rows})
        return _cache


async def save_security_config(values: Mapping[str, object]) -> SecurityConfig:
    global _cache
    config = _coerce_values(values)
    now = time.time()
    async with _lock:
        async with get_db() as conn:
            for key, value in config.as_dict().items():
                await conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                    (key, str(value), now),
                )
            await conn.commit()
        _cache = config
    return config


def apply_security_config(config: SecurityConfig) -> None:
    from app.security import login_rate_limiter

    login_rate_limiter.configure(
        max_attempts=config.login_rate_limit,
        window_seconds=config.login_rate_limit_window_minutes * 60,
    )


async def load_and_apply_security_config() -> SecurityConfig:
    config = await load_security_config()
    apply_security_config(config)
    return config
