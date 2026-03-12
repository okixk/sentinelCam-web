from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.dependencies import User, get_current_user, check_csrf
from app.config import settings

log = logging.getLogger("sentinelCam.proxy")
router = APIRouter(tags=["proxy"])

CAPABILITY_CACHE_TTL = 5.0
_capability_cache: dict = {
    "ts": 0.0,
    "value": {"webrtc_available": False, "mjpeg_available": True, "stream_backend": "mjpeg"},
}
_capability_cache_lock = asyncio.Lock()

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "content-length", "proxy-authenticate",
    "proxy-authorization", "server", "date", "te", "trailers",
    "transfer-encoding", "upgrade",
}

SHUTDOWN_COMMANDS = {"q", "quit", "exit", "stop"}


def _worker_url(path: str) -> str:
    base = (settings.worker_base_url or "http://127.0.0.1:8080").rstrip("/")
    return f"{base}{path}"


def _worker_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.worker_token:
        headers["Authorization"] = f"Bearer {settings.worker_token}"
    return headers


async def _probe_worker_capabilities() -> dict:
    global _capability_cache
    now = time.time()
    async with _capability_cache_lock:
        ts = float(_capability_cache.get("ts", 0.0) or 0.0)
        val = _capability_cache.get("value")
        if isinstance(val, dict) and now - ts <= CAPABILITY_CACHE_TTL:
            return dict(val)

    capabilities = {"webrtc_available": False, "mjpeg_available": True, "stream_backend": "mjpeg"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_worker_url("/api/webrtc/offer"), headers=_worker_headers())
            if 200 <= resp.status_code < 300:
                capabilities["webrtc_available"] = True
                capabilities["stream_backend"] = "webrtc"
            elif resp.status_code not in (404,):
                capabilities["probe_status"] = resp.status_code
    except Exception:
        pass

    async with _capability_cache_lock:
        _capability_cache["ts"] = now
        _capability_cache["value"] = dict(capabilities)
    return capabilities


async def _augment_state(body: bytes) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    if all(k in payload for k in ("webrtc_available", "mjpeg_available", "stream_backend")):
        return body
    payload.update(await _probe_worker_capabilities())
    return json.dumps(payload).encode("utf-8")


def _wants_shutdown(body: bytes, content_type: str) -> bool:
    if not body:
        return False
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct == "application/json":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return False
        if isinstance(payload, dict):
            for key in ("cmd", "command", "action", "event", "name", "type"):
                cmd = str(payload.get(key, "") or "").strip().lower()
                if cmd in SHUTDOWN_COMMANDS:
                    return True
            for key in SHUTDOWN_COMMANDS:
                if payload.get(key) is True:
                    return True
        return False
    return body.decode("utf-8", "ignore").strip().lower() in SHUTDOWN_COMMANDS


def _filter_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


@router.get("/api/state")
async def proxy_state(user: User = Depends(get_current_user)):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_worker_url("/api/state"), headers=_worker_headers())
            body = await _augment_state(resp.content)
            return Response(
                content=body,
                status_code=resp.status_code,
                headers=_filter_headers(resp.headers),
                media_type="application/json",
            )
    except httpx.RequestError as e:
        return JSONResponse({"ok": False, "error": f"proxy request failed: {e}"}, status_code=502)


@router.post("/api/cmd")
async def proxy_cmd(request: Request, user: User = Depends(get_current_user), _csrf=Depends(check_csrf)):
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if _wants_shutdown(body, content_type):
        log.info("Quit command forwarded to worker – proxy stays online")
    try:
        headers = _worker_headers()
        if content_type:
            headers["Content-Type"] = content_type
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(_worker_url("/api/cmd"), content=body, headers=headers)
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=_filter_headers(resp.headers),
            )
    except httpx.RequestError as e:
        return JSONResponse({"ok": False, "error": f"proxy request failed: {e}"}, status_code=502)


@router.get("/stream.mjpg")
async def proxy_mjpeg(request: Request, user: User = Depends(get_current_user)):
    async def stream_chunks() -> AsyncIterator[bytes]:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", _worker_url("/stream.mjpg"), headers=_worker_headers()) as resp:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        yield chunk
        except Exception:
            return

    return StreamingResponse(stream_chunks(), media_type="multipart/x-mixed-replace; boundary=frame")


@router.post("/api/webrtc/offer")
async def proxy_webrtc_offer_post(request: Request, user: User = Depends(get_current_user), _csrf=Depends(check_csrf)):
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    try:
        headers = _worker_headers()
        if content_type:
            headers["Content-Type"] = content_type
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_worker_url("/api/webrtc/offer"), content=body, headers=headers)
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=_filter_headers(resp.headers),
            )
    except httpx.RequestError as e:
        return JSONResponse({"ok": False, "error": f"proxy request failed: {e}"}, status_code=502)


@router.get("/api/webrtc/offer")
async def proxy_webrtc_offer_get(user: User = Depends(get_current_user)):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_worker_url("/api/webrtc/offer"), headers=_worker_headers())
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=_filter_headers(resp.headers),
            )
    except httpx.RequestError as e:
        return JSONResponse({"ok": False, "error": f"proxy request failed: {e}"}, status_code=502)


@router.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_worker_url("/health"), headers=_worker_headers())
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=_filter_headers(resp.headers),
            )
    except httpx.RequestError as e:
        return JSONResponse({"ok": False, "error": f"worker unreachable: {e}"}, status_code=502)


@router.get("/api/proxy/frame-raw.jpg")
async def proxy_frame_raw(user: User = Depends(get_current_user)):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_worker_url("/frame-raw.jpg"), headers=_worker_headers())
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=_filter_headers(resp.headers),
                media_type=resp.headers.get("content-type", "image/jpeg"),
            )
    except httpx.RequestError as e:
        return JSONResponse({"ok": False, "error": f"proxy request failed: {e}"}, status_code=502)
