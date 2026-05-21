"""WebRTC SFU for browser viewers.

Each browser that wants to watch a camera POSTs an SDP offer to
``/api/cameras/{cam_id}/webrtc/offer``. We create one
:class:`aiortc.RTCPeerConnection` per viewer, attach a
:class:`MJPEGSource` track that pulls processed JPEGs out of the per-camera
:class:`~app.streaming.hub.FrameHub`, return an SDP answer, and tear the
connection down again on close/disconnect/failure.

The processed JPEG arrives from the worker; if no worker is connected yet
the same track will happily forward the raw lane so the viewer still sees
something.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

import cv2  # type: ignore
import numpy as np  # type: ignore
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame  # type: ignore

from app.streaming.hub import FrameHub


log = logging.getLogger("sentinelCam.webrtc")

_pcs: Set[RTCPeerConnection] = set()

# Hard cap on simultaneous browser viewers; one H200 can chew through more
# but bounding the cost keeps surprises out of the picture.
_MAX_VIEWERS = 16

# Played in place of a real frame while we wait for the camera to come up.
_PLACEHOLDER_BGR: np.ndarray = np.zeros((360, 640, 3), dtype=np.uint8)


class MJPEGSource(VideoStreamTrack):
    """A WebRTC video track sourced from a :class:`FrameHub`.

    Prefers the processed lane; falls back to the raw lane if the worker is
    offline. Returns the previous decoded frame (or a black placeholder) when
    there's no data so the WebRTC encoder keeps producing packets and the
    browser does not stall.
    """

    kind = "video"

    def __init__(self, hub: FrameHub) -> None:
        super().__init__()
        self._hub = hub
        self._last_capture_ms = 0
        self._last_decoded: Optional[np.ndarray] = None

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        result = await self._hub.wait_processed(self._last_capture_ms, timeout=0.5)
        if result is None:
            result = await self._hub.wait_raw(self._last_capture_ms, timeout=0.05)

        if result is None:
            decoded = self._last_decoded if self._last_decoded is not None else _PLACEHOLDER_BGR
        else:
            jpeg_bytes, capture_ms = result
            self._last_capture_ms = capture_ms
            decoded = await asyncio.to_thread(self._decode_jpeg, jpeg_bytes)
            if decoded is None:
                decoded = self._last_decoded if self._last_decoded is not None else _PLACEHOLDER_BGR
            else:
                self._last_decoded = decoded

        frame = VideoFrame.from_ndarray(decoded, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base
        return frame

    @staticmethod
    def _decode_jpeg(jpeg_bytes: bytes) -> Optional[np.ndarray]:
        if not jpeg_bytes:
            return None
        try:
            arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception:
            return None


async def handle_offer(hub: FrameHub, offer_sdp: str, offer_type: str) -> dict[str, str]:
    """Accept an SDP offer, negotiate an answer, and return the SDP answer.

    Raises :class:`RuntimeError` when the viewer cap is hit, so the caller
    can map it to an HTTP 503.
    """
    if len(_pcs) >= _MAX_VIEWERS:
        raise RuntimeError("max concurrent WebRTC viewers reached")

    pc = RTCPeerConnection()
    _pcs.add(pc)
    track = MJPEGSource(hub)

    @pc.on("connectionstatechange")
    async def on_state_change() -> None:
        state = pc.connectionState
        log.debug("pc[%s] state -> %s", id(pc), state)
        if state in ("failed", "closed", "disconnected"):
            _pcs.discard(pc)
            try:
                await pc.close()
            except Exception:
                pass

    pc.addTrack(track)
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception:
        _pcs.discard(pc)
        try:
            await pc.close()
        except Exception:
            pass
        raise

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


def active_viewers() -> int:
    return len(_pcs)


async def shutdown_all() -> None:
    """Close every open peer connection. Called from the FastAPI lifespan."""
    pcs = list(_pcs)
    _pcs.clear()
    coros = []
    for pc in pcs:
        try:
            coros.append(pc.close())
        except Exception:
            pass
    if coros:
        try:
            await asyncio.wait_for(asyncio.gather(*coros, return_exceptions=True), timeout=5.0)
        except asyncio.TimeoutError:
            pass
