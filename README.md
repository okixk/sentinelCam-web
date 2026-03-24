# sentinelCam Web

`sentinelCam-web` is the browser UI for the sentinelCam stack.

It connects to a running `sentinelCam-worker`, shows the live stream, proxies worker APIs, stores captures and recordings, and provides user/admin workflows with password and passkey authentication.

## What This Repo Does

- FastAPI web app with session auth and WebAuthn/passkeys
- Same-origin proxy for worker state, commands, MJPEG, and WebRTC signaling
- SQLite-backed users, sessions, and recording metadata
- Jinja-rendered UI for stream, gallery, detail, and admin pages
- Docker packaging for the web app

## Architecture

```text
camera/source -> sentinelCam-worker (port 8080) -> sentinelCam-web (port 3000) -> browser
```

The recommended setup depends on the platform:

| Platform | Recommended web start | Recommended worker start |
|---|---|---|
| Windows | Docker Desktop | local PowerShell / `run.bat` |
| Linux | Docker Engine | local `run.sh` or full Docker stack |
| macOS | Docker Desktop | local Terminal / `run.sh` |

Important:

- On Windows and macOS, run only the `web` service in Docker for normal webcam use.
- On Linux, you can run both `web` and `worker` in Docker if the camera is available as `/dev/video*`.
- The web app defaults to proxy mode, so the worker token stays server-side.

## Repository Layout

The docs below assume both repositories sit next to each other:

```text
sentinelCam-web/
sentinelCam-worker/
```

If your worker repo lives somewhere else, set `SENTINELCAM_WORKER_DIR` before using the helper scripts in this repo.

## 1. Common Setup

### Prerequisites

Windows:

- Docker Desktop
- Python 3 with `py` launcher for the local worker
- PowerShell

Linux:

- Docker Engine with Compose
- Bash
- Python 3 with `venv`

macOS:

- Docker Desktop
- Bash or zsh
- Python 3 with `venv`

### Create `.env`

Copy `.env.example` to `.env` and set at least:

```dotenv
WORKER_TOKEN=replace-with-a-long-random-token
ADMIN_USER=admin
ADMIN_PASSWORD=replace-with-a-strong-password
WEBAUTHN_RP_ID=localhost
```

Optional but useful for first launch:

```dotenv
WORKER_SOURCE=synthetic
```

That starts the worker with generated test frames, so you can verify the stack even without a real camera.

Notes:

- `WORKER_TOKEN` must match the worker's `WEB_AUTH_TOKEN`.
- `ADMIN_PASSWORD` is only used on first startup when the web database is empty.
- `WEBAUTHN_RP_ID=localhost` is correct for local development.

## 2. Start On Windows

Recommended path:

- web in Docker
- worker locally in PowerShell

### Fastest Windows Start

From `sentinelCam-web`:

```powershell
Copy-Item .env.example .env
notepad .env
.\scripts\start-local-worker.ps1
```

What this does:

- starts the `web` container with `docker compose up -d --build web`
- reads `WORKER_TOKEN` and `WEB_PORT` from `.env`
- starts the sibling `sentinelCam-worker` locally
- sets `WEB_AUTH_TOKEN` and `WEB_ALLOWED_ORIGINS` for the worker

### Manual Windows Start

Start the web app:

```powershell
docker compose up -d --build web
```

Start the worker in a second PowerShell window:

```powershell
cd ..\sentinelCam-worker
$env:WEB_AUTH_TOKEN = "<same value as WORKER_TOKEN in sentinelCam-web\\.env>"
$env:WEB_ALLOWED_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"
.\run.bat --host 0.0.0.0 --source 0 --no-window --stream auto
```

No-camera smoke test:

```powershell
.\run.bat --host 0.0.0.0 --source synthetic --no-window --stream auto
```

Then open:

```text
http://localhost:3000
```

## 3. Start On Linux

You have two supported Linux paths:

1. Docker web + local worker
2. Full Docker stack

### Option A: Docker Web + Local Worker

From `sentinelCam-web`:

```bash
cp .env.example .env
nano .env
bash ./scripts/start-local-worker.sh
```

What this does:

- starts the `web` container with `docker compose up -d --build web`
- exports `WEB_AUTH_TOKEN` and `WEB_ALLOWED_ORIGINS`
- launches the sibling `sentinelCam-worker` with `--host 0.0.0.0 --no-window --stream auto`

Manual variant:

```bash
docker compose up -d --build web
cd ../sentinelCam-worker
export WEB_AUTH_TOKEN="<same value as WORKER_TOKEN in ../sentinelCam-web/.env>"
export WEB_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000"
bash ./run.sh --host 0.0.0.0 --source 0 --no-window --stream auto
```

No-camera smoke test:

```bash
bash ./run.sh --host 0.0.0.0 --source synthetic --no-window --stream auto
```

### Option B: Full Linux Docker Stack

From `sentinelCam-web`:

```bash
cp .env.example .env
nano .env
docker compose up -d --build
```

This starts:

- `web` on port `3000`
- `worker` on port `8080`

Important Linux Docker notes:

- default worker source is `WORKER_SOURCE=0`
- default camera device is `/dev/video0`
- use `WORKER_VIDEO_DEVICE=/dev/video2` for a different camera
- use `WORKER_SOURCE=rtsp://...` for RTSP or another remote source
- use `WORKER_SOURCE=synthetic` and `WORKER_VIDEO_DEVICE=/dev/null` if you want Docker smoke tests without a webcam

Then open:

```text
http://localhost:3000
```

## 4. Start On macOS

Recommended path:

- web in Docker
- worker locally in Terminal

There is no dedicated macOS helper script in this repo, so the manual path is the normal path.

### Manual macOS Start

From `sentinelCam-web`:

```bash
cp .env.example .env
open -e .env
docker compose up -d --build web
```

Then from `sentinelCam-worker`:

```bash
cd ../sentinelCam-worker
export WEB_AUTH_TOKEN="<same value as WORKER_TOKEN in ../sentinelCam-web/.env>"
export WEB_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000"
bash ./run.sh --host 0.0.0.0 --source 0 --no-window --stream auto
```

No-camera smoke test:

```bash
bash ./run.sh --host 0.0.0.0 --source synthetic --no-window --stream auto
```

macOS note:

- On first camera use, macOS may ask you to allow camera access for Terminal, iTerm, or Python.

Then open:

```text
http://localhost:3000
```

## 5. Run The Web App Without Docker

This is optional. The Docker path above is the recommended one.

When the web app runs directly on the host instead of Docker:

- the default `WORKER_BASE_URL=http://127.0.0.1:8080` usually works
- the worker can stay on `127.0.0.1`
- set `SC_PUBLIC=1` in `.env` if you want the web app to bind to `0.0.0.0`

### Windows

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run_web.py
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run_web.py
```

Then open:

```text
http://127.0.0.1:3000
```

## 6. Verify The Stack

Check the web app:

Windows PowerShell:

```powershell
docker compose ps
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/healthz | Select-Object -ExpandProperty Content
```

Linux / macOS:

```bash
docker compose ps
curl http://127.0.0.1:3000/healthz
```

Check the worker:

Windows PowerShell:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/health | Select-Object -ExpandProperty Content
```

Linux / macOS:

```bash
curl http://127.0.0.1:8080/health
```

Log in to the web app with `ADMIN_USER` / `ADMIN_PASSWORD` from `.env`.

## 7. Stop The Stack

Stop Docker services:

```bash
docker compose down
```

If the worker is running locally, stop it in its own terminal with `Ctrl+C`.

## Useful Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `WORKER_TOKEN` | none | Shared secret between web proxy and worker |
| `WORKER_BASE_URL` | app default `http://127.0.0.1:8080`, Compose default `http://host.docker.internal:8080` | Worker base URL used by the proxy |
| `WEB_PORT` | `3000` | Web app port |
| `SC_PUBLIC` | `0` | Bind to `0.0.0.0` when running the web app directly |
| `ADMIN_USER` | `admin` | Initial admin username for first boot |
| `ADMIN_PASSWORD` | none | Initial admin password for first boot |
| `WEBAUTHN_RP_ID` | `localhost` | Passkey relying-party ID for local development |
| `WORKER_SOURCE` | unset | Worker source for helper scripts and Linux Docker worker |
| `WORKER_BIND_HOST` | `0.0.0.0` | Bind host for helper-started Linux worker and Linux Docker worker |
| `WORKER_VIDEO_DEVICE` | `/dev/video0` | Linux webcam device for Docker passthrough |
| `WORKER_STREAM_MODE` | `auto` | Linux Docker worker stream mode |
| `WORKER_PERFORMANCE_PROFILE` | `auto` | Linux Docker worker performance tuning |
| `WORKER_STREAM_QUALITY` | `auto` | Linux Docker worker quality preset |
| `WORKER_JPEG_QUALITY` | `88` | Linux Docker worker MJPEG quality |
| `WORKER_WEBRTC_CODEC` | `auto` | Linux Docker worker preferred WebRTC codec |
| `WORKER_WEBRTC_BITRATE` | `-1` | Linux Docker worker WebRTC bitrate in kbps |
| `WORKER_WEBRTC_FPS` | `0` | Linux Docker worker WebRTC FPS |
| `WORKER_CAMERA_FPS` | `0` | Linux Docker worker requested capture FPS |

## Troubleshooting

- If the web container cannot reach the worker on Windows, Linux, or macOS, make sure the local worker was started with `--host 0.0.0.0` when the web app itself runs in Docker.
- If you do not have a webcam yet, use `WORKER_SOURCE=synthetic` or `--source synthetic`.
- If Linux Docker cannot open the camera, check `WORKER_VIDEO_DEVICE` and make sure the device exists.
- If the admin login does not work on a reused database, remember that `ADMIN_PASSWORD` is only consumed on first startup.

## Project Layout

```text
app/
  auth/
  dashboard/
  gallery/
  proxy/
  recording/
static/
templates/
scripts/
  start-local-worker.ps1
  start-local-worker.sh
  start-docker-worker.sh
docker-compose.yml
run_web.py
```

## Related Repos

- Worker: `../sentinelCam-worker`
- Edge capture node: `sentinelCam-edge`
