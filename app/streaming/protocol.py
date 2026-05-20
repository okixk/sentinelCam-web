"""Binary envelope for the web <-> worker WebSocket channel.

Both directions share the same 17-byte prefix:

    [1 byte  : message type]
    [8 bytes : camera id, unsigned big-endian]
    [8 bytes : capture timestamp in milliseconds since epoch, unsigned big-endian]
    [N bytes : payload — for image messages, a JPEG bitstream]

Message types currently in use:

    0x01 RAW_FRAME       web -> worker — raw JPEG straight from the Pi
    0x02 PROCESSED_FRAME worker -> web — JPEG with detection overlay
    0x03 KEYFRAME_REQ    web -> worker — hint that a fresh keyframe is needed

Control messages (heartbeats, settings updates, status) are sent as JSON text
frames over the same WebSocket connection, not via this binary envelope.
"""
from __future__ import annotations

import struct
import time
from typing import Final, NamedTuple

_HEADER = struct.Struct(">BQQ")
HEADER_LEN: Final[int] = _HEADER.size  # 17

MSG_RAW_FRAME: Final[int] = 0x01
MSG_PROCESSED_FRAME: Final[int] = 0x02
MSG_KEYFRAME_REQ: Final[int] = 0x03


class Frame(NamedTuple):
    msg_type: int
    camera_id: int
    capture_ms: int
    payload: bytes

    @property
    def capture_ts(self) -> float:
        return self.capture_ms / 1000.0


def now_ms() -> int:
    return int(time.time() * 1000)


def encode(msg_type: int, camera_id: int, capture_ms: int, payload: bytes) -> bytes:
    return _HEADER.pack(int(msg_type) & 0xFF, int(camera_id), int(capture_ms)) + payload


def decode(data: bytes) -> Frame:
    if len(data) < HEADER_LEN:
        raise ValueError(f"frame too short ({len(data)} bytes, need {HEADER_LEN})")
    msg_type, camera_id, capture_ms = _HEADER.unpack(data[:HEADER_LEN])
    return Frame(msg_type=msg_type, camera_id=camera_id, capture_ms=capture_ms, payload=data[HEADER_LEN:])
