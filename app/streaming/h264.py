"""Minimal H.264 Annex-B helpers for the web relay.

Pure-Python (no PyAV / ffmpeg needed) so it is cheap and unit-testable. Kept in
shape-parity with ``sentinelCam-worker/web_pipeline/encoder.py``.
"""
from __future__ import annotations

from typing import Iterator


def iter_nal_units(buf: bytes) -> Iterator[bytes]:
    """Yield raw NAL unit payloads (without start codes) from an Annex-B buffer."""
    i = 0
    n = len(buf)
    while i < n:
        start = buf.find(b"\x00\x00\x01", i)
        if start == -1:
            break
        nal_start = start + 3
        nxt = buf.find(b"\x00\x00\x01", nal_start)
        if nxt == -1:
            yield buf[nal_start:]
            break
        end = nxt
        if end > 0 and buf[end - 1] == 0:  # 4-byte start code -> trim extra zero
            end -= 1
        yield buf[nal_start:end]
        i = nxt


def looks_like_annexb(buf: bytes) -> bool:
    """True if the buffer begins with a 3- or 4-byte Annex-B start code."""
    return buf[:3] == b"\x00\x00\x01" or buf[:4] == b"\x00\x00\x00\x01"


def is_keyframe(buf: bytes) -> bool:
    """True if the access unit contains an IDR slice or parameter set (SPS/PPS)."""
    for nal in iter_nal_units(buf):
        if not nal:
            continue
        nal_type = nal[0] & 0x1F
        if nal_type in (5, 7, 8):  # 5=IDR, 7=SPS, 8=PPS
            return True
    return False
