# sentinelCam Web

Browser-first camera capture and gallery service. Runs as a single Docker
Compose stack with four services: the FastAPI app, a Caddy reverse proxy, a
PostgreSQL database, and a wg-easy WireGuard server for remote access.
Recordings are stored in a local Docker volume mounted into the web container.

## What you get

- FastAPI app with password and passkey (WebAuthn) auth
- In-browser camera capture (image + video) via `MediaDevices` + `MediaRecorder`
- Recordings + thumbnails stored on local Docker storage
- Users, sessions, passkeys, and recording metadata in PostgreSQL
- Caddy in front, terminating TLS (local CA by default, Let's Encrypt when you
  set `SC_PUBLIC_HOSTNAME` + `SC_TLS_EMAIL`)
- WireGuard VPN (wg-easy) for remote access

## Architecture

```text
browser
  |  HTTPS (or via VPN)
  v
caddy ---- http://web:3000 ----> fastapi (this repo)
                                |      |
                                |      +--> postgres:5432  (auth, sessions, recordings)
                                |      +--> /data/recordings (local Docker volume)
wg-easy -- UDP 51820/udp ------> host network (VPN tunnel for remote clients)
```

Only Caddy and wg-easy publish ports to the host. PostgreSQL stays on the
internal `sentinelcam` Docker network. Recording files stay in the
`recordings-data` Docker volume mounted at `/data/recordings` in the web
container.

## Quick start

1. Generate a bcrypt hash for the wg-easy admin password:

   ```bash
   docker run --rm ghcr.io/wg-easy/wg-easy:14 wgpw 'your-vpn-admin-password'
   ```

2. Copy `.env.example` to `.env` and fill in:

   - `ADMIN_USER` / `ADMIN_PASSWORD` - first-boot admin
   - `POSTGRES_PASSWORD` - database password
   - `LOCAL_STORAGE_PATH` - recording path inside the web container
   - `WG_HOST` - the hostname or IP your VPN clients dial
   - `WG_PASSWORD_HASH` - the bcrypt hash from step 1 (keep the single quotes)
   - `WEBAUTHN_RP_ID` - `localhost` for local dev, your DNS name otherwise

3. Bring the stack up:

   ```bash
   docker compose up -d --build
   ```

4. Open the web app on the host:

   - `https://localhost/` (Caddy will use its local CA; browsers warn the first
     time, so accept the cert or trust Caddy's local root)
   - If you set `SC_PUBLIC_HOSTNAME`, open `https://<your-hostname>/`

5. Sign in with `ADMIN_USER` / `ADMIN_PASSWORD`. Press **Start camera** on the
   capture page, then **Capture image** or **Start recording**. Files are saved
   in the local Docker volume and show up in the gallery.

## Admin consoles

The wg-easy admin UI is bound to `127.0.0.1`, so it is reachable from the host
but not the public internet:

- wg-easy admin: <http://127.0.0.1:51821> - log in with the password whose
  bcrypt hash you set in `WG_PASSWORD_HASH`

When you SSH to the host you can forward that port or reach it over the VPN. Do
not publish it to the public internet without an additional auth layer in
front.

## VPN access

The wg-easy server publishes UDP port `51820`. Create a client in the wg-easy
UI, scan or download the WireGuard config, import it into your WireGuard
client, and connect. Connected peers can reach `postgres` and the web service
by their service names on the internal network.

## TLS

- Default: Caddy issues a self-signed cert from its built-in local CA. The trust
  root lives at `/data/caddy/pki/authorities/local/root.crt` inside the `caddy`
  container; import it into your devices to silence cert warnings.
- Production: set `SC_PUBLIC_HOSTNAME` to a real DNS name pointing at the host,
  plus `SC_TLS_EMAIL`. Caddy will provision a Let's Encrypt cert automatically
  on first request.

## Running tests

The previous HTTP-level smoke tests targeted the SQLite + worker-proxy
architecture and have been replaced by import-level checks for now:

```bash
python -m unittest discover -s tests
```

## Repository layout

```text
app/
  auth/        password + WebAuthn flow
  dashboard/   admin routes (users, sessions, ops)
  gallery/     gallery pages and JSON feed
  recording/   upload + serve + delete (local-storage backed)
  config.py    typed settings (env-driven)
  database.py  PostgreSQL pool + aiosqlite-compatible shim
  storage.py   local filesystem storage helpers
  main.py      FastAPI app + middleware
static/
templates/
Caddyfile
docker-compose.yml
Dockerfile
.env.example
```

## Environment variables

| Variable | Purpose |
|---|---|
| `ADMIN_USER` / `ADMIN_PASSWORD` | First-boot admin credentials |
| `WEBAUTHN_RP_ID` | Passkey relying-party ID; must match the hostname |
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_LIMIT_WINDOW_MINUTES` | First-start IP login limit defaults |
| `LOCKOUT_THRESHOLD` / `LOCKOUT_DURATION_MINUTES` | First-start user lockout defaults |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Database credentials |
| `LOCAL_STORAGE_PATH` | Recording storage path inside the web container |
| `SC_PUBLIC_HOSTNAME` | DNS name Caddy serves (default `sentinelcam.local`) |
| `SC_TLS_EMAIL` | Let's Encrypt email (default `internal`) |
| `WG_HOST` | Hostname/IP that VPN clients dial |
| `WG_PORT` | WireGuard UDP port (default 51820) |
| `WG_PASSWORD_HASH` | Bcrypt hash for the wg-easy admin UI |

See `.env.example` for the full list with defaults.
