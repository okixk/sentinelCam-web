"""In-memory frame distribution.

Each camera has its own :class:`FrameHub` that holds the latest "raw" frame
(direct from the Pi) and the latest "processed" frame (sent back by the
worker with the detection overlay). Browser MJPEG / WebRTC viewers wait on
the processed lane and fall back to the raw lane when no worker is online.

The hubs live for the lifetime of the process and are looked up by camera
id via :func:`frame_hubs.get`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional


log = logging.getLogger("sentinelCam.hub")


@dataclass
class _Slot:
    payload: Optional[bytes] = None
    capture_ms: int = 0
    received_at: float = 0.0
    waiter: asyncio.Event = field(default_factory=asyncio.Event)
    total_frames: int = 0


class FrameHub:
    """Stores the latest raw + processed JPEG for one camera."""

    __slots__ = ("camera_id", "_raw", "_processed", "_lock")

    def __init__(self, camera_id: int) -> None:
        self.camera_id = int(camera_id)
        self._raw = _Slot()
        self._processed = _Slot()
        self._lock = asyncio.Lock()

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
            # Wake everyone waiting on the previous frame, then arm a fresh
            # event so the next call to wait_next() blocks until publish.
            old, slot.waiter = slot.waiter, asyncio.Event()
            old.set()

    async def wait_processed(self, last_capture_ms: int, timeout: float = 5.0) -> Optional[tuple[bytes, int]]:
        return await self._wait(self._processed, last_capture_ms, timeout)

    async def wait_raw(self, last_capture_ms: int, timeout: float = 5.0) -> Optional[tuple[bytes, int]]:
        return await self._wait(self._raw, last_capture_ms, timeout)

    async def _wait(self, slot: _Slot, last_capture_ms: int, timeout: float) -> Optional[tuple[bytes, int]]:
        # Fast path: we already have a fresher frame.
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

    def stats(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "raw_frames": self._raw.total_frames,
            "processed_frames": self._processed.total_frames,
            "raw_last_at": self._raw.received_at or None,
            "processed_last_at": self._processed.received_at or None,
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
