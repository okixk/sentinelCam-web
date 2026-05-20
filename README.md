# sentinelCam Web

Browser-first camera capture and gallery service. Runs as a single Docker
Compose stack with four services: the FastAPI app, an Apache reverse proxy,
a PostgreSQL database, and a wg-easy WireGuard server for remote access.
Recordings are stored in a local Docker volume mounted into the web container.

## What you get

- FastAPI app with password and passkey (WebAuthn) auth
- In-browser camera capture (image + video) via `MediaDevices` + `MediaRecorder`
- Recordings + thumbnails stored on local Docker storage
- Users, sessions, passkeys, and recording metadata in PostgreSQL
- Apache in front, terminating TLS (self-signed cert generated on first boot;
  swap in any cert pair `server.crt` + `server.key` in the `apache-certs`
  volume to replace it)
- WireGuard VPN (wg-easy) for remote access

## Architecture and exposed ports

Only **three** ports are exposed to the outside world. Everything else lives on
the internal `sentinelcam` Docker network or on host loopback.

```text
                                                    +-----------------+
                  TCP 80   ---->  Apache  ----+---->|                 |
                  TCP 443  ---->  (TLS)       |     |  web (FastAPI)  |---> postgres:5432
   internet  -->                              |     |   on web:3000   |---> /data/recordings (volume)
                  UDP 1194 ---->  wg-easy ----+     +-----------------+
                                  (WireGuard)
```

| External port | Service     | Purpose                                              |
|---------------|-------------|------------------------------------------------------|
| `80/tcp`      | apache      | HTTP -> 301 to HTTPS                                 |
| `443/tcp`     | apache      | HTTPS reverse-proxy to web                           |
| `1194/udp`    | wg-easy     | WireGuard VPN (configurable via `WG_PORT`)           |
| (none public) | postgres    | Internal-network only                                |
| (none public) | web         | Internal-network only (Apache talks to it on `:3000`)|
| `127.0.0.1:51821` | wg-easy admin UI | Host loopback only; reach via SSH tunnel or VPN  |

> The VPN port is **WireGuard on UDP/1194**. WireGuard speaks WireGuard
> regardless of the port number it sits on — VPN clients need a
> **WireGuard** client, not an OpenVPN client. Set `WG_PORT` in `.env` to
> any UDP port you prefer.

## Quick start

1. Generate a bcrypt hash for the wg-easy admin password:

   ```bash
   docker run --rm ghcr.io/wg-easy/wg-easy:14 wgpw 'your-vpn-admin-password'
   ```

2. Create a `.env` file in the repo root and fill in:

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

   - `https://localhost/` (Apache serves the self-signed cert generated on
     first boot — browsers will warn once; accept it, or replace the cert
     pair in the `apache-certs` volume)
   - If you set `SC_PUBLIC_HOSTNAME`, open `https://<your-hostname>/`

5. Sign in with `ADMIN_USER` / `ADMIN_PASSWORD`. Press **Start camera** on the
   capture page, then **Capture image** or **Start recording**. Files are saved
   in the local Docker volume and show up in the gallery.

## Admin consoles

The wg-easy admin UI is bound to `127.0.0.1`, so it is reachable from the host
but not the public internet:

- wg-easy admin: <http://127.0.0.1:51821> - log in with the password whose
  bcrypt hash you set in `WG_PASSWORD_HASH`

The recommended way to reach it from your laptop is an **SSH tunnel** (or via
the VPN once a client is configured — see below). Do not publish it to the
public internet.

## VPN access (WireGuard)

Once `docker compose up -d` is running, UDP `1194` on the server speaks
WireGuard. This is the only way to reach anything other than the web app from
outside the LAN.

### 1. Open the wg-easy admin UI from your laptop

The admin UI is loopback-only, so you tunnel it over SSH the first time:

```bash
ssh -L 51821:127.0.0.1:51821 <user>@<server>
```

Then open <http://127.0.0.1:51821> in your local browser and log in with the
password whose bcrypt hash you set as `WG_PASSWORD_HASH`.

### 2. Create a client config

In the wg-easy UI: **New Client** -> give it a name (e.g. `laptop`) -> it
generates a config. You can either:

- Scan the QR code with the mobile WireGuard app, or
- Download the `.conf` file and import it on the desktop client.

The generated config already points at `WG_HOST:WG_PORT` (so `<server>:1194/udp`
with our defaults) and tunnels `0.0.0.0/0`, meaning **all** your client traffic
is routed through the server while connected. If you only want LAN-style access
without sending unrelated browser traffic through the VPN, edit
`AllowedIPs = 10.8.0.0/24, <docker-bridge-subnet>` after import. Find the docker
bridge subnet with:

```bash
docker network inspect sentinelcam_sentinelcam --format '{{(index .IPAM.Config 0).Subnet}}'
```

### 3. Install the WireGuard client on your device

| OS              | Where to get it                                                          |
|-----------------|--------------------------------------------------------------------------|
| Windows         | <https://www.wireguard.com/install/> (official installer)                |
| macOS           | App Store: "WireGuard" (by WireGuard Development Team) or `brew install wireguard-tools` |
| iOS             | App Store: "WireGuard"                                                   |
| Android         | Play Store: "WireGuard" (or F-Droid for the FOSS build)                  |
| Linux (CLI)     | `sudo apt install wireguard` / `sudo dnf install wireguard-tools`        |
| Linux (Network Manager) | `sudo apt install network-manager-wireguard` (Ubuntu)            |

### 4. Import the config and connect

- **Mobile**: open the WireGuard app -> tap **+** -> **Scan from QR code** -> hold
  the camera over the QR code shown in wg-easy -> name the tunnel -> toggle it
  on.
- **Windows / macOS desktop app**: open WireGuard -> **Add Tunnel** ->
  **Import tunnel(s) from file** -> select the downloaded `.conf` -> click
  **Activate**.
- **Linux CLI**: save the file as `/etc/wireguard/sentinelcam.conf`,
  `sudo wg-quick up sentinelcam`. To disconnect: `sudo wg-quick down sentinelcam`.

### 5. Verify the tunnel

From the connected client:

```bash
# Should print the WireGuard server's tunnel-internal IP (default 10.8.0.1)
curl -s http://10.8.0.1:51821 -o /dev/null -w "%{http_code}\n"

# Reach the web app via the tunnel (no LAN exposure needed)
curl -k https://<server>/healthz
```

If both respond, the VPN is up. The wg-easy admin UI is now reachable at
<http://10.8.0.1:51821> as well — you no longer need the SSH tunnel for
day-to-day use.

### Reaching internal services via the VPN

With `AllowedIPs = 0.0.0.0/0` (the default), all your client traffic exits via
the server, so you can reach:

- The web app on `https://<server>/` (same as without VPN)
- The wg-easy admin UI on `http://10.8.0.1:51821`
- The host's SSH on `<server>:22` (if your server allows SSH on the LAN)
- Containers on the `sentinelcam` Docker bridge by their container IP (look up
  with `docker network inspect sentinelcam_sentinelcam`). The `web` service
  listens on port `3000` and `postgres` on port `5432`.

## TLS

- Default: the Apache entrypoint generates a 10-year self-signed certificate
  with `CN=<SC_PUBLIC_HOSTNAME>` + a SAN for `localhost` / `127.0.0.1`. The
  cert lives in the `apache-certs` named Docker volume.
- Production: drop your real `server.crt` and `server.key` into that volume
  (e.g. via `docker compose cp` or by mounting the volume's path) and restart
  Apache. The entrypoint only generates a cert when one is missing.
- For Let's Encrypt: terminate ACME externally (e.g. `certbot`) and copy the
  resulting cert + key into the volume. Apache itself does not run an ACME
  client in this stack.

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
apache/
  Dockerfile   httpd:2.4-alpine + ssl/proxy/headers modules
  entrypoint.sh self-signed cert generator
  httpd-vhosts.conf  HTTPS reverse-proxy config
static/
templates/
docker-compose.yml
Dockerfile
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
| `SC_PUBLIC_HOSTNAME` | DNS name Apache serves; used for the self-signed cert CN/SAN (default `sentinelcam.local`) |
| `WG_HOST` | Hostname/IP that VPN clients dial |
| `WG_PORT` | WireGuard UDP port (default 1194) |
| `WG_PASSWORD_HASH` | Bcrypt hash for the wg-easy admin UI |

See the **Quick start** section above for the minimal set of variables you
need to define in `.env`.
