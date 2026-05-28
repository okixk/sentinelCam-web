# sentinelCam Web

Browser-first camera capture and gallery service. Runs as a Docker Compose
stack: a Traefik edge router, a web container that runs Apache in front of
the FastAPI app, a PostgreSQL database, and a wg-easy WireGuard server for
remote access. The separately-managed Probe-VA / aarestadt.info stack
(with its own Apache build) plugs into the same Traefik via a shared
docker network. Recordings are stored in a local Docker volume mounted
into the web container.

## What you get

- FastAPI app with password and passkey (WebAuthn) auth
- In-browser camera capture (image + video) via `MediaDevices` + `MediaRecorder`
- Recordings + thumbnails stored on local Docker storage
- Users, sessions, passkeys, and recording metadata in PostgreSQL
- Traefik as the edge router, terminating TLS with Cloudflare Origin
  Certificates that you drop into `./certs/`
- Apache inside the web container serving `/static/` directly and
  reverse-proxying everything else to uvicorn
- WireGuard VPN (wg-easy) for remote access

## Architecture and exposed ports

Only **three** ports are exposed to the outside world. Everything else lives on
the internal `sentinelcam` Docker network or on host loopback.

```text
                                                       +-------------------------------+
                  TCP 80   ---->  Traefik  ---Host---->|  web container                |
                  TCP 443  ---->  (TLS)     header     |  ├─ Apache :80                |---> postgres:5432
   internet  -->                            routing    |  └─ uvicorn 127.0.0.1:3000    |---> /data/recordings
                                              |        +-------------------------------+
                  UDP 1194 --->  wg-easy      |
                                 (WireGuard)  +------> probe-va container (own Apache build)
```

| External port     | Service          | Purpose                                                  |
|-------------------|------------------|----------------------------------------------------------|
| `80/tcp`          | traefik          | HTTP -> 301 to HTTPS                                     |
| `443/tcp`         | traefik          | HTTPS, Host-routed to `web` or `probe-va`                |
| `1194/udp`        | wg-easy          | WireGuard VPN (configurable via `WG_PORT`)               |
| (none public)     | postgres         | Internal-network only                                    |
| (none public)     | web              | Reached by Traefik via `proxy-net` on port 80 (Apache)   |
| (none public)     | probe-va         | Reached by Traefik via `proxy-net` on port 80 (Apache)   |
| `127.0.0.1:8080`  | traefik dashboard| Host loopback only; reach via SSH tunnel or VPN          |
| `127.0.0.1:51821` | wg-easy admin UI | Host loopback only; reach via SSH tunnel or VPN          |

> The VPN port is **WireGuard on UDP/1194**. WireGuard speaks WireGuard
> regardless of the port number it sits on — VPN clients need a
> **WireGuard** client, not an OpenVPN client. Set `WG_PORT` in `.env` to
> any UDP port you prefer.

### Internal network topology (VPN reachability)

```text
           VPN client (10.8.0.5)
                 │
                 │  WireGuard tunnel (UDP 1194)
                 ▼
        ┌────────────────────────────┐
        │  wg-easy container         │
        │  ├─ wg0:    10.8.0.1       │  ← tunnel side (WireGuard)
        │  └─ eth0:   172.30.0.20    │  ← bridge side (docker)
        │      SNAT / MASQUERADE     │
        └────────────────────────────┘
                 │
                 ▼  docker bridge "sentinelcam" (172.30.0.0/24)
        ┌────────────────┬────────────────┬────────────────┐
        │ traefik        │ web            │ postgres       │
        │ 172.30.0.10    │ 172.30.0.x     │ 172.30.0.x     │
        └────────────────┴────────────────┴────────────────┘
```

- `10.8.0.0/24` is the **WireGuard tunnel network**. Only WireGuard peers
  have addresses there: the wg-easy container itself (`10.8.0.1`) and the
  connected clients (`10.8.0.2`, `10.8.0.3`, …). Traefik, postgres and
  the web container do not speak WireGuard and have no `10.8.0.x` address.
- `172.30.0.0/24` is the **docker bridge** the containers use to talk to
  each other.
- Only wg-easy is dual-homed: it sits on both networks at once.

So from inside the VPN you can reach the admin UIs at:

| Service           | URL                                       | Notes                           |
|-------------------|-------------------------------------------|---------------------------------|
| wg-easy admin     | `http://10.8.0.1:51821`                   | tunnel-internal IP (wg-easy *is* this address) |
| wg-easy admin     | `http://172.30.0.20:51821`                | bridge IP (same UI, alternate path)            |
| Traefik dashboard | `http://172.30.0.10:8080/dashboard/`      | bridge IP only — Traefik has no 10.8.0.x       |

Why not give Traefik a `10.8.0.x` address too? That would make it a
WireGuard peer (with its own keypair and a `[Peer]` entry in the server
config) — extra moving parts for no real gain. Routing through the
wg-easy NAT to the docker bridge is the standard way to expose
sibling containers to VPN clients.

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

3. Drop your Cloudflare Origin Certs into `./certs/` (see
   `certs/README.md`):

   ```text
   certs/sentinelcam.crt    certs/sentinelcam.key
   certs/aarestadt.crt      certs/aarestadt.key
   ```

   These files are gitignored. The site hostnames must be Cloudflare-proxied
   ("orange cloud") for browsers to trust the chain. Keep
   `vpn.sentinelcam.ch` as "DNS only" — Cloudflare does not proxy UDP.

4. Create the shared docker network (one-time) and bring the stack up:

   ```bash
   docker network create proxy-net
   docker compose up -d --build
   ```

5. Open the web app:

   - On `https://<SC_PUBLIC_HOSTNAME>/` once the DNS resolves to your
     server through Cloudflare

6. Sign in with `ADMIN_USER` / `ADMIN_PASSWORD`. Press **Start camera** on the
   capture page, then **Capture image** or **Start recording**. Files are saved
   in the local Docker volume and show up in the gallery.

## Admin consoles

Both admin surfaces are bound to `127.0.0.1` on the host **and** reachable
via the WireGuard tunnel on fixed docker-bridge IPs (see
[Internal network topology](#internal-network-topology-vpn-reachability)):

| Service           | Via SSH tunnel (host loopback)    | Via active VPN                      |
|-------------------|-----------------------------------|-------------------------------------|
| Traefik dashboard | `http://127.0.0.1:8080/dashboard/`| `http://172.30.0.10:8080/dashboard/`|
| wg-easy admin     | `http://127.0.0.1:51821`          | `http://10.8.0.1:51821` *or* `http://172.30.0.20:51821` |

Neither dashboard is published on the public internet. The wg-easy admin
UI is also where you create new VPN client configs and download the
`.conf` / QR code.

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

TLS is terminated by Traefik using **Cloudflare Origin Certificates** that
you drop into `./certs/` (gitignored). One cert pair per zone:

```text
certs/sentinelcam.crt    certs/sentinelcam.key
certs/aarestadt.crt      certs/aarestadt.key
```

Generate them in the Cloudflare dashboard under **SSL/TLS → Origin Server
→ Create Certificate**. Cloudflare Origin Certs are signed by Cloudflare's
private origin CA, so browsers only trust them when the connection comes
through Cloudflare's edge — make sure the corresponding DNS records are
Cloudflare-**proxied** ("orange cloud"). `vpn.sentinelcam.ch` must stay
"DNS only" because Cloudflare does not proxy UDP.

There is no ACME client in this stack — Origin Certs are long-lived
(15-year default).

## Running tests

The previous HTTP-level smoke tests targeted the SQLite + worker-proxy
architecture and have been replaced by import-level checks for now:

```bash
python -m unittest discover -s tests
```

## Repository layout

```text
app/
  auth/         password + WebAuthn flow
  dashboard/    admin routes (users, sessions, ops)
  gallery/      gallery pages and JSON feed
  recording/    upload + serve + delete (local-storage backed)
  config.py     typed settings (env-driven)
  database.py   PostgreSQL pool + aiosqlite-compatible shim
  storage.py    local filesystem storage helpers
  main.py       FastAPI app + middleware
traefik/
  traefik.yml   static Traefik config (entrypoints, providers, dashboard)
  dynamic/      file-provider configs (Cloudflare Origin Cert wiring)
web-apache/
  sentinelcam.conf  Apache vhost serving /static/ and proxying to uvicorn
  supervisord.conf  supervisord program defs for Apache + uvicorn
certs/          Cloudflare Origin Cert dropzone (gitignored)
static/
templates/
docker-compose.yml
Dockerfile     Web container: Apache + uvicorn under supervisord
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
| `SC_PUBLIC_HOSTNAME` | DNS name used as the Traefik Host-router rule for the FastAPI app (default `sentinelcam.local`) |
| `WG_HOST` | Hostname/IP that VPN clients dial |
| `WG_PORT` | WireGuard UDP port (default 1194) |
| `WG_PASSWORD_HASH` | Bcrypt hash for the wg-easy admin UI |

See the **Quick start** section above for the minimal set of variables you
need to define in `.env`.
