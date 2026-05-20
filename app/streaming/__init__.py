"""Pi -> web -> worker -> browser streaming pipeline.

The web server is the single public endpoint for the stack. Cameras (Pi) and
the worker connect into it via authenticated WebSocket channels, and browsers
pull from the web server via MJPEG (today) or WebRTC (planned).

Modules:
  hub          - in-memory FrameHubs (one per camera) with raw/processed lanes
  tokens       - camera/worker token issue + verify (argon2 hashes in DB)
  protocol     - binary envelope used between web <-> worker
  worker_link  - global state for the single worker connection (dispatch + heartbeats)
  routes       - FastAPI router: WebSocket ingest, worker channel, MJPEG out
"""
from app.streaming.hub import FrameHub, frame_hubs
from app.streaming.tokens import (
    issue_camera_token,
    issue_worker_token,
    list_cameras,
    list_workers,
    revoke_camera,
    revoke_worker,
    verify_camera_token,
    verify_worker_token,
)
from app.streaming.worker_link import worker_link

__all__ = [
    "FrameHub",
    "frame_hubs",
    "issue_camera_token",
    "issue_worker_token",
    "list_cameras",
    "list_workers",
    "revoke_camera",
    "revoke_worker",
    "verify_camera_token",
    "verify_worker_token",
    "worker_link",
]
