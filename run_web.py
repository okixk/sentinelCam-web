#!/usr/bin/env python3
"""Entry point for sentinelCam web server."""
import uvicorn
from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0" if settings.public else settings.web_host,
        port=settings.web_port,
        log_level="info",
    )
