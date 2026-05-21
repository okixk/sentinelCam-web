"""Admin-configured automatic recording on YOLO detection.

The worker emits a JSON ``detection`` text frame on its WebSocket
connection whenever the inference step recognises one or more configured
classes. The web server reads the persisted :class:`AutoCaptureConfig`,
applies a per-camera cooldown so we do not flood storage with near-
duplicate clips, and saves a snapshot or short clip as a recording owned
by the lowest-id admin user.

The config is persisted in the ``app_settings`` table so it survives
restarts. The whole thing is opt-in — when disabled the detection frames
are still parsed (so we can log a heartbeat-style count) but no recording
is created.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.database import get_db
from app.recording.live_capture import (
    ALLOWED_CLIP_DURATIONS,
    record_clip_from_hub,
    snapshot_from_hub,
)
from app.streaming.hub import frame_hubs


log = logging.getLogger("sentinelCam.auto_capture")


# COCO-80 — the default class set every Ultralytics YOLO checkpoint knows.
# Surfaced to the admin UI so picking trigger classes is point-and-click.
COCO_CLASSES: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
)
COCO_SET = frozenset(COCO_CLASSES)

_SETTINGS_KEY = "auto_capture"
_DEFAULT_COOLDOWN_S = 30
_DEFAULT_DURATION_S = 15
_DEFAULT_MODE = "clip"
_ALLOWED_MODES = ("snapshot", "clip")


@dataclass
class AutoCaptureConfig:
    enabled: bool = False
    classes: list[str] = field(default_factory=list)
    cooldown_s: int = _DEFAULT_COOLDOWN_S
    duration_s: int = _DEFAULT_DURATION_S
    mode: str = _DEFAULT_MODE

    @classmethod
    def default(cls) -> "AutoCaptureConfig":
        return cls()

    def as_dict(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "classes": list(self.classes),
            "cooldown_s": int(self.cooldown_s),
            "duration_s": int(self.duration_s),
            "mode": self.mode,
        }


def _coerce_classes(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [c for c in value if isinstance(c, str) and c in COCO_SET]


def _validate_payload(payload: dict) -> AutoCaptureConfig:
    enabled = bool(payload.get("enabled", False))
    classes = _coerce_classes(payload.get("classes", []))
    cooldown_s = int(payload.get("cooldown_s", _DEFAULT_COOLDOWN_S))
    if cooldown_s < 1 or cooldown_s > 3600:
        raise ValueError("cooldown_s must be between 1 and 3600")
    mode = str(payload.get("mode", _DEFAULT_MODE)).lower()
    if mode not in _ALLOWED_MODES:
        raise ValueError(f"mode must be one of {_ALLOWED_MODES}")
    duration_s = int(payload.get("duration_s", _DEFAULT_DURATION_S))
    if mode == "clip" and duration_s not in ALLOWED_CLIP_DURATIONS:
        raise ValueError(f"duration_s must be one of {ALLOWED_CLIP_DURATIONS}")
    if enabled and not classes:
        raise ValueError("at least one trigger class is required when enabled")
    return AutoCaptureConfig(
        enabled=enabled,
        classes=classes,
        cooldown_s=cooldown_s,
        duration_s=duration_s,
        mode=mode,
    )


_state_lock = asyncio.Lock()
_current_config: AutoCaptureConfig = AutoCaptureConfig.default()
_last_fired_at: dict[int, float] = {}


async def load_config() -> AutoCaptureConfig:
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (_SETTINGS_KEY,)
        )
        row = await cursor.fetchone()
    if not row:
        return AutoCaptureConfig.default()
    try:
        payload = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        log.warning("auto_capture config row is not valid JSON; using defaults")
        return AutoCaptureConfig.default()
    try:
        return _validate_payload(payload)
    except ValueError as exc:
        log.warning("auto_capture config is invalid (%s); using defaults", exc)
        return AutoCaptureConfig.default()


async def reload() -> AutoCaptureConfig:
    """Re-read the config from the DB and update the in-memory cache."""
    global _current_config
    config = await load_config()
    async with _state_lock:
        _current_config = config
    return config


def get_current_config() -> AutoCaptureConfig:
    return _current_config


async def save_config(payload: dict) -> AutoCaptureConfig:
    config = _validate_payload(payload)
    serialized = json.dumps(config.as_dict())
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = EXCLUDED.updated_at",
            (_SETTINGS_KEY, serialized, time.time()),
        )
    global _current_config
    async with _state_lock:
        _current_config = config
    return config


async def _admin_user_id() -> Optional[int]:
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        )
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


async def handle_detection(camera_id: int, classes: Iterable[str]) -> None:
    """Called by the worker channel for each detection frame.

    Decides whether to fire an auto recording based on the live config and
    a per-camera cooldown. Idempotent — runs as a background task so a
    slow encode does not stall the worker socket.
    """
    config = _current_config
    if not config.enabled or not config.classes:
        return
    hit = next((c for c in classes if c in config.classes), None)
    if hit is None:
        return
    now = time.monotonic()
    last = _last_fired_at.get(int(camera_id), 0.0)
    if now - last < config.cooldown_s:
        return
    _last_fired_at[int(camera_id)] = now

    asyncio.create_task(_fire(camera_id, hit, config))


async def _fire(camera_id: int, trigger: str, config: AutoCaptureConfig) -> None:
    hub = frame_hubs.get(int(camera_id))
    if hub is None:
        log.info("auto_capture skipped: camera %d has no hub yet", camera_id)
        return
    owner = await _admin_user_id()
    if owner is None:
        log.warning("auto_capture skipped: no admin user available as owner")
        return
    try:
        if config.mode == "snapshot":
            recording_id = await snapshot_from_hub(
                hub, owner, description=f"Auto-trigger: {trigger}",
                auto=True, auto_trigger=trigger,
            )
        else:
            recording_id = await record_clip_from_hub(
                hub, owner, config.duration_s,
                description=f"Auto-trigger: {trigger}",
                auto=True, auto_trigger=trigger,
            )
        log.info(
            "auto_capture: saved recording %d (camera %d, trigger=%s, mode=%s)",
            recording_id, camera_id, trigger, config.mode,
        )
    except Exception:
        log.exception(
            "auto_capture: failed to save recording for camera %d trigger=%s",
            camera_id, trigger,
        )


async def trigger_test_fire(camera_id: int, trigger: str = "person") -> None:
    """Admin-only: simulate a detection so the operator can confirm the
    end-to-end auto-capture flow without waiting for the real YOLO worker."""
    config = _current_config
    # Bypass cooldown for the test fire so the operator gets immediate feedback.
    await _fire(camera_id, trigger, config)
