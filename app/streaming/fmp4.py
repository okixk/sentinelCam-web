"""Fragmented-MP4 relay for the browser live view.

A browser viewer opens ``GET /api/cameras/{cam_id}/live.mp4``. We subscribe to
the camera's H.264 lane (starting at a keyframe), pipe the Annex-B access units
through ``ffmpeg -c:v copy`` into a fragmented MP4 (``movflags
frag_keyframe+empty_moov``) and stream that to the browser, which plays it via
Media Source Extensions.

Why not WebRTC? WebRTC media is UDP on ephemeral ports. This deployment exposes
only 80/443/1194 and sits behind Cloudflare (HTTP-only proxy), so WebRTC media
cannot reach a remote browser. fMP4 over HTTPS works over 443 for both remote
(Cloudflare) and VPN clients. ``-c:v copy`` means the web server never decodes
or re-encodes — all the encode cost stays on the worker's GPU.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Set

from app.streaming.hub import FrameHub
from app.streaming.protocol import MSG_KEYFRAME_REQ, encode, now_ms
from app.streaming.worker_link import worker_link


log = logging.getLogger("sentinelCam.fmp4")

# Hard cap on simultaneous live viewers (one cheap ffmpeg copy process each).
MAX_VIEWERS = 16

# Running ffmpeg subprocesses, tracked so the lifespan can tear them down.
_procs: Set[asyncio.subprocess.Process] = set()


def at_capacity() -> bool:
    return len(_procs) >= MAX_VIEWERS


def _ffmpeg_args() -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        # Stamp AUs with their arrival time; the small probe window keeps
        # startup fast. Do NOT add "-fflags +nobuffer" here: with "-f h264"
        # + "-c:v copy" it makes the demuxer silently drop ~1/3 of the frames
        # (measured: 90-frame stream -> 60 decoded) AND quadruples first-byte
        # latency (~1.3s vs ~0.3s with the probe settings below).
        "-probesize", "32768",
        "-analyzeduration", "0",
        "-use_wallclock_as_timestamps", "1",
        "-f", "h264",
        "-i", "pipe:0",
        "-an",
        "-c:v", "copy",
        "-f", "mp4",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
        "-frag_duration", "250000",  # flush a fragment at least every 250 ms
        "pipe:1",
    ]


async def _request_keyframe(cam_id: int) -> None:
    try:
        await worker_link.dispatch_to_worker(encode(MSG_KEYFRAME_REQ, cam_id, now_ms(), b""))
    except Exception:
        log.debug("keyframe request dispatch failed", exc_info=True)


def active_viewers() -> int:
    return len(_procs)


async def live_mp4(hub: FrameHub) -> AsyncIterator[bytes]:
    """Yield fragmented-MP4 bytes for one viewer."""
    cam_id = hub.camera_id
    # Nudge the worker to emit a fresh IDR so we can start quickly.
    await _request_keyframe(cam_id)

    proc = await asyncio.create_subprocess_exec(
        *_ffmpeg_args(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _procs.add(proc)

    async def _feed() -> None:
        try:
            async for au in hub.subscribe_h264(idle_timeout=10.0):
                if proc.stdin is None or proc.stdin.is_closing():
                    break
                proc.stdin.write(au)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("h264 feed stopped", exc_info=True)
        finally:
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception:
                    pass

    async def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                log.debug("ffmpeg[%d]: %s", cam_id, line.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    feed_task = asyncio.create_task(_feed())
    err_task = asyncio.create_task(_drain_stderr())
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        feed_task.cancel()
        err_task.cancel()
        for t in (feed_task, err_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await _terminate(proc)
        _procs.discard(proc)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        pass


async def shutdown_all() -> None:
    """Kill every running ffmpeg relay. Called from the FastAPI lifespan."""
    procs = list(_procs)
    _procs.clear()
    for proc in procs:
        await _terminate(proc)
