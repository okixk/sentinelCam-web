"""Capture from a live FrameHub into a Recording.

Two entry points:

- :func:`snapshot_from_hub` grabs the most recent processed and raw JPEG
  for a camera and stores both as a single ``image`` recording.
- :func:`record_clip_from_hub` collects JPEGs from both lanes for a fixed
  duration and pipes them through ffmpeg into MP4s — one for the overlay
  lane, one for the raw lane. The result is a single ``video`` recording
  with both ``overlay_filename`` and ``raw_filename`` set, so the gallery's
  existing overlay-toggle plays both back.

Both functions enforce the per-user storage quota, audit-log, and schedule
a thumbnail warmup so the gallery thumbnail shows up quickly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import HTTPException

from app.config import settings
from app.database import get_db
from app.storage import delete_many, put_bytes, recording_key
from app.streaming.hub import FrameHub
from app.thumbnail_jobs import schedule_thumbnail_warmup


log = logging.getLogger("sentinelCam.live_capture")
AUDIT = logging.getLogger("sentinelCam.audit")

ALLOWED_CLIP_DURATIONS = (10, 30, 60)
CLIP_FPS = 15  # output framerate; matches typical Pi capture rate
MAX_CLIP_FRAMES = max(ALLOWED_CLIP_DURATIONS) * CLIP_FPS * 2  # generous safety cap


def _audit(event: str, **kwargs) -> None:
    AUDIT.info(json.dumps({"event": event, **kwargs, "timestamp": time.time()}))


async def _check_quota(user_id: int, add_bytes: int, cleanup_keys: list[str]) -> None:
    quota_bytes = settings.storage_quota_per_user_mb * 1024 * 1024
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM recordings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
    used = int(row[0] if row else 0)
    if used + add_bytes > quota_bytes:
        if cleanup_keys:
            await delete_many(cleanup_keys)
        raise HTTPException(
            413, f"Storage quota exceeded ({settings.storage_quota_per_user_mb} MB limit)"
        )


async def _insert_recording(
    *,
    user_id: int,
    type_: str,
    overlay_filename: str,
    raw_filename: Optional[str],
    size_bytes: int,
    duration_seconds: Optional[float],
    description: str,
    auto: bool,
    auto_trigger: Optional[str],
    camera_id: Optional[int],
) -> int:
    metadata_payload: dict[str, object] = {}
    if description:
        metadata_payload["description"] = description
    metadata = json.dumps(metadata_payload) if metadata_payload else None

    async with get_db() as conn:
        cursor = await conn.execute(
            "INSERT INTO recordings "
            "(user_id, type, filename, overlay_filename, raw_filename, "
            " size_bytes, duration_seconds, metadata, "
            " auto, auto_trigger, camera_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                user_id,
                type_,
                overlay_filename,
                overlay_filename,
                raw_filename,
                size_bytes,
                duration_seconds,
                metadata,
                bool(auto),
                auto_trigger,
                camera_id,
            ),
        )
        row = await cursor.fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
#  Snapshot
# ---------------------------------------------------------------------------

async def snapshot_from_hub(
    hub: FrameHub,
    user_id: int,
    *,
    description: str = "",
    auto: bool = False,
    auto_trigger: Optional[str] = None,
) -> int:
    """Save the latest processed + raw JPEG of `hub` as one image recording.

    Returns the new recording id. Raises HTTPException(503) if no frame is
    currently available, or 413 if the user's storage quota is full.
    """
    overlay_payload, _overlay_ts = hub.latest_processed()
    raw_payload, _raw_ts = hub.latest_raw()
    if overlay_payload is None and raw_payload is None:
        raise HTTPException(503, "No frame available from this camera yet")
    if overlay_payload is None:
        # Use the raw frame as the overlay too — viewers still get a picture.
        overlay_payload = raw_payload

    file_uuid = uuid.uuid4().hex
    overlay_filename = f"{file_uuid}.jpg"
    overlay_key = recording_key(user_id, overlay_filename)

    raw_filename: Optional[str] = None
    raw_key: Optional[str] = None
    raw_size = 0
    if raw_payload is not None and raw_payload is not overlay_payload:
        raw_filename = f"{file_uuid}_raw.jpg"
        raw_key = recording_key(user_id, raw_filename)
        raw_size = len(raw_payload)

    overlay_size = len(overlay_payload)
    total_size = overlay_size + raw_size

    # Reserve quota before writing; clean up partial writes on failure.
    cleanup: list[str] = [overlay_key] + ([raw_key] if raw_key else [])
    await _check_quota(user_id, total_size, cleanup)

    try:
        await put_bytes(overlay_key, overlay_payload, content_type="image/jpeg")
        if raw_key and raw_payload is not None:
            await put_bytes(raw_key, raw_payload, content_type="image/jpeg")
    except Exception:
        await delete_many(cleanup)
        raise

    recording_id = await _insert_recording(
        user_id=user_id,
        type_="image",
        overlay_filename=overlay_filename,
        raw_filename=raw_filename,
        size_bytes=total_size,
        duration_seconds=None,
        description=description.strip()[:1000],
        auto=auto,
        auto_trigger=auto_trigger,
        camera_id=hub.camera_id,
    )

    _audit(
        "recording.snapshot",
        user_id=user_id,
        id=recording_id,
        camera_id=hub.camera_id,
        auto=auto,
        auto_trigger=auto_trigger,
    )
    schedule_thumbnail_warmup([recording_id])
    return recording_id


# ---------------------------------------------------------------------------
#  Clip recording
# ---------------------------------------------------------------------------

async def _collect_jpegs(hub: FrameHub, lane: str, duration_s: int) -> list[bytes]:
    """Pull JPEGs from one lane of the hub for ~duration_s seconds.

    Uses the hub's wait_* methods so we get exactly one frame per published
    update. Capped by MAX_CLIP_FRAMES so a runaway publisher cannot OOM us.
    """
    frames: list[bytes] = []
    deadline = time.monotonic() + duration_s
    last_capture = 0
    wait_fn = hub.wait_processed if lane == "processed" else hub.wait_raw
    while time.monotonic() < deadline and len(frames) < MAX_CLIP_FRAMES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = await wait_fn(last_capture, timeout=min(remaining, 1.0))
        if result is None:
            continue
        payload, capture_ms = result
        last_capture = capture_ms
        frames.append(payload)
    return frames


async def _encode_mp4(jpegs: list[bytes], fps: int) -> bytes:
    """Pipe JPEGs through ffmpeg to produce an MP4 byte string."""
    if not jpegs:
        return b""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-framerate", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-f", "mp4",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def feed() -> None:
        try:
            for jpeg in jpegs:
                proc.stdin.write(jpeg)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    feed_task = asyncio.create_task(feed())
    stdout, stderr = await proc.communicate()
    await feed_task
    if proc.returncode != 0:
        msg = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}): {msg[:400]}")
    return stdout


async def record_clip_from_hub(
    hub: FrameHub,
    user_id: int,
    duration_s: int,
    *,
    description: str = "",
    auto: bool = False,
    auto_trigger: Optional[str] = None,
) -> int:
    """Record a `duration_s` clip from both lanes of the FrameHub.

    Both lanes are encoded as MP4. Returns the new recording id.
    """
    if duration_s not in ALLOWED_CLIP_DURATIONS:
        raise HTTPException(400, f"duration_s must be one of {ALLOWED_CLIP_DURATIONS}")

    # Sanity: only proceed if the hub has at least one fresh frame.
    if hub.latest_processed()[0] is None and hub.latest_raw()[0] is None:
        raise HTTPException(503, "No frame available from this camera yet")

    log.info("recording clip for user %d camera %d (%ds)", user_id, hub.camera_id, duration_s)
    processed_frames, raw_frames = await asyncio.gather(
        _collect_jpegs(hub, "processed", duration_s),
        _collect_jpegs(hub, "raw", duration_s),
    )

    if not processed_frames and not raw_frames:
        raise HTTPException(503, "Hub went silent during clip recording")

    # Encode each available lane in parallel.
    overlay_mp4_task = asyncio.create_task(
        _encode_mp4(processed_frames or raw_frames, CLIP_FPS)
    )
    raw_mp4_task = (
        asyncio.create_task(_encode_mp4(raw_frames, CLIP_FPS)) if raw_frames else None
    )
    overlay_mp4 = await overlay_mp4_task
    raw_mp4 = await raw_mp4_task if raw_mp4_task else b""

    if not overlay_mp4:
        raise HTTPException(500, "ffmpeg produced an empty overlay clip")

    file_uuid = uuid.uuid4().hex
    overlay_filename = f"{file_uuid}.mp4"
    overlay_key = recording_key(user_id, overlay_filename)
    raw_filename: Optional[str] = None
    raw_key: Optional[str] = None
    if raw_mp4:
        raw_filename = f"{file_uuid}_raw.mp4"
        raw_key = recording_key(user_id, raw_filename)

    total_size = len(overlay_mp4) + len(raw_mp4)
    cleanup: list[str] = [overlay_key] + ([raw_key] if raw_key else [])
    await _check_quota(user_id, total_size, cleanup)

    try:
        await put_bytes(overlay_key, overlay_mp4, content_type="video/mp4")
        if raw_key and raw_mp4:
            await put_bytes(raw_key, raw_mp4, content_type="video/mp4")
    except Exception:
        await delete_many(cleanup)
        raise

    recording_id = await _insert_recording(
        user_id=user_id,
        type_="video",
        overlay_filename=overlay_filename,
        raw_filename=raw_filename,
        size_bytes=total_size,
        duration_seconds=float(duration_s),
        description=description.strip()[:1000],
        auto=auto,
        auto_trigger=auto_trigger,
        camera_id=hub.camera_id,
    )

    _audit(
        "recording.clip",
        user_id=user_id,
        id=recording_id,
        camera_id=hub.camera_id,
        duration_s=duration_s,
        auto=auto,
        auto_trigger=auto_trigger,
        bytes=total_size,
    )
    schedule_thumbnail_warmup([recording_id])
    return recording_id
