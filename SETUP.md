# sentinelCam Web — Setup-Anleitung

Schritt-für-Schritt-Anleitung, wie du den **Web-Stack** (Traefik-Edge-Router,
Apache+FastAPI-Web-Container, PostgreSQL, wg-easy-VPN) startest und
konfigurierst. Die Architektur ist im [README](README.md) beschrieben —
dieses Dokument konzentriert sich auf den Betrieb.

> Diese Anleitung passt zum Branch `feature/infra-revamp`. Sie umfasst die
> WebRTC-SFU-Phase (aiortc), bei der Browser standardmässig per WebRTC
> zusehen und nur bei Fehlern auf MJPEG zurückfallen.

---

## 1. Voraussetzungen auf dem Host

- Linux-Host (getestet auf Ubuntu 24.04)
- Docker Engine ≥ 25 + Docker-Compose-Plugin
- 80/tcp, 443/tcp und 1194/udp frei (oder per `.env` auf andere Ports gelegt)
- DNS-/Hostname, unter dem der Server erreichbar sein soll (z. B.
  `sentinelcam.example.com`). Für reines Localhost-Testen reicht `localhost`.

Nichts weiter — alle Python-Abhängigkeiten (inklusive `aiortc` + `av` für die
WebRTC-SFU) werden vom Docker-Build installiert.

---

## 2. `.env` anlegen

Im Repo-Root eine Datei `.env` erstellen. Minimale Variablen:

```env
# Erst-Admin
ADMIN_USER=admin
ADMIN_PASSWORD=<starkes-passwort>

# Postgres
POSTGRES_PASSWORD=<db-passwort>

# WebAuthn / Passkeys: muss zur öffentlichen Domain passen,
# sonst lehnt der Browser Passkeys ab. Für lokale Tests: localhost
WEBAUTHN_RP_ID=sentinelcam.example.com

# Optionaler öffentlicher Hostname (wird im UI/Cookies verwendet)
SC_PUBLIC_HOSTNAME=sentinelcam.example.com

# VPN (wg-easy)
WG_HOST=sentinelcam.example.com       # Hostname/IP, den VPN-Clients dialen
WG_PASSWORD_HASH='<bcrypt-hash>'      # siehe nächster Schritt — Single-Quotes!

# Storage-Pfad im Web-Container (auf Volume gemountet)
LOCAL_STORAGE_PATH=/data/recordings
```

Bcrypt-Hash fürs wg-easy-Admin-Passwort erzeugen:

```bash
docker run --rm ghcr.io/wg-easy/wg-easy:14 wgpw 'dein-vpn-admin-passwort'
```

Der Hash enthält `$`-Zeichen — **immer in `'…'`** stehen lassen, sonst
expandiert die Shell die Zeichen weg.

---

## 3. Cloudflare Origin Certificates ablegen

Traefik terminiert TLS mit Cloudflare Origin Certs. Pro Domain ein Cert
im Cloudflare-Dashboard erstellen (SSL/TLS → Origin Server → Create
Certificate) und die vier Dateien nach `./certs/` legen:

```
certs/
├── sentinelcam.crt
├── sentinelcam.key
├── aarestadt.crt
└── aarestadt.key
```

Die Dateien sind via `.gitignore` ausgeschlossen. Damit Browser den
Origin-Cert akzeptieren, müssen `sentinelcam.ch` und `aarestadt.info`
in Cloudflare **proxied** (orange Wolke) sein. `vpn.sentinelcam.ch` muss
weiterhin **DNS only** (graue Wolke) sein, weil Cloudflare WireGuard-UDP
nicht durchreicht.

---

## 4. Stack hochfahren

```bash
docker compose up -d --build
```

Erststart dauert ein paar Minuten (Image-Build). Status prüfen:

```bash
docker compose ps
docker compose logs -f web      # Apache + uvicorn (multi-process)
docker compose logs -f traefik  # Edge-Routing + TLS
```

Healthcheck der App:

```bash
curl https://localhost/healthz
# -> {"ok":true}
```

Im Browser dann `https://<dein-host>/` öffnen, mit `ADMIN_USER` /
`ADMIN_PASSWORD` einloggen. Falls Browser-Warnung kommt: Cloudflare-Proxy
(orange Wolke) für den Hostnamen prüfen — ohne ihn akzeptiert kein Browser
das Origin Cert.

Das Traefik-Dashboard liegt auf `127.0.0.1:8080` — entweder per
SSH-Tunnel (`ssh -L 8080:127.0.0.1:8080 <user>@<server>`) oder über das
VPN erreichbar.

---

## 5. Kamera- und Worker-Tokens ausstellen

Im Admin-Bereich (`/admin`):

1. Kamera-Token: **Cameras → New camera**. Der Token (`sc-cam-<id>-…`) wird
   **einmalig** angezeigt — sofort in den `.env` des Pi-Streamers eintragen.
2. Worker-Token: **Workers → New worker**. Der Token (`sc-wrk-<id>-…`) geht
   in die `.env` der `sentinelCam-worker`-Instanz.

Tokens werden in der DB nur als Hash gespeichert. Verlorene Tokens lassen
sich nicht wiederherstellen — neuen Token ausstellen und den alten
widerrufen.

---

## 6. VPN-Setup

UDP/1194 ist nur WireGuard — Clients brauchen einen WireGuard-Client (kein
OpenVPN). Erst-Setup der Peers:

```bash
ssh -L 51821:127.0.0.1:51821 <user>@<server>
```

Im Browser dann `http://127.0.0.1:51821` öffnen und mit dem
wg-easy-Admin-Passwort einloggen. Neue Peers anlegen, QR-Code scannen oder
`.conf` herunterladen. Detail-Walkthrough: [VPN-conection.md](VPN-conection.md).

---

## 7. Streaming-Stack: was läuft wo

```
 Raspberry Pi (Kamera)  --wss--> /api/ingest/{cam_id}    \
                                                          \
                                                           > sentinelCam-web --MJPEG/WebRTC--> Browser
                                                          /
 sentinelCam-worker     --wss--> /api/worker/connect      /
   (YOLO + Overlay)
```

- **`/api/ingest/{cam_id}`**: Pi liefert rohe JPEGs (Bearer-Token).
- **`/api/worker/connect`**: Worker erhält die rohen JPEGs, schickt verarbeitete
  JPEGs (mit Overlay) zurück. Heartbeats als JSON-Textframes.
- **`/api/cameras/{id}/stream.mjpg`**: MJPEG-Stream (Multipart) — Fallback.
- **`/api/cameras/{id}/webrtc/offer`**: SDP-Offer-Endpoint der WebRTC-SFU
  (siehe nächster Abschnitt).

Wenn der Worker offline ist, gehen die rohen Pi-Frames direkt an die Browser
— so siehst du sofort, ob die Kamera-Kette aufrecht steht.

---

## 8. WebRTC-Viewer (Phase B, aiortc)

`app/streaming/webrtc.py` implementiert eine simple WebRTC-SFU: pro Browser
eine `RTCPeerConnection`, ein `MJPEGSource`-Track decodiert die JPEGs aus dem
`FrameHub` und sendet sie als WebRTC-Video-Track.

- **Default im Browser**: WebRTC. Bei Fehler (SDP, ICE, Stall > 5 s,
  `connectionstatechange` ∈ `failed/disconnected`) fällt das UI automatisch
  auf MJPEG zurück.
- **Manuelle Wahl**: Dropdown „Transport“ auf der Stream-Seite —
  `Auto` (Default), `WebRTC only` oder `MJPEG only`.
- **Reconnect-Knopf**: Erzwingt einen neuen WebRTC-Versuch (z. B. nach
  Netzwechsel oder wenn man von MJPEG zurück will).
- **Viewer-Limit**: 16 simultane Browser pro Kamera (`_MAX_VIEWERS` in
  `webrtc.py`). Bei Überschreitung → HTTP 503.
- **Aktive Viewer**: `/api/admin/ops` liefert `webrtc.viewers` für die
  Admin-Status-Seite.

### Voraussetzungen für WebRTC im Browser

- Verbindung muss **TLS** sein (Traefik macht das). Auf `http://localhost`
  funktioniert WebRTC ebenfalls; auf `http://<andere-domain>` blockt der
  Browser die `RTCPeerConnection`.
- Browser braucht `RTCPeerConnection` + H.264-Decode-Fähigkeit (alle
  aktuellen Browser haben das).
- ICE Servers sind **leer** konfiguriert (nur lokal/VPN-Zugriff erwartet).
  Falls Clients hinter NAT von ausserhalb deines VPN zugreifen, einen
  STUN/TURN-Server in `static/js/viewer.js` ergänzen.

### Troubleshooting WebRTC

| Symptom | Ursache / Fix |
|---|---|
| Stream startet, fällt nach 5 s auf MJPEG | Kein RTP angekommen — Firewall/NAT. STUN/TURN ergänzen oder MJPEG fest pinnen. |
| HTTP 400 „WebRTC negotiation failed“ | Ungültige SDP — Browser-Cache leeren oder Browser updaten. |
| HTTP 503 „max concurrent WebRTC viewers reached“ | 16-Viewer-Limit erreicht. `_MAX_VIEWERS` hochschrauben oder Sessions schliessen. |
| Video läuft, ist aber schwarz | Worker liefert keine Frames; SFU spielt das letzte JPEG ab oder einen schwarzen Placeholder. Worker-Status auf `/admin` prüfen. |

---

## 9. Tests / Smoke

```bash
docker compose exec web python -m unittest tests.test_smoke -v
```

Prüft Imports und ob alle Routen (inkl. `/api/cameras/{cam_id}/webrtc/offer`)
registriert sind.

---

## 10. Updates

```bash
git pull
docker compose up -d --build
```

Migrationsfreundlich — der Webcontainer ruft beim Start `init_db()` auf und
spielt fehlende Schemaänderungen automatisch ein.

---

## 11. Wichtige Pfade & Volumes

| Volume / Pfad | Inhalt |
|---|---|
| `postgres-data` | DB-Files |
| `recordings-data` → `${LOCAL_STORAGE_PATH}` | Aufnahmen + Thumbnails |
| `./certs/` (bind mount) | Cloudflare Origin Certs (gitignored) |
| `wireguard-data` | WG-Konfig + Peer-Keys |

Backup-Empfehlung: alle vier Volumes plus `.env`.
