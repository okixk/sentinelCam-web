# VPN connection guide

Everything except the public-facing HTTPS sites (port 443, routed by
Traefik) and the WireGuard endpoint (UDP on 1194, on `vpn.sentinelcam.ch`)
is closed to the public network. This file walks through how to connect
to the VPN so you can reach the internal services (the Traefik dashboard,
the `wg-easy` admin UI, the Postgres database, the web container's debug
port, the host's SSH).

The server runs **WireGuard** on UDP `1194`. WireGuard works on any UDP port
— the protocol on the wire is still WireGuard. You need a **WireGuard
client**, not an OpenVPN client.

---

## 1. Server-side prep (run once)

Generate the bcrypt hash for the `wg-easy` admin UI password:

```bash
docker run --rm ghcr.io/wg-easy/wg-easy:14 wgpw 'your-vpn-admin-password'
```

Paste the resulting `$2a$...` string into `.env` as
`WG_PASSWORD_HASH='...'` (keep the single quotes so Docker Compose does not
interpret the `$` signs).

Set `WG_HOST` in `.env` to the **public** hostname or IP that VPN clients
will dial. For LAN-only access this is the server's LAN IP; for remote use
it's the public IP or a DNS name pointing at it.

Bring the stack up:

```bash
docker compose up -d --build
```

## 2. Open the wg-easy admin UI from your laptop

The admin UI is bound to `127.0.0.1` on the server, so it is not reachable
from the public network. The first time you tunnel it over SSH:

```bash
ssh -L 51821:127.0.0.1:51821 administrator@<server>
```

While that SSH session is open, point your laptop browser at
<http://127.0.0.1:51821> and log in with the password whose bcrypt hash
you set in `WG_PASSWORD_HASH`.

## 3. Create a client config in wg-easy

In the admin UI:

1. Click **+ New Client**.
2. Give it a name (`laptop`, `phone`, `office`, …).
3. wg-easy shows a **QR code** and offers the `.conf` file for download.

Create one client per device. Configs include the server endpoint, the
client's tunnel IP (typically `10.8.0.X`), the server's public key, and
the client's freshly generated keypair.

## 4. Install the WireGuard client

| Platform           | Source                                                    |
|--------------------|-----------------------------------------------------------|
| Windows            | <https://www.wireguard.com/install/> (official installer) |
| macOS              | App Store: "WireGuard" (WireGuard Development Team)       |
| iOS / iPadOS       | App Store: "WireGuard"                                    |
| Android            | Play Store: "WireGuard" (or F-Droid for the FOSS build)   |
| Linux (CLI)        | `sudo apt install wireguard` / `sudo dnf install wireguard-tools` |
| Linux (NetworkManager UI) | `sudo apt install network-manager-wireguard`       |

## 5. Import and connect

**Mobile (iOS / Android)**

1. Open the WireGuard app -> tap **+** -> **Scan from QR code**.
2. Hold the camera over the QR code in wg-easy.
3. Name the tunnel, toggle it on.

**Windows / macOS desktop app**

1. Open WireGuard -> **Add Tunnel** -> **Import tunnel(s) from file**.
2. Pick the `.conf` you downloaded from wg-easy.
3. Click **Activate**.

**Linux CLI**

```bash
sudo cp ~/Downloads/laptop.conf /etc/wireguard/sentinelcam.conf
sudo wg-quick up sentinelcam
# disconnect:
sudo wg-quick down sentinelcam
```

## 6. Verify the tunnel

From the connected client:

```bash
# The WireGuard server's tunnel-internal IP should respond.
# Default is 10.8.0.1; substitute whatever wg-easy assigned.
curl -s -o /dev/null -w "%{http_code}\n" http://10.8.0.1:51821

# The web app is reachable through the tunnel too:
curl -k https://<server>/healthz
```

If both produce a non-error status, the VPN is up. The wg-easy admin UI
is now reachable directly at <http://10.8.0.1:51821> — you no longer need
the SSH tunnel for day-to-day use.

## 7. What you can reach via the VPN

With the default `AllowedIPs = 0.0.0.0/0, ::/0` (full tunnel) all traffic
from the client exits via the server, so you can reach:

- The web app on `https://<server>/`
- The Traefik dashboard on `http://10.8.0.1:8080/dashboard/`
- The wg-easy admin UI on `http://10.8.0.1:51821`
- The server's SSH on `<server>:22` if SSH is permitted on the LAN
- Containers on the `sentinelcam` Docker bridge by their internal IP.
  Find the subnet:

  ```bash
  docker network inspect sentinelcam_sentinelcam \
      --format '{{(index .IPAM.Config 0).Subnet}}'
  ```

  Postgres listens on `5432`; the web container listens on `3000`.

## 8. Split-tunnel (optional)

If you do not want unrelated browser traffic to flow through the server,
edit the client config after import:

```ini
[Peer]
AllowedIPs = 10.8.0.0/24, <docker-bridge-subnet>
```

Then `wg-quick down` and `wg-quick up` (or toggle the tunnel in the GUI).

## 9. Troubleshooting

- **Tunnel says connected but nothing is reachable** -> the server's
  firewall is dropping forwarded traffic. Verify
  `net.ipv4.ip_forward = 1` (the wg-easy container sets this for itself
  but a host firewall can still block it). On the host:
  `sudo ufw status` / `sudo iptables -L FORWARD`.
- **wg-easy admin UI shows the new client as "0 KB" forever** -> the
  client never reached the server. Check that UDP `1194` is open from
  the client's network and that `WG_HOST` resolves to the public IP.
- **DNS does not resolve through the tunnel** -> set `WG_DEFAULT_DNS` in
  `.env` to a reachable resolver (`1.1.1.1`, `8.8.8.8`, your LAN
  resolver), then regenerate the client config.
