# sentinelCam Worker

## Web-Ausgabe (Browser)

Standardmäßig zeigt `webcam.py` das Ergebnis in einem OpenCV-Fenster.
Wenn du es **headless** (z.B. auf einem Server/RPi) laufen lassen willst, kannst du den Stream auf einer Webseite anzeigen:

### 1) Schnell & ohne Zusatz-Dependencies: MJPEG

```bash
./run.sh --web --stream mjpeg --port 8080
```

Dann im Browser öffnen:

* `http://<host>:8080/`

MJPEG ist sehr kompatibel, aber nicht der effizienteste Codec.

### 2) Niedrigere Latenz: WebRTC (empfohlen)

WebRTC liefert typischerweise die geringste End-to-End-Latenz im Browser.

Zusatz-Pakete installieren:

```bash
python -m pip install aiohttp aiortc av
```

Start:

```bash
./run.sh --web --stream webrtc --port 8080 --webrtc-codec auto
```

Codec-Präferenz (best-effort):

* `--webrtc-codec h264` (meist beste Kompatibilität / HW-Encoding möglich)
* `--webrtc-codec vp8` / `vp9`
* `--webrtc-codec av1` (nur wenn Browser + FFmpeg/PyAV Encoder unterstützen; oft CPU-lastig)

Wenn `--stream auto` genutzt wird (Default) und WebRTC nicht verfügbar ist, fällt der Worker automatisch auf MJPEG zurück.

## Tipps für weniger Latenz

* `--max-fps` hoch setzen (oder `--max-fps 0` für uncapped), damit der Worker nicht künstlich schläft.
* `--width/--height` und `--imgsz` reduzieren, wenn Inference zu langsam ist.
* Für RTSP-Quellen kann eine kleine Buffer-Queue helfen (wir setzen best-effort `CAP_PROP_BUFFERSIZE=1`).

> Hinweis: Der eingebaute Web-Server hat **keine Auth**. Für echte Deployments bitte hinter Reverse-Proxy/VPN betreiben.
# sentinelCam-web
