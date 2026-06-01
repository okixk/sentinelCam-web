"""Binary envelope for the web <-> worker WebSocket channel.

This file is kept byte-for-byte identical to
``sentinelCam-worker/web_pipeline/protocol.py``. A contract test
(``tests/test_protocol_contract.py`` in each repo) guards against drift, and
the JSON control handshake carries ``PROTOCOL_VERSION`` so a version skew fails
loudly instead of mis-decoding frames.

The 17-byte binary prefix is shared by every image/video message:

    [1 byte  : message type]
    [8 bytes : camera id, unsigned big-endian]
    [8 bytes : capture timestamp in milliseconds since epoch, unsigned big-endian]
    [N bytes : payload]

Message types:

    0x01 RAW_FRAME        web -> worker   raw JPEG straight from the camera/edge
    0x02 PROCESSED_FRAME  worker -> web   JPEG with overlay (snapshots / MJPEG / clips)
    0x03 KEYFRAME_REQ     web -> worker   ask the worker to emit an H.264 IDR now
    0x04 PROCESSED_H264   worker -> web   one H.264 access unit (Annex-B) with overlay (live lane)

Control messages (hello, heartbeat, detection) are JSON text frames, not this
binary envelope. They carry a ``"proto"`` field equal to ``PROTOCOL_VERSION``.
"""
from __future__ import annotations

import struct
import time
from typing import Final, NamedTuple

# Bumped to 2 when MSG_PROCESSED_H264 / the H.264 live lane was introduced.
PROTOCOL_VERSION: Final[int] = 2

_HEADER = struct.Struct(">BQQ")
HEADER_LEN: Final[int] = _HEADER.size  # 17

MSG_RAW_FRAME: Final[int] = 0x01
MSG_PROCESSED_FRAME: Final[int] = 0x02
MSG_KEYFRAME_REQ: Final[int] = 0x03
MSG_PROCESSED_H264: Final[int] = 0x04


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
