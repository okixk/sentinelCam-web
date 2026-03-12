#!/usr/bin/env python3
"""Small static web server plus worker reverse proxy for sentinelCam web."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("sentinelCam.web")


ROOT = Path(__file__).resolve().parent
INDEX_HTML = ROOT / "index.html"

DEFAULT_WORKER_BASE_URL = "http://127.0.0.1:8080"
WORKER_BASE_URL = (os.environ.get("WORKER_BASE_URL", DEFAULT_WORKER_BASE_URL) or DEFAULT_WORKER_BASE_URL).rstrip("/")
WORKER_TOKEN = (os.environ.get("WORKER_TOKEN", "") or "").strip()

# Set via environment variable PUBLIC=1 or change here directly.
# True  = server listens on 0.0.0.0 (all network interfaces, accessible from other devices)
# False = server listens on 127.0.0.1 (localhost only, default)
PUBLIC = os.environ.get("PUBLIC", "0").strip() in ("1", "true", "yes")

WEB_HOST = "0.0.0.0" if PUBLIC else "127.0.0.1"
CAPABILITY_CACHE_TTL = 5.0

try:
    WEB_PORT = int(os.environ.get("WEB_PORT", "3000") or "3000")
except ValueError:
    WEB_PORT = 3000

PROXY_GET_PATHS = {
    "/api/state",
    "/api/webrtc/offer",
    "/health",
    "/stream.mjpg",
}
PROXY_POST_PATHS = {
    "/api/cmd",
    "/api/webrtc/offer",
}
SHUTDOWN_COMMANDS = {"q", "quit", "exit", "stop"}
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
}
_capability_cache_lock = threading.Lock()
_capability_cache: dict[str, object] = {
    "ts": 0.0,
    "value": {
        "webrtc_available": False,
        "mjpeg_available": True,
        "stream_backend": "mjpeg",
    },
}


def _worker_url(path: str, query: str) -> str:
    base = WORKER_BASE_URL or DEFAULT_WORKER_BASE_URL
    return f"{base}{path}{('?' + query) if query else ''}"


def _normalize_cmd_name(raw_cmd: object) -> str:
    return str(raw_cmd or "").strip().lower()


def _worker_request_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if WORKER_TOKEN:
        headers["Authorization"] = f"Bearer {WORKER_TOKEN}"
    return headers


def _probe_worker_stream_capabilities() -> dict[str, object]:
    now = time.time()
    with _capability_cache_lock:
        cached_ts = float(_capability_cache.get("ts", 0.0) or 0.0)
        cached_value = _capability_cache.get("value")
        if isinstance(cached_value, dict) and now - cached_ts <= CAPABILITY_CACHE_TTL:
            return dict(cached_value)

    capabilities = {
        "webrtc_available": False,
        "mjpeg_available": True,
        "stream_backend": "mjpeg",
    }
    request = urllib.request.Request(
        _worker_url("/api/webrtc/offer", ""),
        headers=_worker_request_headers(),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if 200 <= int(response.status) < 300:
                capabilities["webrtc_available"] = True
                capabilities["stream_backend"] = "webrtc"
    except urllib.error.HTTPError as exc:
        if int(exc.code) not in (404,):
            capabilities["probe_status"] = int(exc.code)
    except urllib.error.URLError:
        pass

    with _capability_cache_lock:
        _capability_cache["ts"] = now
        _capability_cache["value"] = dict(capabilities)
    return capabilities


def _augment_state_payload(body: bytes) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        return body

    if not isinstance(payload, dict):
        return body

    if all(key in payload for key in ("webrtc_available", "mjpeg_available", "stream_backend")):
        return body

    payload.update(_probe_worker_stream_capabilities())
    return json.dumps(payload).encode("utf-8")


def _request_wants_shutdown(path: str, content_type: str, request_body: bytes | None) -> bool:
    if path != "/api/cmd":
        return False

    body = request_body or b""
    if not body:
        return False

    if content_type.split(";", 1)[0].strip().lower() == "application/json":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return False

        if isinstance(payload, dict):
            for key in ("cmd", "command", "action", "event", "name", "type"):
                cmd = _normalize_cmd_name(payload.get(key))
                if cmd in SHUTDOWN_COMMANDS:
                    return True
            for key in SHUTDOWN_COMMANDS:
                if payload.get(key) is True:
                    return True
            return False

        return _normalize_cmd_name(payload) in SHUTDOWN_COMMANDS

    return _normalize_cmd_name(body.decode("utf-8", "ignore")) in SHUTDOWN_COMMANDS


class SentinelCamHandler(BaseHTTPRequestHandler):
    server_version = "SentinelCamWeb/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ("", "/", "/index.html"):
            self._serve_index()
            return
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path in PROXY_GET_PATHS:
            self._proxy_request(parsed)
            return
        self._send_not_found()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in PROXY_POST_PATHS:
            self._proxy_request(parsed)
            return
        self._send_not_found()

    def do_OPTIONS(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in PROXY_GET_PATHS or parsed.path in PROXY_POST_PATHS:
            self.send_response(204)
            self.send_header("Allow", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_not_found()

    def log_message(self, fmt: str, *args: object) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _serve_index(self) -> None:
        try:
            raw = INDEX_HTML.read_bytes()
        except OSError as exc:
            self._send_json(
                500,
                {
                    "ok": False,
                    "error": f"could not read index.html: {exc}",
                },
            )
            return

        nonce = secrets.token_urlsafe(24)
        payload = raw.replace(b"<script>", f'<script nonce="{nonce}">'.encode(), 1)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", f"script-src 'nonce-{nonce}'")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _proxy_request(self, parsed: urllib.parse.SplitResult) -> None:
        target_url = _worker_url(parsed.path, parsed.query)

        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = 0
        request_body = self.rfile.read(content_length) if content_length > 0 else None

        outgoing_headers = {}
        content_type = (self.headers.get("Content-Type", "") or "").strip()
        accept = (self.headers.get("Accept", "") or "").strip()
        wants_shutdown = _request_wants_shutdown(parsed.path, content_type, request_body)
        if content_type:
            outgoing_headers["Content-Type"] = content_type
        if accept:
            outgoing_headers["Accept"] = accept
        if WORKER_TOKEN:
            outgoing_headers["Authorization"] = f"Bearer {WORKER_TOKEN}"

        request = urllib.request.Request(
            target_url,
            data=request_body,
            headers=outgoing_headers,
            method=self.command,
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body_override = None
                if parsed.path == "/api/state":
                    body_override = _augment_state_payload(response.read())
                self._relay_response(
                    response.status,
                    response.headers.items(),
                    response,
                    streaming=parsed.path == "/stream.mjpg",
                    body_override=body_override,
                )
                if wants_shutdown:
                    log.info("Quit command forwarded to worker – proxy stays online")
        except urllib.error.HTTPError as exc:
            body_override = None
            if parsed.path == "/api/state":
                body_override = _augment_state_payload(exc.read())
            self._relay_response(exc.code, exc.headers.items(), exc, streaming=False, body_override=body_override)
        except urllib.error.URLError as exc:
            detail = getattr(exc, "reason", exc)
            self._send_json(
                502,
                {
                    "ok": False,
                    "error": f"proxy request failed: {detail}",
                    "upstream": target_url,
                },
            )

    def _relay_response(
        self,
        status: int,
        headers: object,
        response: object,
        streaming: bool,
        body_override: bytes | None = None,
    ) -> None:
        body = body_override if body_override is not None else b""
        if not streaming and body_override is None:
            body = response.read()

        self.send_response(status)
        for key, value in headers:
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            self.send_header(key, value)
        if not streaming:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if not streaming:
            if body:
                self.wfile.write(body)
            return

        try:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_not_found(self) -> None:
        self._send_json(404, {"ok": False, "error": "not found"})


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    log.info("Serving sentinelCam web on http://%s:%s", WEB_HOST, WEB_PORT)
    log.info("Proxy target: %s", WORKER_BASE_URL)
    if WORKER_TOKEN:
        log.info("Worker token: configured server-side")
    else:
        log.warning("Worker token: not configured")

    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), SentinelCamHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
