"""Capture from a live FrameHub into a Recording.

Two entry points:

- :func:`snapshot_from_hub` grabs the most recent processed and raw JPEG
  for a camera and stores both as a single ``image`` recording.
- :func:`record_clip_from_hub` records a fixed duration into MP4s — one for
  the overlay lane, one for the raw lane — so the gallery's existing
  overlay-toggle plays both back. JPEG lanes are sampled at a fixed output
  fps (repeating frames when a lane is slower, e.g. the 2 fps detection
  sidecar of an edge-H.264 camera). When the camera streams H.264 directly,
  the raw clip is stream-copied from the H.264 lane at full quality instead.

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

async def _record_lane_mp4(hub: FrameHub, lane: str, duration_s: int, fps: int) -> bytes:
    """Sample one lane's JPEGs at `fps` straight into ffmpeg → MP4 bytes.

    Each output tick takes the lane's freshest frame, repeating it if the
    lane is slower than `fps` — so the clip always plays back in real time,
    even for the ~2 fps detection sidecar of an edge-H.264 camera (a naive
    as-they-arrive feed muxed at a fixed framerate turns that into a 7x
    time-lapse). Memory stays at O(1 frame); capped by MAX_CLIP_FRAMES.
    Returns b"" if the lane never produced a frame.
    """
    getter = hub.latest_processed if lane == "processed" else hub.latest_raw
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
        # Fragmented MP4 muxes cleanly to a non-seekable pipe (moov up front),
        # so ffmpeg doesn't buffer the whole clip in RAM to relocate the atom
        # (which +faststart would force here). Decodable by cv2 + <video>.
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    written = 0

    async def feed() -> None:
        nonlocal written
        deadline = time.monotonic() + duration_s
        period = 1.0 / max(fps, 1)
        next_tick = time.monotonic()
        try:
            while time.monotonic() < deadline and written < MAX_CLIP_FRAMES:
                payload, _capture_ms = getter()
                if payload is not None:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                    written += 1
                next_tick += period
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                else:
                    next_tick = time.monotonic()
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
    if written == 0:
        return b""
    if proc.returncode != 0:
        msg = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}): {msg[:400]}")
    return stdout


# Safety cap for stream-copied H.264 clips (60s at ~25 Mbit/s would be ~190MB).
_MAX_H264_CLIP_BYTES = 200 * 1024 * 1024


async def _record_h264_mp4(hub: FrameHub, duration_s: int) -> bytes:
    """Stream-copy the camera's H.264 lane into an MP4 for `duration_s`.

    For edge-H.264 cameras this is the only full-framerate video — the JPEG
    lanes carry just the low-fps detection sidecar. ``-c:v copy`` keeps the
    full 1080p30 quality at near-zero CPU cost. Starts at a keyframe (the hub
    subscription guarantees it). Returns b"" if the lane produced nothing.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        # Same demuxer settings as the fMP4 live relay: small probe for fast
        # start; no +nobuffer (it silently drops ~1/3 of the frames).
        "-probesize", "32768",
        "-analyzeduration", "0",
        "-use_wallclock_as_timestamps", "1",
        "-f", "h264",
        "-i", "-",
        "-an",
        "-c:v", "copy",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    written = 0

    async def feed() -> None:
        nonlocal written
        deadline = time.monotonic() + duration_s
        try:
            async for au in hub.subscribe_h264(idle_timeout=2.0):
                if time.monotonic() >= deadline or written >= _MAX_H264_CLIP_BYTES:
                    break
                proc.stdin.write(au)
                await proc.stdin.drain()
                written += len(au)
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
    if written == 0:
        return b""
    if proc.returncode != 0:
        msg = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg copy failed (rc={proc.returncode}): {msg[:400]}")
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
    if (
        hub.latest_processed()[0] is None
        and hub.latest_raw()[0] is None
        and not hub.has_h264()
    ):
        raise HTTPException(503, "No frame available from this camera yet")

    log.info("recording clip for user %d camera %d (%ds)", user_id, hub.camera_id, duration_s)
    # Record both lanes concurrently, streaming frames into ffmpeg as they
    # arrive so memory stays bounded regardless of duration/concurrency.
    if hub.has_h264():
        # Edge-H.264 camera: the full-framerate video lives in the H.264 lane
        # (stream-copied, no re-encode). The processed JPEG lane (the worker's
        # annotated sidecar) still provides the overlay clip.
        processed_mp4, h264_mp4 = await asyncio.gather(
            _record_lane_mp4(hub, "processed", duration_s, CLIP_FPS),
            _record_h264_mp4(hub, duration_s),
        )
        if processed_mp4:
            overlay_mp4 = processed_mp4
            raw_mp4 = h264_mp4
        else:
            overlay_mp4 = h264_mp4
            raw_mp4 = b""
    else:
        processed_mp4, raw_lane_mp4 = await asyncio.gather(
            _record_lane_mp4(hub, "processed", duration_s, CLIP_FPS),
            _record_lane_mp4(hub, "raw", duration_s, CLIP_FPS),
        )
        if processed_mp4:
            overlay_mp4 = processed_mp4
            raw_mp4 = raw_lane_mp4  # may be b"" if the raw lane was silent
        else:
            # No processed lane (worker offline): use the raw lane as the
            # overlay and do not store a duplicate raw file.
            overlay_mp4 = raw_lane_mp4
            raw_mp4 = b""

    if not overlay_mp4:
        raise HTTPException(503, "No frames available from this camera during recording")

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
