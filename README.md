# sentinelCam Web Development

`sentinelCam-web` is the browser UI for the sentinelCam stack.

It connects to a running [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker) instance, displays the processed stream, shows worker state, and provides runtime control, user management, and a recording gallery.

## Features

### Stream & Control
- Live **WebRTC** stream viewer (MJPEG fallback)
- Worker status display (preset, detection, pose, FPS, inference, codec, bitrate)
- Model switching with loading feedback
- Remote worker control (next/prev model, pose, overlay, inference, quit)
- **Capture** snapshots and **record** video clips from the stream
- KI-Overlay toggle (switch between raw and AI-processed frames)

### Gallery & Sharing
- **Gallery** with thumbnail grid, filtering, sorting, and pagination
- Recordings stored with raw + overlay variants
- **Sharing**: Users can share individual recordings with all other users (🔓/🔒 toggle)
- **Admin access**: Admin can view and delete all recordings, but cannot share other users' recordings
- Download and delete from the detail view

### Authentication & Security
- **Session-based authentication** with Argon2 password hashing
- **WebAuthn / Passkey** support for all users (register from stream page or admin panel)
- Dynamic WebAuthn RP-ID (auto-detects domain from request, works with `localhost` and `127.0.0.1`)
- **Admin dashboard** (user management, session management, worker control, passkey management)
- **CSRF protection** on all state-changing endpoints
- **Content Security Policy** with script nonces
- **Rate limiting** and account lockout on login
- Same-origin proxy for production-safe token handling

### Infrastructure
- **Docker** support (hybrid architecture)
- SQLite database with automatic migrations
- Persistent data volume for DB and recordings
- Health check endpoint (`/health`)

## Architecture

```
camera → sentinelCam-worker (local, port 8080) → sentinelCam-web (Docker, port 3000) → browser
```

The worker runs **locally** (needs webcam access), the web app runs in **Docker**.  
The web container connects to the worker via `host.docker.internal:8080`.

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
ADMIN_PASSWORD=<strong-password-min-12-chars>
WEBAUTHN_RP_ID=localhost
```

> **Note:** `WORKER_TOKEN` must match the token used by the worker (`WEB_AUTH_TOKEN`).  
> `ADMIN_PASSWORD` must be at least 12 characters.

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

Check that the container is healthy:

```powershell
docker compose ps
```

### 4. Open

Go to **http://localhost:3000** and log in with the credentials from `.env`.

### Stopping

```powershell
# Stop the web container:
docker compose down

# Stop the worker: press Ctrl+C in the worker terminal
```

### Rebuilding after code changes

```powershell
docker compose up -d --build
```

## Why the worker runs locally

Docker on Windows cannot access the host webcam. The worker needs direct camera access, so it runs outside Docker. The web container connects to the local worker via `host.docker.internal`.

## User roles & permissions

| Action                  | Normal User        | Admin              |
|-------------------------|--------------------|--------------------|
| View own recordings     | ✅                 | ✅                 |
| View shared recordings  | ✅                 | ✅                 |
| View all recordings     | ❌                 | ✅                 |
| Share own recordings    | ✅                 | ✅                 |
| Share others' recordings| ❌                 | ❌                 |
| Delete own recordings   | ✅                 | ✅                 |
| Delete others' recordings| ❌                | ✅                 |
| Register passkeys       | ✅ (stream page)   | ✅ (admin panel)   |
| Manage users            | ❌                 | ✅                 |
| Control worker          | ✅ (stream page)   | ✅ (both pages)    |

## Project structure

```
app/                    # FastAPI application
  auth/                 #   Authentication (login, WebAuthn/Passkey, sessions)
  dashboard/            #   Admin dashboard
  gallery/              #   Recording gallery pages
  proxy/                #   Worker API proxy (stream, signaling, status)
  recording/            #   Capture, record, upload, share endpoints
  config.py             #   Settings from environment (pydantic-settings)
  database.py           #   SQLite (aiosqlite), schema & migrations
  security.py           #   Password hashing, CSRF, rate limiting
  main.py               #   App entry point, CSP middleware, routes
static/
  css/style.css         # Styles
  js/
    auth.js             #   Login & passkey authentication
    stream.js           #   Stream page (WebRTC, capture, passkey registration)
    admin.js            #   Admin panel (user/session mgmt, passkey mgmt)
    gallery.js          #   Gallery grid (filtering, sorting, share badges)
templates/              # Jinja2 HTML templates
  base.html             #   Layout with CSP nonce
  login.html            #   Login page
  stream.html           #   Main stream & control page
  admin.html            #   Admin dashboard
  gallery.html          #   Gallery grid
  gallery_detail.html   #   Recording detail with overlay toggle & sharing
docker-compose.yml      # Docker Compose (web service only)
Dockerfile              # Python 3.13-slim container
run_web.py              # Uvicorn launcher
web_server.py           # Standalone Python proxy (non-Docker alternative)
```

## Environment variables

| Variable                   | Default        | Description                                |
|----------------------------|----------------|--------------------------------------------|
| `WORKER_BASE_URL`          | `http://127.0.0.1:8080` | Worker connection URL              |
| `WORKER_TOKEN`             | *(required)*   | Shared secret for worker authentication    |
| `WEB_PORT`                 | `3000`         | Port the web server listens on             |
| `PUBLIC`                   | `0`            | Bind to `0.0.0.0` when `1`                 |
| `INITIAL_ADMIN_USER`       | —              | Create admin user on first start           |
| `INITIAL_ADMIN_PASSWORD`   | —              | Password for initial admin (min 12 chars)  |
| `WEBAUTHN_RP_ID`           | `localhost`    | WebAuthn Relying Party ID (domain)         |
| `SECRET_KEY`               | *(auto)*       | Session signing key (auto-generated if empty) |
| `SESSION_MAX_AGE_HOURS`    | `8`            | Session expiry                             |
| `LOGIN_RATE_LIMIT`         | `5`            | Max login attempts per minute              |
| `LOCKOUT_THRESHOLD`        | `10`           | Failed logins before account lockout       |
| `LOCKOUT_DURATION_MINUTES` | `30`           | Lockout duration                           |
| `MAX_UPLOAD_SIZE_MB`       | `100`          | Max recording upload size                  |
| `STORAGE_QUOTA_PER_USER_MB`| `500`          | Storage quota per user                     |

## Running without Docker

```bash
pip install -r requirements.txt
python run_web.py
```

Set the environment variables listed above, or create a `.env` file.

## Network / Firewall

| Connection           | Port        | Protocol | Purpose                            |
|----------------------|-------------|----------|------------------------------------|
| Browser → Web        | 3000        | TCP      | Web UI + API proxy                 |
| Web → Worker         | 8080        | TCP      | HTTP proxy (API, signaling, MJPEG) |
| Browser ↔ Worker     | 40000–40100 | UDP      | WebRTC media (direct, optional)    |

- **MJPEG** runs fully through the proxy (no direct connection needed).
- **WebRTC** requires direct UDP between browser and worker.

## API endpoints

| Method   | Path                                | Auth     | Description                         |
|----------|-------------------------------------|----------|-------------------------------------|
| `POST`   | `/auth/login`                       | —        | Login with username/password        |
| `POST`   | `/auth/logout`                      | Session  | Logout                              |
| `POST`   | `/auth/webauthn/register/begin`     | Session  | Start passkey registration          |
| `POST`   | `/auth/webauthn/register/complete`  | Session  | Finish passkey registration         |
| `POST`   | `/auth/webauthn/login/begin`        | —        | Start passkey login                 |
| `POST`   | `/auth/webauthn/login/complete`     | —        | Finish passkey login                |
| `GET`    | `/auth/webauthn/credentials`        | Session  | List user's passkeys                |
| `DELETE` | `/auth/webauthn/credentials/{id}`   | Session  | Delete a passkey                    |
| `GET`    | `/api/recordings`                   | Session  | List recordings (own + shared)      |
| `GET`    | `/api/recordings/{id}`              | Session  | Recording metadata                  |
| `GET`    | `/api/recordings/{id}/file`         | Session  | Serve recording file                |
| `GET`    | `/api/recordings/{id}/thumbnail`    | Session  | Serve thumbnail                     |
| `POST`   | `/api/recordings/upload`            | Session  | Upload a captured recording         |
| `DELETE` | `/api/recordings/{id}`              | Session  | Delete recording (owner or admin)   |
| `PATCH`  | `/api/recordings/{id}/share`        | Session  | Toggle sharing (owner only)         |
| `GET`    | `/api/proxy/*`                      | Session  | Proxy to worker API                 |
| `GET`    | `/health`                           | —        | Health check                        |

## Related repositories

- **Worker:** [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker) – Camera capture & YOLO processing (required)
- **Edge:** [`sentinelCam-edge`](https://github.com/okixk/sentinelCam-edge) – Future camera-side capture node (optional)
