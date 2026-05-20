"""Tiny in-process observability primitives for the admin status page.

We do not want to pull in Prometheus / OTLP / external sinks for this small
stack. Two things are enough to triage a misbehaving deployment:

- a rolling buffer of the most recent application errors (so an admin can
  see "the last 50 things that went wrong" without SSH-ing into the host),
- a few process metrics that are cheap to read every time the status
  endpoint is hit.

The buffer is wired to the root logger via :class:`RingBufferHandler`. It is
deliberately tiny and bounded; this is operator-visible state, not telemetry.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable


_BUFFER_LIMIT = 200


@dataclass(frozen=True)
class ErrorEvent:
    timestamp: float
    level: str
    logger: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
        }


@dataclass
class _Buffer:
    events: Deque[ErrorEvent] = field(default_factory=lambda: deque(maxlen=_BUFFER_LIMIT))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, event: ErrorEvent) -> None:
        with self.lock:
            self.events.append(event)

    def snapshot(self) -> list[ErrorEvent]:
        with self.lock:
            return list(self.events)

    def reset(self) -> None:
        with self.lock:
            self.events.clear()


_BUFFER = _Buffer()


class RingBufferHandler(logging.Handler):
    """Logging handler that keeps WARNING+ records in :data:`_BUFFER`."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin shim
        try:
            message = record.getMessage()
        except Exception:
            message = record.msg if isinstance(record.msg, str) else repr(record.msg)
        if record.exc_info:
            try:
                message = f"{message} | {self.format(record).splitlines()[-1]}"
            except Exception:
                pass
        _BUFFER.append(
            ErrorEvent(
                timestamp=record.created,
                level=record.levelname,
                logger=record.name,
                message=message[:500],
            )
        )


def install_error_capture(min_level: int = logging.WARNING) -> RingBufferHandler:
    """Attach the ring-buffer handler to the root logger exactly once."""
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, RingBufferHandler):
            return existing
    handler = RingBufferHandler(level=min_level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > min_level:
        root.setLevel(min_level)
    return handler


def recent_errors(limit: int = 50) -> list[dict[str, object]]:
    events = _BUFFER.snapshot()
    if limit and limit > 0:
        events = events[-limit:]
    return [event.as_dict() for event in reversed(events)]


def reset_error_buffer() -> None:
    """Test-only helper."""
    _BUFFER.reset()


# ---------------------------------------------------------------------------
#  Cheap process-side metrics
# ---------------------------------------------------------------------------

_PROCESS_STARTED_AT = time.time()


def process_uptime_seconds() -> float:
    return max(0.0, time.time() - _PROCESS_STARTED_AT)


def disk_usage(path: str) -> dict[str, int | str]:
    """Return total/used/free bytes for the filesystem hosting ``path``."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": path,
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
        "free_bytes": int(usage.free),
    }


def directory_size_bytes(path: str, *, max_entries: int = 50_000) -> int | None:
    """Best-effort sum of file sizes under ``path``. Returns None on error.

    Uses os.scandir to avoid stat-ing each child twice; caps recursion via the
    ``max_entries`` budget so a misconfigured storage tree cannot wedge the
    status endpoint.
    """
    if not path or not os.path.isdir(path):
        return None
    total = 0
    stack: list[str] = [path]
    visited = 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    visited += 1
                    if visited > max_entries:
                        return total
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total
