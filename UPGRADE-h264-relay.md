# Upgrade: H.264 fragmented-MP4 live relay + hardening

This branch (`feature/h264-relay-hardening`, all three repos) replaces the
per-viewer WebRTC JPEG transcode with a fragmented-MP4 (H.264) relay and adds
security/reliability fixes. This file is the deploy + test runbook.

## What changed (architecture)

```
camera/edge --JPEG--> web  --JPEG--> worker (YOLO + GPU H.264)
                       |                 |
                       |<--H.264 + JPEG--+
                       |
   browser <--fMP4 (ffmpeg -c:v copy, no re-encode)-- web   [primary]
   browser <--MJPEG (passthrough)-------------------- web   [fallback]
```

- The worker GPU-encodes the annotated frame to H.264 **and** keeps a JPEG lane
  for snapshots / clips / MJPEG fallback.
- The web server muxes the H.264 to fragmented-MP4 with `ffmpeg -c:v copy`
  (no decode/re-encode — the web container has no GPU) and streams it over
  HTTPS. The browser plays it via Media Source Extensions; on any error it
  falls back to MJPEG automatically.
- WebRTC is gone (its UDP can't traverse the 80/443/1194 + Cloudflare edge);
  `aiortc`/PyAV were removed from the web image.

## Run the tests

No GPU/camera needed — these cover the wire protocol, NAL/keyframe parsing and
the H.264 hub lane (pure-Python, run anywhere):

```bash
# web
cd sentinelCam-web && python -m unittest discover -s tests -v

# worker
cd sentinelCam-worker && python -m unittest tests.test_protocol_contract tests.test_encoder_nal -v
```

The `test_golden_header` case in both repos asserts the **same** wire bytes, so
if either `protocol.py` drifts its suite fails (the cross-repo contract guard).

## Deploy (production)

The worker dials into the web server; nothing new is exposed to the internet
(still only 80/443/1194).

1. **Web** — pull the branch, then:
   ```bash
   cd sentinelCam-web
   # Optional: set SC_PUBLIC_ORIGIN=https://<your-host> in .env if the public
   # host differs from WEBAUTHN_RP_ID. With SC_PUBLIC=1 it defaults to
   # https://<WEBAUTHN_RP_ID>, which is what pins WebAuthn/cookies/CSRF.
   docker compose up -d --build
   curl -fsS https://<host>/readyz   # -> {"ok": true, "db": true}
   ```
2. **Worker** — set the new H.264 vars in `.env` (see
   `.env.pipeline.example`: `WORKER_H264`, `WORKER_VIDEO_FPS`,
   `WORKER_H264_BITRATE_KBPS`, `WORKER_PROCESSED_JPEG_FPS`,
   `WORKER_YOLO_CLASSES`), then:
   ```bash
   cd sentinelCam-worker
   docker compose -f docker-compose.pipeline.yml up -d --build
   docker compose -f docker-compose.pipeline.yml logs -f worker
   # look for: "H.264 encoder selected: <codec>"  and  "worker connected"
   ```
3. **Edge** — `pip install -r laptop-streamer/requirements.txt` and run with the
   `SC_*` env vars (see edge README). `SC_INSECURE` now defaults to 0 (verify TLS).
4. Open the live page. The HUD shows **H.264** when fMP4 is active, **MJPEG**
   when it fell back. Snapshots and clips keep working (JPEG lane).

## GPU H.264 (NVENC)

PyAV's prebuilt wheels bundle an ffmpeg **without** NVENC, so the worker uses
`libx264` (CPU) by default — fine for a few 720p streams. For true GPU encode,
build PyAV against an NVENC-enabled ffmpeg on the worker host, or set
`WORKER_H264=0` to disable H.264 (the web then serves MJPEG only). The selected
encoder is logged at startup and shown in the worker heartbeat (`encoder`).

## Reproducible builds (web)

```bash
pip install pip-tools
pip-compile --generate-hashes -o requirements.lock requirements.txt
pip install --require-hashes -r requirements.lock
```

## Rollback

Each repo is on its own branch; redeploy the previous branch
(`web: feature/infra-revamp`, `worker: feature/web-streaming-pipeline`,
`edge: main`) and `docker compose up -d --build` to revert.

## Deferred (separate reviewed step)

The standalone `webcam.py` (a LAN-only direct-WebRTC tool the web product does
not use) still has its ~1900-line `main()` god-function, dead `SharedEncoder`
code, and a no-op MJPEG connection cap. These were left for a separate change
since that path can't be exercised in CI here; the SIGTERM handler and
camera-reopen reliability fixes were applied.
```
