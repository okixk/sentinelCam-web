from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import close_pool, init_db
from app.observability import install_error_capture
from app.runtime_settings import load_and_apply_security_config
from app.storage import ensure_storage
from app.streaming import webrtc as _webrtc
from app.thumbnail_jobs import shutdown_thumbnail_jobs


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Capture WARNING+ log records into a ring buffer so the admin status
    # page can show recent errors without us shipping logs externally.
    install_error_capture()
    await init_db()
    await load_and_apply_security_config()
    await ensure_storage()
    try:
        yield
    finally:
        await _webrtc.shutdown_all()
        await shutdown_thumbnail_jobs()
        await close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory="templates")

static_path = Path(__file__).parent.parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce

    response = await call_next(request)
    forwarded_proto = (request.headers.get("x-forwarded-proto", "") or "").split(",", 1)[0].strip().lower()
    is_secure = request.url.scheme == "https" or forwarded_proto == "https"

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    path = request.url.path
    if path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    else:
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' blob: data:; "
            # mediasource srcObject for WebRTC needs 'mediastream:' too.
            f"media-src 'self' blob: mediastream:; "
            f"connect-src 'self'; "
            f"object-src 'none'; "
            f"frame-ancestors 'none'; "
            f"base-uri 'self'; "
            f"form-action 'self'"
        )

    if (
        getattr(request.state, "session_cookie_refresh", False)
        and not getattr(request.state, "skip_session_cookie_refresh", False)
    ):
        session_id = request.cookies.get("session")
        csrf_token = request.cookies.get("csrf_token")
        max_age = settings.session_max_age_hours * 3600
        if session_id:
            response.set_cookie(
                "session",
                session_id,
                httponly=True,
                samesite="strict",
                secure=is_secure,
                path="/",
                max_age=max_age,
            )
        if csrf_token:
            response.set_cookie(
                "csrf_token",
                csrf_token,
                httponly=False,
                samesite="strict",
                secure=is_secure,
                path="/",
                max_age=max_age,
            )

    return response


from app.auth.routes import router as auth_router
from app.dashboard.routes import router as dashboard_router
from app.gallery.routes import router as gallery_router
from app.recording.routes import router as recording_router
from app.streaming.routes import router as streaming_router

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(gallery_router)
app.include_router(recording_router)
app.include_router(streaming_router)


@app.get("/", response_class=HTMLResponse)
async def capture_page(request: Request):
    from app.auth.dependencies import _get_session_user
    user = await _get_session_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    return templates.TemplateResponse(request, "stream.html", {"request": request, "user": user})


@app.get("/favicon.ico")
async def favicon():
    icon_path = static_path / "branding" / "favicon.png"
    if icon_path.exists():
        return FileResponse(str(icon_path), media_type="image/png")
    return HTMLResponse("", status_code=204)


@app.get("/healthz")
async def app_health():
    return JSONResponse({"ok": True})
