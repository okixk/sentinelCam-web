from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.dependencies import User, check_csrf, get_current_user, require_admin
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
    "connection",
    "keep-alive",
    "content-length",
    "proxy-authenticate",
    "proxy-authorization",
    "server",
    "date",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    # Strip cookies and auth coming from the worker so its session/auth state
    # never leaks into the web browser's origin.
    "set-cookie",
    "cookie",
    "www-authenticate",
}

SHUTDOWN_COMMANDS = {"q", "quit", "exit", "stop"}
_worker_proxy_status: dict[str, object] = {
    "last_attempt_at": None,
    "last_ok_at": None,
    "last_error_at": None,
    "last_error": "",
    "last_path": "",
    "last_status_code": None,
}


def _worker_url(path: str) -> str:
    base = (settings.worker_base_url or "http://127.0.0.1:8080").rstrip("/")
    return f"{base}{path}"


def _worker_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.worker_token:
        headers["Authorization"] = f"Bearer {settings.worker_token}"
    return headers


def _worker_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "worker_http_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="worker proxy client not initialized")
    return client


def _proxy_error_response(path: str, error: Exception, *, detail: str = "proxy request failed") -> JSONResponse:
    _worker_proxy_status["last_attempt_at"] = time.time()
    _worker_proxy_status["last_error_at"] = time.time()
    # Keep the verbose error string only in server-side status (visible via
    # /api/admin/ops) — do not echo it to the browser, where it would leak
    # internal worker URLs and internal exception strings.
    _worker_proxy_status["last_error"] = f"{detail}: {error}"
    _worker_proxy_status["last_path"] = path
    _worker_proxy_status["last_status_code"] = 502
    log.warning("Worker proxy %s failed: %s", path, error)
    return JSONResponse(
        {"ok": False, "error": detail},
        status_code=502,
    )


async def _probe_worker_capabilities(request: Request) -> dict:
    global _capability_cache
    now = time.time()
    async with _capability_cache_lock:
        ts = float(_capability_cache.get("ts", 0.0) or 0.0)
        val = _capability_cache.get("value")
        if isinstance(val, dict) and now - ts <= CAPABILITY_CACHE_TTL:
            return dict(val)

    capabilities = {"webrtc_available": False, "mjpeg_available": True, "stream_backend": "mjpeg"}
    try:
        resp = await _worker_client(request).get(_worker_url("/api/webrtc/offer"), headers=_worker_headers())
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


async def _augment_state(request: Request, body: bytes) -> bytes:
    if (
        b'"webrtc_available"' in body
        and b'"mjpeg_available"' in body
        and b'"stream_backend"' in body
    ):
        return body
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    if all(k in payload for k in ("webrtc_available", "mjpeg_available", "stream_backend")):
        return body
    payload.update(await _probe_worker_capabilities(request))
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


def _mark_worker_success(path: str, status_code: int) -> None:
    _worker_proxy_status["last_attempt_at"] = time.time()
    _worker_proxy_status["last_ok_at"] = time.time()
    _worker_proxy_status["last_path"] = path
    _worker_proxy_status["last_status_code"] = status_code
    _worker_proxy_status["last_error"] = ""


def get_worker_proxy_status() -> dict[str, object]:
    return dict(_worker_proxy_status)


def reset_worker_proxy_status() -> None:
    _worker_proxy_status.update(
        {
            "last_attempt_at": None,
            "last_ok_at": None,
            "last_error_at": None,
            "last_error": "",
            "last_path": "",
            "last_status_code": None,
        }
    )


@router.get("/api/state")
async def proxy_state(request: Request, user: User = Depends(get_current_user)):
    try:
        resp = await _worker_client(request).get(_worker_url("/api/state"), headers=_worker_headers())
        _mark_worker_success("/api/state", resp.status_code)
        body = await _augment_state(request, resp.content)
        return Response(
            content=body,
            status_code=resp.status_code,
            headers=_filter_headers(resp.headers),
            media_type="application/json",
        )
    except httpx.RequestError as e:
        return _proxy_error_response("/api/state", e)


@router.post("/api/cmd")
async def proxy_cmd(request: Request, user: User = Depends(require_admin), _csrf=Depends(check_csrf)):
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    if _wants_shutdown(body, content_type):
        log.info("Quit command forwarded to worker; proxy stays online")
    try:
        headers = _worker_headers()
        if content_type:
            headers["Content-Type"] = content_type
        resp = await _worker_client(request).post(_worker_url("/api/cmd"), content=body, headers=headers)
        _mark_worker_success("/api/cmd", resp.status_code)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=_filter_headers(resp.headers),
        )
    except httpx.RequestError as e:
        return _proxy_error_response("/api/cmd", e)


async def _proxy_streaming_mjpeg(request: Request, worker_path: str) -> Response:
    try:
        client = _worker_client(request)
        upstream = await client.send(
            client.build_request("GET", _worker_url(worker_path), headers=_worker_headers()),
            stream=True,
        )
        _mark_worker_success(worker_path, upstream.status_code)
    except httpx.RequestError as e:
        return _proxy_error_response(worker_path, e)

    if upstream.status_code >= 400:
        try:
            body = await upstream.aread()
            return Response(
                content=body,
                status_code=upstream.status_code,
                headers=_filter_headers(upstream.headers),
                media_type=upstream.headers.get("content-type"),
            )
        finally:
            await upstream.aclose()

    async def stream_chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        except Exception as exc:
            log.warning("Worker MJPEG stream ended: %s", exc)
        finally:
            await upstream.aclose()

    headers = _filter_headers(upstream.headers)
    headers["Cache-Control"] = "no-store"
    headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(
        stream_chunks(),
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )


@router.get("/stream.mjpg")
async def proxy_mjpeg(request: Request, user: User = Depends(get_current_user)):
    return await _proxy_streaming_mjpeg(request, "/stream.mjpg")


@router.get("/stream-raw.mjpg")
async def proxy_raw_mjpeg(request: Request, user: User = Depends(get_current_user)):
    return await _proxy_streaming_mjpeg(request, "/stream-raw.mjpg")


@router.post("/api/webrtc/offer")
async def proxy_webrtc_offer_post(request: Request, user: User = Depends(get_current_user), _csrf=Depends(check_csrf)):
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    try:
        headers = _worker_headers()
        if content_type:
            headers["Content-Type"] = content_type
        resp = await _worker_client(request).post(_worker_url("/api/webrtc/offer"), content=body, headers=headers)
        _mark_worker_success("/api/webrtc/offer", resp.status_code)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=_filter_headers(resp.headers),
        )
    except httpx.RequestError as e:
        return _proxy_error_response("/api/webrtc/offer", e)


@router.get("/api/webrtc/offer")
async def proxy_webrtc_offer_get(request: Request, user: User = Depends(get_current_user)):
    try:
        resp = await _worker_client(request).get(_worker_url("/api/webrtc/offer"), headers=_worker_headers())
        _mark_worker_success("/api/webrtc/offer", resp.status_code)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=_filter_headers(resp.headers),
        )
    except httpx.RequestError as e:
        return _proxy_error_response("/api/webrtc/offer", e)


@router.get("/health")
async def health(request: Request, user: User = Depends(get_current_user)):
    try:
        resp = await _worker_client(request).get(_worker_url("/health"), headers=_worker_headers())
        _mark_worker_success("/health", resp.status_code)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=_filter_headers(resp.headers),
        )
    except httpx.RequestError as e:
        return _proxy_error_response("/health", e, detail="worker unreachable")


@router.get("/api/proxy/frame-raw.jpg")
async def proxy_frame_raw(request: Request, user: User = Depends(get_current_user)):
    try:
        resp = await _worker_client(request).get(_worker_url("/frame-raw.jpg"), headers=_worker_headers())
        _mark_worker_success("/frame-raw.jpg", resp.status_code)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=_filter_headers(resp.headers),
            media_type=resp.headers.get("content-type", "image/jpeg"),
        )
    except httpx.RequestError as e:
        return _proxy_error_response("/frame-raw.jpg", e)
