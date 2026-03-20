# sentinelCam Web

`sentinelCam-web` is the browser control surface for the sentinelCam stack.

It connects to a running `sentinelCam-worker`, displays the live stream, shows worker state, lets users control inference, and stores captures and recordings with authentication and sharing.

## What this repo contains

- FastAPI web app with session auth and WebAuthn/passkeys
- Same-origin proxy to the worker for state, commands, MJPEG, and WebRTC signaling
- SQLite-backed user, session, and recording storage
- Jinja-rendered UI for stream, gallery, recording detail, and admin workflows
- Docker packaging for the web app and optional Linux worker container

## Architecture

```text
camera/source -> sentinelCam-worker (port 8080) -> sentinelCam-web (port 3000) -> browser
```

- Windows and macOS typically run the worker locally because camera access usually needs the host OS.
- Linux can run the worker locally or in Docker.
- The web app defaults to proxy mode, so worker credentials stay server-side.

## Quick start

### 1. Create `.env`

Copy `.env.example` to `.env` and set at least:

```dotenv
WORKER_TOKEN=<shared-secret>
ADMIN_USER=admin
ADMIN_PASSWORD=<strong-password-min-12-chars>
WEBAUTHN_RP_ID=localhost
```

Important:

- `WORKER_TOKEN` must match the worker's `WEB_AUTH_TOKEN`.
- `ADMIN_PASSWORD` is only used when the database is empty on first start.
- The helper scripts in `scripts/` read from this `.env`.

### 2. Make sure the worker repo exists next to this repo

Expected default layout:

```text
sentinelCam-web/
sentinelCam-worker/
```

If your worker repo lives somewhere else, use `SENTINELCAM_WORKER_DIR` or the script flag shown below.

### 3. Start the stack

Choose one path:

#### Windows local worker

```powershell
.\scripts\start-local-worker.ps1
```

What it does:

- runs `docker compose up -d --build web` for the web service
- reads `WORKER_TOKEN` from `.env`
- sets `WEB_ALLOWED_ORIGINS` for local browser access
- launches the sibling `sentinelCam-worker` repo

#### Linux local worker

```bash
bash ./scripts/start-local-worker.sh
```

What it does:

- runs `docker compose up -d --build web` for the web service
- starts the worker locally with `--host 0.0.0.0`
- uses `WORKER_SOURCE` and `WORKER_BIND_HOST` from `.env` when set

#### Linux Docker worker

```bash
bash ./scripts/start-docker-worker.sh
```

This runs the full Linux stack from `docker-compose.yml`.

Notes:

- set `WORKER_VIDEO_DEVICE` in `.env` if your camera is not `/dev/video0`
- set `WORKER_SOURCE` in `.env` for RTSP or other non-camera sources
- use `WORKER_VIDEO_DEVICE=/dev/null` for remote-stream-only hosts

### 4. Open the app

Open:

```text
http://localhost:3000
```

Log in with the credentials from `.env`.

## Manual startup commands

Use these if you do not want the helper scripts.

### Web app

```bash
docker compose up -d --build
```

For a local worker workflow where only the web service should start:

```bash
docker compose up -d --build web
```

### Windows local worker

```powershell
cd ..\sentinelCam-worker
$env:WEB_AUTH_TOKEN = "<same WORKER_TOKEN as in .env>"
$env:WEB_ALLOWED_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"
.\run.bat --no-window --stream auto
```

### Linux local worker

```bash
cd ../sentinelCam-worker
export WEB_AUTH_TOKEN="<same WORKER_TOKEN as in .env>"
export WEB_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000"
./run.sh --host 0.0.0.0 --no-window --stream auto
```

If you want a non-default source:

```bash
./run.sh --host 0.0.0.0 --source rtsp://HOST:PORT/stream --no-window --stream auto
```

### Linux Docker worker

```bash
docker compose up -d --build
```

## Verification

Check the web app:

```bash
docker compose ps
curl http://127.0.0.1:3000/healthz
```

Check the worker:

```bash
curl http://127.0.0.1:8080/health
```

If you started the worker locally, its own terminal logs are usually the fastest place to diagnose startup issues.

## Stopping

Stop the web app:

```bash
docker compose down
```

If you started the worker locally, stop it in its own terminal with `Ctrl+C`.

If you started the Linux Docker worker manually, include the same override files in the `down` command.

## Docker files

- `docker-compose.yml`
  - primary compose file
  - starts the full Linux stack by default
  - for local-worker workflows, start only the web service with `docker compose up -d --build web`

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `WORKER_TOKEN` | none | Shared secret between web app and worker |
| `WORKER_BASE_URL` | app default `http://127.0.0.1:8080`, Compose default `http://host.docker.internal:8080` | Worker URL used by the proxy |
| `WEB_PORT` | `3000` | Port for the web app |
| `PUBLIC` | `0` | Bind to `0.0.0.0` when running the app directly |
| `ADMIN_USER` | `admin` | Initial admin username for first boot |
| `ADMIN_PASSWORD` | none | Initial admin password for first boot |
| `WEBAUTHN_RP_ID` | `localhost` | Passkey relying-party ID |
| `WORKER_SOURCE` | unset | Optional source passed to helper scripts or Linux worker container |
| `WORKER_VIDEO_DEVICE` | `/dev/video0` | Linux webcam device path for Docker passthrough |
| `WORKER_BIND_HOST` | `0.0.0.0` | Bind host for Linux worker container and local Linux helper script |

## Features

### Stream and control

- WebRTC viewer with MJPEG fallback
- worker status chips for preset, detection, FPS, inference, codec, bitrate
- remote worker controls for model switching, pose, overlay, inference, and quit
- in-browser capture and recording upload

### Authentication and security

- session-based auth with Argon2 password hashing
- WebAuthn/passkeys
- CSRF protection for state-changing requests
- security headers and CSP nonces
- login rate limiting and account lockout

### Gallery and admin

- gallery with filtering, sorting, pagination, and detail views
- private/shared recording model
- user and session management for admins

## Project layout

```text
app/
  auth/         authentication, sessions, WebAuthn routes
  dashboard/    admin routes
  gallery/      gallery pages
  proxy/        worker proxy routes
  recording/    upload, listing, file, delete, share routes
  config.py     environment-backed settings
  database.py   SQLite schema and first-run admin bootstrap
  main.py       FastAPI app entry point
static/
  css/style.css
  js/
templates/
scripts/
  start-local-worker.ps1
  start-local-worker.sh
  start-docker-worker.sh
Dockerfile
docker-compose.yml
run_web.py
web_server.py
```

## Legacy path

`web_server.py`, `index.html`, and `apache/sentinelcam.conf.example` describe an older standalone/static-hosting path. The Docker and FastAPI app flow in `run_web.py` + `app/main.py` is the primary path for active development.

## Related repos

- Worker: `sentinelCam-worker`
- Edge capture node: `sentinelCam-edge`
