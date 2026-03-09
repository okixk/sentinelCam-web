# sentinelCam Web

`sentinelCam-web` is the browser UI for the sentinelCam stack.

It is a lightweight frontend that connects to a running [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker) instance, displays the processed stream, shows worker state, and sends runtime control commands.

## What this repo does

- connects to a worker over HTTP
- displays the processed stream in the browser
- prefers WebRTC for low-latency playback
- falls back to MJPEG only when `/stream.mjpg` is available
- polls worker state
- sends commands like:
  - next / previous model
  - pose toggle
  - overlay toggle
  - inference toggle
  - quit
- supports same-origin proxy deployment for production
- stays easy to move behind Apache or another reverse proxy

## What this repo does not do

This repo does **not** run YOLO itself.  
It does **not** capture cameras directly.  
All video processing happens in [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker).

## Where this repo fits

Typical flow:

`camera -> worker -> web browser`

Recommended production flow:

`camera -> worker -> apache/python web proxy -> browser`

Future distributed flow:

`camera -> sentinelCam-edge -> sentinelCam-worker -> sentinelCam-web`

## Related repositories

- **Processing backend:** [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker)  
  Required. Provides WebRTC signaling, worker state, commands, health, and optional MJPEG fallback.

- **Edge capture node:** [`sentinelCam-edge`](https://github.com/okixk/sentinelCam-edge)  
  Optional future camera-side component that will feed streams into the worker.

## Requirements

You need a running worker first.

Example worker base URL:

```text
http://127.0.0.1:8080
```

For production, same-origin proxy mode is recommended so the browser does not need direct worker access or the worker token.

## Quick start

This repo contains:

- a static frontend in `index.html`
- a lightweight Python helper server in `web_server.py`
- an Apache reverse-proxy example in `apache/sentinelcam.conf.example`

Recommended local start:

```bash
python web_server.py
```

Then open:

```text
http://127.0.0.1:3000/
```

Environment variables used by `web_server.py`:

- `WORKER_BASE_URL`
  - default: `http://127.0.0.1:8080`
- `WORKER_TOKEN`
  - optional
  - if set, the proxy adds `Authorization: Bearer <WORKER_TOKEN>` server-side
- `WEB_HOST`
  - default: `127.0.0.1`
- `WEB_PORT`
  - default: `3000`

Example:

```bash
WORKER_BASE_URL=http://127.0.0.1:8080
WORKER_TOKEN=replace-with-a-long-random-secret
python web_server.py
```

The UI input field supports two connection modes:

- `/`
  - same-origin proxy mode
  - recommended for production
  - works with `web_server.py` or Apache
- `http://host:port`
  - direct browser-to-worker mode
  - only for local/dev use
  - requires the worker to allow the browser origin with `WEB_ALLOWED_ORIGINS`

## Worker endpoints used by this UI

The UI expects the worker to provide:

- `POST /api/webrtc/offer`
- `GET /api/state`
- `POST /api/cmd`
- `GET /health`
- `GET /stream.mjpg` only for explicit MJPEG fallback

So if your worker runs on `http://192.168.1.50:8080`, this UI can use:

- `http://192.168.1.50:8080/api/webrtc/offer`
- `http://192.168.1.50:8080/api/state`
- `http://192.168.1.50:8080/api/cmd`
- `http://192.168.1.50:8080/health`
- `http://192.168.1.50:8080/stream.mjpg`

In proxy mode, the browser stays on relative paths like:

- `/api/webrtc/offer`
- `/api/state`
- `/api/cmd`
- `/health`
- `/stream.mjpg`

## Features

- live WebRTC stream viewer
- explicit MJPEG fallback when available
- worker status display
- current preset / detection / pose / FPS / inference state
- model switching with loading feedback
- basic remote worker control from the browser
- same-origin proxy support for production-safe token handling
- easy Apache migration because the frontend stays static

## Files

- `index.html` - complete frontend UI
- `web_server.py` - small Python static server and reverse proxy
- `apache/sentinelcam.conf.example` - example Apache config for same-origin deployment

## Notes

- This repo is intentionally simple.
- The frontend itself stays static so it can be served by Apache or another web server without code changes.
- `WORKER_TOKEN` should stay server-side only.
- For direct browser access, the worker must allow explicit origins with `WEB_ALLOWED_ORIGINS`.
- Do not use wildcard origins in production.
- Use HTTPS in production, or plain `http://localhost` only for local development.
- WebRTC is always attempted first.
- MJPEG fallback only works when the worker is actually serving `/stream.mjpg`.

## Status

Current WebRTC-first browser frontend for the sentinelCam stack, with optional Python proxy support and Apache-ready deployment.
