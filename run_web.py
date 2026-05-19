#!/usr/bin/env python3
"""Entry point for sentinelCam web server."""
import os

import uvicorn

from app.config import settings


if __name__ == "__main__":
    # Honor X-Forwarded-* headers only when the immediate peer is a trusted
    # reverse proxy. Default to loopback so a directly internet-facing
    # deployment cannot spoof scheme/host via client-set headers; the TLS
    # overlay sets SC_FORWARDED_ALLOW_IPS="*" because the only ingress to
    # the web container is the in-network Caddy sidecar.
    forwarded_allow_ips = os.environ.get("SC_FORWARDED_ALLOW_IPS", "127.0.0.1")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0" if settings.public else settings.web_host,
        port=settings.web_port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
    )
