"""In-memory frame distribution.

Each camera has its own :class:`FrameHub` holding three lanes:

- ``raw``       — the latest JPEG straight from the camera/edge (fallback view),
- ``processed`` — the latest JPEG sent back by the worker with the detection
  overlay (used for snapshots, clips and the MJPEG fallback),
- ``h264``      — a short rolling buffer of H.264 Annex-B access units sent back
  by the worker, used for the low-latency live view. The web server relays these
  to browsers as fragmented-MP4 (``ffmpeg -c:v copy``) without re-encoding.

The hubs live for the lifetime of the process and are looked up by camera id
via :func:`frame_hubs.get`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional


log = logging.getLogger("sentinelCam.hub")


@dataclass
class _Slot:
    payload: Optional[bytes] = None
    capture_ms: int = 0
    received_at: float = 0.0
    waiter: asyncio.Event = field(default_factory=asyncio.Event)
    total_frames: int = 0


class _H264Lane:
    """Rolling buffer of H.264 access units with keyframe-aware subscription.

    Keeps the last ``maxlen`` access units so a freshly-joined viewer can start
    at the most recent keyframe without waiting a full GOP. The web also asks
    the worker for a fresh keyframe on join, so this buffer is mostly a jitter
    cushion, not a long backlog.
    """

    __slots__ = ("_buf", "_seq", "_waiter", "received_at", "total", "source")

    def __init__(self, maxlen: int = 300) -> None:
        self._buf: deque[tuple[int, bytes, bool]] = deque(maxlen=maxlen)
        self._seq = 0
        self._waiter = asyncio.Event()
        self.received_at = 0.0
        self.total = 0
        # Which producer currently owns the lane: "edge" (camera-encoded H.264
        # arriving on /api/ingest) or "worker" (annotated re-encode). Exactly
        # one producer may feed the lane at a time — interleaving two encoders
        # (different SPS/PPS, resolution, timestamps) corrupts the ffmpeg
        # ``-c:v copy`` mux downstream.
        self.source: Optional[str] = None

    def fresh(self) -> bool:
        return self.total > 0 and (time.time() - self.received_at) < 10.0

    def publish(self, access_unit: bytes, is_keyframe: bool, source: str = "worker") -> None:
        if not access_unit:
            return
        if source != self.source:
            # The edge wins while its stream is fresh: when a camera sends
            # H.264 directly, the worker only sees the low-fps detection
            # sidecar and its re-encode must not displace the real stream.
            if self.source == "edge" and source == "worker" and self.fresh():
                return
            # Taking over (or the previous producer went stale): drop the old
            # buffer so a new subscriber never sees AUs from two encoders.
            self._buf.clear()
            self.source = source
        self._seq += 1
        self._buf.append((self._seq, access_unit, bool(is_keyframe)))
        self.total += 1
        self.received_at = time.time()
        old, self._waiter = self._waiter, asyncio.Event()
        old.set()

    def _last_keyframe_tail(self) -> tuple[list[tuple[int, bytes]], int]:
        items = list(self._buf)
        kf_idx = None
        for i in range(len(items) - 1, -1, -1):
            if items[i][2]:
                kf_idx = i
                break
        if kf_idx is None:
            return [], (items[-1][0] if items else 0)
        tail = items[kf_idx:]
        return [(s, au) for (s, au, _kf) in tail], tail[-1][0]

    async def _after(self, last_seq: int, timeout: float) -> tuple[list[tuple[int, bytes, bool]], int]:
        fresh = [t for t in self._buf if t[0] > last_seq]
        if fresh:
            return fresh, fresh[-1][0]
        waiter = self._waiter
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return [], last_seq
        fresh = [t for t in self._buf if t[0] > last_seq]
        return fresh, (fresh[-1][0] if fresh else last_seq)

    async def subscribe(self, *, idle_timeout: float = 10.0) -> AsyncIterator[bytes]:
        """Yield Annex-B access units, starting from a keyframe.

        Emits the current keyframe tail immediately if one is buffered;
        otherwise waits for the next keyframe (skipping leading delta frames so
        the downstream ffmpeg copy always starts on an IDR with parameter sets).
        Stops if no new unit arrives within ``idle_timeout`` seconds.
        """
        tail, last_seq = self._last_keyframe_tail()
        seen_keyframe = bool(tail)
        for _seq, au in tail:
            yield au

        while True:
            fresh, last_seq = await self._after(last_seq, timeout=idle_timeout)
            if not fresh:
                # idle timeout — nothing new; let the caller decide to stop.
                return
            for _seq, au, is_kf in fresh:
                if not seen_keyframe:
                    if not is_kf:
                        continue
                    seen_keyframe = True
                yield au


class FrameHub:
    """Stores the latest raw + processed JPEG and a rolling H.264 lane."""

    __slots__ = ("camera_id", "_raw", "_processed", "_h264", "_lock")

    def __init__(self, camera_id: int) -> None:
        self.camera_id = int(camera_id)
        self._raw = _Slot()
        self._processed = _Slot()
        self._h264 = _H264Lane()
        self._lock = asyncio.Lock()

    # ----- JPEG lanes -----------------------------------------------------

    async def publish_raw(self, payload: bytes, capture_ms: int) -> None:
        await self._publish(self._raw, payload, capture_ms)

    async def publish_processed(self, payload: bytes, capture_ms: int) -> None:
        await self._publish(self._processed, payload, capture_ms)

    async def _publish(self, slot: _Slot, payload: bytes, capture_ms: int) -> None:
        if not payload:
            return
        async with self._lock:
            slot.payload = payload
            slot.capture_ms = int(capture_ms)
            slot.received_at = time.time()
            slot.total_frames += 1
            old, slot.waiter = slot.waiter, asyncio.Event()
            old.set()

    async def wait_processed(self, last_capture_ms: int, timeout: float = 5.0) -> Optional[tuple[bytes, int]]:
        return await self._wait(self._processed, last_capture_ms, timeout)

    async def wait_raw(self, last_capture_ms: int, timeout: float = 5.0) -> Optional[tuple[bytes, int]]:
        return await self._wait(self._raw, last_capture_ms, timeout)

    async def _wait(self, slot: _Slot, last_capture_ms: int, timeout: float) -> Optional[tuple[bytes, int]]:
        if slot.payload is not None and slot.capture_ms > last_capture_ms:
            return slot.payload, slot.capture_ms
        waiter = slot.waiter
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if slot.payload is None:
            return None
        return slot.payload, slot.capture_ms

    def latest_processed(self) -> tuple[Optional[bytes], int]:
        return self._processed.payload, self._processed.capture_ms

    def latest_raw(self) -> tuple[Optional[bytes], int]:
        return self._raw.payload, self._raw.capture_ms

    # ----- H.264 lane -----------------------------------------------------

    def publish_h264(self, access_unit: bytes, is_keyframe: bool, source: str = "worker") -> None:
        self._h264.publish(access_unit, is_keyframe, source)

    def subscribe_h264(self, *, idle_timeout: float = 10.0) -> AsyncIterator[bytes]:
        return self._h264.subscribe(idle_timeout=idle_timeout)

    def has_h264(self) -> bool:
        return self._h264.fresh()

    # ----- stats ----------------------------------------------------------

    def stats(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "raw_frames": self._raw.total_frames,
            "processed_frames": self._processed.total_frames,
            "h264_units": self._h264.total,
            "h264_source": self._h264.source,
            "raw_last_at": self._raw.received_at or None,
            "processed_last_at": self._processed.received_at or None,
            "h264_last_at": self._h264.received_at or None,
            "raw_bytes_last": len(self._raw.payload) if self._raw.payload else 0,
            "processed_bytes_last": len(self._processed.payload) if self._processed.payload else 0,
        }


class _FrameHubs:
    """Process-wide registry of per-camera hubs."""

    def __init__(self) -> None:
        self._hubs: dict[int, FrameHub] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, camera_id: int) -> FrameHub:
        async with self._lock:
            hub = self._hubs.get(int(camera_id))
            if hub is None:
                hub = FrameHub(camera_id)
                self._hubs[int(camera_id)] = hub
            return hub

    def get(self, camera_id: int) -> Optional[FrameHub]:
        return self._hubs.get(int(camera_id))

    def all_stats(self) -> list[dict[str, object]]:
        return [hub.stats() for hub in self._hubs.values()]


frame_hubs = _FrameHubs()
