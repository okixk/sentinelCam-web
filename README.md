# sentinelCam Web Development

`sentinelCam-web` is the browser UI for the sentinelCam stack.

It connects to a running [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker) instance, displays the processed stream, shows worker state, and provides runtime control, user management, and a recording gallery.

## Features

- Live **WebRTC** stream viewer (MJPEG fallback)
- Worker status display (preset, detection, pose, FPS, inference, codec, bitrate)
- Model switching with loading feedback
- Remote worker control (next/prev model, pose, overlay, inference, quit)
- **Capture** snapshots and **record** video clips from the stream
- **Gallery** with thumbnail grid, filtering, sorting, and pagination
- **User authentication** (session-based with Argon2 password hashing)
- **WebAuthn / Passkey** support
- **Admin dashboard** (user management, session management, worker control)
- **CSRF protection** and **Content Security Policy**
- **Rate limiting** on login
- **Docker** support
- Same-origin proxy for production-safe token handling

## Architecture

```
camera → sentinelCam-worker (local) → sentinelCam-web (Docker) → browser
```

The worker runs **locally** (needs webcam access), the web app runs in **Docker**.

## Quick start (Windows)

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker) cloned next to this repo
- Python 3.12+ (for the worker)

### 1. Configure environment

Create a `.env` file in the project root:

```dotenv
WORKER_TOKEN=<random-secret>
ADMIN_USER=admin
ADMIN_PASSWORD=<min-12-chars>
WEBAUTHN_RP_ID=localhost
```

### 2. Start the worker

Open a PowerShell terminal:

```powershell
cd path\to\sentinelCam-worker
$env:WEB_AUTH_TOKEN = "<same WORKER_TOKEN as in .env>"
$env:WEB_ALLOWED_ORIGINS = "http://localhost:3000"
.\run.bat --no-window --stream auto
```

Wait until you see:
```
INFO: Stream (MJPEG fallback): http://localhost:8080/stream.mjpg
```

### 3. Start the web app

Open a second PowerShell terminal:

```powershell
cd path\to\sentinelCam-web
docker compose up -d
```

### 4. Open

Go to **http://localhost:3000** and log in with the credentials from `.env`.

### Stopping

```powershell
# Stop the web container:
cd path\to\sentinelCam-web
docker compose down

# Stop the worker: press Ctrl+C in the worker terminal
```

## Why the worker runs locally

Docker on Windows cannot access the host webcam. The worker needs direct camera access, so it runs outside Docker. The web container connects to the local worker via `host.docker.internal`.

## Project structure

```
app/                    # FastAPI application
  auth/                 #   Authentication (login, WebAuthn, sessions)
  dashboard/            #   Admin dashboard
  gallery/              #   Recording gallery
  proxy/                #   Worker API proxy
  recording/            #   Capture & record endpoints
  config.py             #   Settings from environment
  database.py           #   SQLite (aiosqlite)
  security.py           #   Password hashing, CSRF, rate limiting
  main.py               #   App entry point, CSP middleware
static/                 # CSS & JavaScript
templates/              # Jinja2 HTML templates
docker-compose.yml      # Docker Compose (web service only)
Dockerfile              # Web app container
run_web.py              # Uvicorn launcher
web_server.py           # Standalone Python proxy (non-Docker alternative)
```

## Running without Docker

```bash
pip install -r requirements.txt
python run_web.py
```

Environment variables: `WORKER_BASE_URL`, `WORKER_TOKEN`, `WEB_PORT`, `PUBLIC`, `INITIAL_ADMIN_USER`, `INITIAL_ADMIN_PASSWORD`, `WEBAUTHN_RP_ID`.

## Network / Firewall

| Connection           | Port        | Protocol | Purpose                            |
|----------------------|-------------|----------|------------------------------------|
| Browser → Web        | 3000        | TCP      | Web UI + API proxy                 |
| Web → Worker         | 8080        | TCP      | HTTP proxy (API, signaling, MJPEG) |
| Browser ↔ Worker     | 50000–51000 | UDP      | WebRTC media (direct, optional)    |

- **MJPEG** runs fully through the proxy (no direct connection needed).
- **WebRTC** requires direct UDP between browser and worker.

## Related repositories

- **Worker:** [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker) – Camera capture & YOLO processing (required)
- **Edge:** [`sentinelCam-edge`](https://github.com/okixk/sentinelCam-edge) – Future camera-side capture node (optional)
