from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

templates = Jinja2Templates(directory="templates")

# Mount static files
static_path = Path(__file__).parent.parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce

    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

    # Don't add CSP to streaming responses or static files
    path = request.url.path
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'nonce-{nonce}'; "
            f"img-src 'self' blob: data:; "
            f"media-src 'self' blob:; "
            f"connect-src 'self'; "
            f"frame-ancestors 'none'; "
            f"base-uri 'self'; "
            f"form-action 'self'"
        )

    return response


# Include routers
from app.auth.routes import router as auth_router
from app.proxy.routes import router as proxy_router
from app.dashboard.routes import router as dashboard_router
from app.gallery.routes import router as gallery_router
from app.recording.routes import router as recording_router

app.include_router(auth_router)
app.include_router(proxy_router)
app.include_router(dashboard_router)
app.include_router(gallery_router)
app.include_router(recording_router)


@app.get("/", response_class=HTMLResponse)
async def stream_page(request: Request):
    from app.auth.dependencies import _get_session_user
    user = await _get_session_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=302)
    return templates.TemplateResponse("stream.html", {"request": request, "user": user})


@app.get("/favicon.ico")
async def favicon():
    return HTMLResponse("", status_code=204)
