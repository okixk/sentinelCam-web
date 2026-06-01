"""Single-worker connection state.

The architecture today has exactly one worker dialing into the web server.
This module keeps track of that connection so that other parts of the app
(the ingest path that needs to push raw frames out to the worker, the
admin status page that wants to know "is the worker online?") can talk to
it without holding a reference to the WebSocket directly.

When multi-worker support lands, this becomes a dict keyed by worker id.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket


log = logging.getLogger("sentinelCam.worker")

# A worker that has not sent a heartbeat within this many seconds is treated as
# wedged: it is reported "stale" and the watchdog closes the channel so the
# admin UI / fallback path see it as down instead of a frozen "online".
STALE_AFTER_S = 15.0


@dataclass
class WorkerConnection:
    worker_id: int
    websocket: WebSocket
    connected_at: float
    last_heartbeat_at: float = 0.0
    last_status: dict[str, Any] = field(default_factory=dict)

    async def send_bytes(self, data: bytes) -> bool:
        try:
            await self.websocket.send_bytes(data)
            return True
        except Exception as exc:
            log.warning("send to worker %d failed: %s", self.worker_id, exc)
            return False


class WorkerLink:
    """Process-wide registry of the active worker connection."""

    def __init__(self) -> None:
        self._current: Optional[WorkerConnection] = None
        self._lock = asyncio.Lock()

    async def attach(self, worker_id: int, websocket: WebSocket) -> WorkerConnection:
        async with self._lock:
            if self._current is not None:
                # Only one worker at a time. Drop the older one cleanly.
                log.info("evicting previous worker connection %d", self._current.worker_id)
                try:
                    await self._current.websocket.close(code=4000)
                except Exception:
                    pass
            conn = WorkerConnection(
                worker_id=worker_id,
                websocket=websocket,
                connected_at=time.time(),
            )
            self._current = conn
            return conn

    async def detach(self, conn: WorkerConnection) -> None:
        async with self._lock:
            if self._current is conn:
                self._current = None

    def current(self) -> Optional[WorkerConnection]:
        return self._current

    async def dispatch_to_worker(self, data: bytes) -> bool:
        conn = self._current
        if conn is None:
            return False
        return await conn.send_bytes(data)

    def status(self) -> dict[str, Any]:
        conn = self._current
        if conn is None:
            return {"connected": False, "alive": False}
        return {
            "connected": True,
            "alive": not self._is_stale(conn),
            "worker_id": conn.worker_id,
            "connected_at": conn.connected_at,
            "last_heartbeat_at": conn.last_heartbeat_at or None,
            "seconds_since_heartbeat": self._seconds_since_heartbeat(conn),
            "last_status": dict(conn.last_status),
        }

    @staticmethod
    def _seconds_since_heartbeat(conn: WorkerConnection) -> Optional[float]:
        ref = conn.last_heartbeat_at or conn.connected_at
        if not ref:
            return None
        return round(time.time() - ref, 1)

    @staticmethod
    def _is_stale(conn: WorkerConnection) -> bool:
        ref = conn.last_heartbeat_at or conn.connected_at
        return bool(ref) and (time.time() - ref) > STALE_AFTER_S

    async def close_if_stale(self) -> bool:
        """Close + detach the worker if its heartbeat has gone stale.

        Returns True if a stale worker was evicted. Intended to be polled by a
        background watchdog task.
        """
        conn = self._current
        if conn is None or not self._is_stale(conn):
            return False
        log.warning("worker %d heartbeat stale (>%.0fs); closing channel", conn.worker_id, STALE_AFTER_S)
        try:
            await conn.websocket.close(code=4001)
        except Exception:
            pass
        await self.detach(conn)
        return True


worker_link = WorkerLink()
