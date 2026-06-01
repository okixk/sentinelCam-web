"""WebSocket ingest, worker channel, and MJPEG playback endpoints."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from app.auth.dependencies import User, check_csrf, get_current_user
from app.auto_capture import handle_detection
from app.database import get_db
from app.recording.live_capture import (
    ALLOWED_CLIP_DURATIONS,
    record_clip_from_hub,
    snapshot_from_hub,
)
from app.streaming import fmp4
from app.streaming.h264 import is_keyframe, looks_like_annexb
from app.streaming.hub import frame_hubs
from app.streaming.protocol import (
    HEADER_LEN,
    PROTOCOL_VERSION,
    MSG_PROCESSED_FRAME,
    MSG_PROCESSED_H264,
    MSG_RAW_FRAME,
    Frame,
    decode,
    encode,
    now_ms,
)
from app.streaming.tokens import (
    record_camera_frame,
    update_worker_status,
    verify_camera_token,
    verify_worker_token,
)
from app.streaming.worker_link import worker_link


log = logging.getLogger("sentinelCam.streaming")
AUDIT = logging.getLogger("sentinelCam.audit")
router = APIRouter(tags=["streaming"])


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

_MAX_FRAME_BYTES = 4 * 1024 * 1024  # 4 MiB ceiling per JPEG, well above 1080p
_MAX_H264_AU_BYTES = 2 * 1024 * 1024  # 2 MiB ceiling per H.264 access unit


def _extract_bearer(ws: WebSocket) -> str:
    auth = (ws.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Allow ?token=... as a fallback because the browser/Pi clients that drive
    # WebSocket connections cannot always set custom headers easily.
    return (ws.query_params.get("token") or "").strip()


def _looks_like_jpeg(payload: bytes) -> bool:
    return len(payload) >= 3 and payload[:3] == b"\xff\xd8\xff"


# ---------------------------------------------------------------------------
#  Pi ingest WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/api/ingest/{cam_id}")
async def ingest(ws: WebSocket, cam_id: int) -> None:
    token = _extract_bearer(ws)
    if not token:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    verified_id = await verify_camera_token(token)
    if verified_id is None or verified_id != cam_id:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    hub = await frame_hubs.get_or_create(cam_id)
    log.info("camera %d connected", cam_id)
    frames_seen = 0
    try:
        while True:
            payload = await ws.receive_bytes()
            if len(payload) > _MAX_FRAME_BYTES or not _looks_like_jpeg(payload):
                # Silently drop bad frames; do not tear down the channel for
                # the occasional partial frame on flaky links.
                continue
            capture_ms = now_ms()
            await hub.publish_raw(payload, capture_ms)
            envelope = encode(MSG_RAW_FRAME, cam_id, capture_ms, payload)
            # Best-effort forward to the worker; if the worker is offline the
            # frame still lives in the raw hub and viewers can see it directly.
            await worker_link.dispatch_to_worker(envelope)
            frames_seen += 1
            if frames_seen % 30 == 0:
                # Update DB stats roughly once per second @ 30 fps.
                try:
                    await record_camera_frame(cam_id, len(payload))
                except Exception:
                    log.exception("record_camera_frame failed")
    except WebSocketDisconnect:
        log.info("camera %d disconnected after %d frames", cam_id, frames_seen)
    except Exception:
        log.exception("camera %d connection errored after %d frames", cam_id, frames_seen)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  Worker channel WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/api/worker/connect")
async def worker_connect(ws: WebSocket) -> None:
    token = _extract_bearer(ws)
    if not token:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    worker_id = await verify_worker_token(token)
    if worker_id is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    evicted = worker_link.current()
    conn = await worker_link.attach(worker_id, ws)
    log.info("worker %d connected", worker_id)
    AUDIT.info(json.dumps({
        "event": "worker.connect",
        "worker_id": worker_id,
        # Surface a takeover: a leaked token connecting evicts the live worker.
        "evicted_worker_id": evicted.worker_id if evicted else None,
        "timestamp": time.time(),
    }))
    try:
        # Initial hello so the worker knows the channel is up (+ protocol version).
        await ws.send_text(json.dumps({
            "type": "hello", "worker_id": worker_id, "proto": PROTOCOL_VERSION, "ts": time.time(),
        }))

        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break

            data = message.get("bytes")
            if data is not None:
                try:
                    frame = decode(data)
                except ValueError:
                    continue
                await _handle_worker_binary(frame)
                continue

            text = message.get("text")
            if text is not None:
                await _handle_worker_text(conn, text)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("worker %d connection errored", worker_id)
    finally:
        await worker_link.detach(conn)
        log.info("worker %d disconnected", worker_id)
        try:
            await ws.close()
        except Exception:
            pass


async def _handle_worker_binary(frame: Frame) -> None:
    # Validate payloads server-side: a leaked worker token must not be able to
    # inject oversized or malformed frames to spoof feeds / exhaust memory.
    if frame.msg_type == MSG_PROCESSED_FRAME:
        if len(frame.payload) > _MAX_FRAME_BYTES or not _looks_like_jpeg(frame.payload):
            return
        hub = frame_hubs.get(frame.camera_id) or await frame_hubs.get_or_create(frame.camera_id)
        await hub.publish_processed(frame.payload, frame.capture_ms)
    elif frame.msg_type == MSG_PROCESSED_H264:
        if len(frame.payload) > _MAX_H264_AU_BYTES or not looks_like_annexb(frame.payload):
            return
        hub = frame_hubs.get(frame.camera_id) or await frame_hubs.get_or_create(frame.camera_id)
        hub.publish_h264(frame.payload, is_keyframe(frame.payload))


async def _handle_worker_text(conn, text: str) -> None:
    try:
        msg = json.loads(text)
    except ValueError:
        return
    if not isinstance(msg, dict):
        return
    peer_proto = msg.get("proto")
    if peer_proto is not None and not getattr(conn, "_proto_warned", False):
        try:
            if int(peer_proto) != PROTOCOL_VERSION:
                log.warning(
                    "worker %s protocol mismatch: worker=%s web=%s — frames may mis-decode",
                    conn.worker_id, peer_proto, PROTOCOL_VERSION,
                )
        except (TypeError, ValueError):
            pass
        conn._proto_warned = True  # type: ignore[attr-defined]
    kind = str(msg.get("type") or "").lower()
    if kind == "heartbeat":
        conn.last_heartbeat_at = time.time()
        status_payload = msg.get("status") or {}
        if isinstance(status_payload, dict):
            conn.last_status = status_payload
            try:
                await update_worker_status(conn.worker_id, json.dumps(status_payload))
            except Exception:
                log.exception("update_worker_status failed")
        return
    if kind == "detection":
        # {"type":"detection","camera_id":<int>,"classes":["person",...]}
        try:
            cam_id = int(msg.get("camera_id"))
        except (TypeError, ValueError):
            return
        raw_classes = msg.get("classes") or []
        if not isinstance(raw_classes, list):
            return
        classes = [str(c) for c in raw_classes if isinstance(c, str)]
        if not classes:
            return
        try:
            await handle_detection(cam_id, classes)
        except Exception:
            log.exception("handle_detection failed for camera %d", cam_id)


# ---------------------------------------------------------------------------
#  MJPEG output to the browser
# ---------------------------------------------------------------------------

_MJPEG_BOUNDARY = "sentinelcam"
_MJPEG_IDLE_TIMEOUT = 30.0  # close the stream after this long without a frame


async def _mjpeg_generator(cam_id: int) -> AsyncIterator[bytes]:
    hub = frame_hubs.get(cam_id)
    if hub is None:
        return
    last_capture = 0
    idle_started = time.time()
    while True:
        result = await hub.wait_processed(last_capture, timeout=1.0)
        if result is None:
            result = await hub.wait_raw(last_capture, timeout=0.0)
        if result is None:
            if time.time() - idle_started > _MJPEG_IDLE_TIMEOUT:
                return
            await asyncio.sleep(0.05)
            continue
        idle_started = time.time()
        payload, last_capture = result
        header = (
            f"--{_MJPEG_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode("ascii")
        yield header + payload + b"\r\n"


@router.get("/api/cameras/{cam_id}/stream.mjpg")
async def camera_mjpeg(cam_id: int, user: User = Depends(get_current_user)) -> StreamingResponse:
    hub = frame_hubs.get(cam_id)
    if hub is None or (hub.latest_processed()[0] is None and hub.latest_raw()[0] is None):
        raise HTTPException(404, "Camera offline")
    response = StreamingResponse(
        _mjpeg_generator(cam_id),
        media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@router.get("/api/cameras/{cam_id}/frame.jpg")
async def camera_latest_frame(cam_id: int, user: User = Depends(get_current_user)):
    hub = frame_hubs.get(cam_id)
    if hub is None:
        raise HTTPException(404, "Camera offline")
    payload, _ts = hub.latest_processed()
    if payload is None:
        payload, _ts = hub.latest_raw()
    if payload is None:
        raise HTTPException(404, "No frame yet")
    from fastapi.responses import Response
    return Response(
        content=payload,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# ---------------------------------------------------------------------------
#  Viewer-facing camera listing (no secrets)
# ---------------------------------------------------------------------------

@router.get("/api/cameras")
async def list_active_cameras(user: User = Depends(get_current_user)):
    """Return the active cameras a viewer is allowed to subscribe to.

    Strips token hashes; only id, name, last_frame_at, and a derived "live"
    flag are returned. Admins still get the full picture via /api/admin/cameras.
    """
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id, name, last_frame_at FROM cameras WHERE revoked_at IS NULL ORDER BY id"
        )
        rows = await cursor.fetchall()
    items = []
    for r in rows:
        cam_id = int(r["id"])
        hub = frame_hubs.get(cam_id)
        has_frames = hub is not None and (hub.latest_processed()[0] is not None or hub.latest_raw()[0] is not None)
        items.append(
            {
                "id": cam_id,
                "name": r["name"],
                "last_frame_at": r["last_frame_at"],
                "live": bool(has_frames),
            }
        )
    return JSONResponse({"items": items})


# ---------------------------------------------------------------------------
#  Manual snapshot / clip recording from the live stream
# ---------------------------------------------------------------------------

class _SnapshotBody(BaseModel):
    description: str = ""

    @field_validator("description")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()[:1000]


class _ClipBody(BaseModel):
    duration_s: int
    description: str = ""

    @field_validator("duration_s")
    @classmethod
    def _check_duration(cls, value: int) -> int:
        if value not in ALLOWED_CLIP_DURATIONS:
            raise ValueError(f"duration_s must be one of {ALLOWED_CLIP_DURATIONS}")
        return value

    @field_validator("description")
    @classmethod
    def _trim(cls, value: str) -> str:
        return (value or "").strip()[:1000]


@router.post("/api/cameras/{cam_id}/snapshot", status_code=201)
async def camera_snapshot(
    cam_id: int,
    body: _SnapshotBody,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    hub = frame_hubs.get(cam_id)
    if hub is None:
        raise HTTPException(404, "Camera offline")
    recording_id = await snapshot_from_hub(hub, user.id, description=body.description)
    return JSONResponse({"ok": True, "id": recording_id}, status_code=201)


@router.post("/api/cameras/{cam_id}/clip", status_code=201)
async def camera_clip(
    cam_id: int,
    body: _ClipBody,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    hub = frame_hubs.get(cam_id)
    if hub is None:
        raise HTTPException(404, "Camera offline")
    recording_id = await record_clip_from_hub(
        hub, user.id, body.duration_s, description=body.description
    )
    return JSONResponse({"ok": True, "id": recording_id}, status_code=201)


# ---------------------------------------------------------------------------
#  Low-latency live view: fragmented-MP4 (H.264) over HTTPS, MSE in the browser
# ---------------------------------------------------------------------------

@router.get("/api/cameras/{cam_id}/live.mp4")
async def camera_live_mp4(cam_id: int, user: User = Depends(get_current_user)) -> StreamingResponse:
    """Stream the worker's H.264 lane to the browser as fragmented-MP4.

    The web server never decodes/re-encodes (ffmpeg ``-c:v copy``); all encode
    cost stays on the worker's GPU. Returns 404 when no H.264 is flowing so the
    browser can fall back to MJPEG.
    """
    hub = frame_hubs.get(cam_id)
    if hub is None or not hub.has_h264():
        raise HTTPException(404, "No live H.264 stream for this camera")
    if fmp4.at_capacity():
        raise HTTPException(503, "max concurrent live viewers reached")
    response = StreamingResponse(fmp4.live_mp4(hub), media_type="video/mp4")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
