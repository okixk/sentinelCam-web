#!/usr/bin/env python3
"""Entry point for sentinelCam web server."""
import os

import uvicorn

from app.config import settings


if __name__ == "__main__":
    forwarded_allow_ips = os.environ.get("SC_FORWARDED_ALLOW_IPS", "127.0.0.1")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0" if settings.public else settings.web_host,
        port=settings.web_port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        # Generous pong deadline for camera ingest over thin VPN links: with the
        # 20s defaults the pong queues behind the frame backlog and uvicorn
        # closes a healthy ingest socket (1011 keepalive ping timeout) every
        # ~20-40s. Dead-link detection still works, just with more tolerance.
        ws_ping_interval=30,
        ws_ping_timeout=120,
    )
