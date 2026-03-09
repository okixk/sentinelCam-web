# sentinelCam Web

`sentinelCam-web` is the browser UI for the sentinelCam stack.

It is a lightweight frontend that connects to a running [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker) instance, displays the processed stream, shows worker state, and sends runtime control commands.

## What this repo does

- connects to a worker over HTTP
- displays the processed MJPEG stream in the browser
- polls worker state
- sends commands like:
  - next / previous model
  - pose toggle
  - overlay toggle
  - inference toggle
  - quit

## What this repo does not do

This repo does **not** run YOLO itself.  
It does **not** capture cameras directly.  
All video processing happens in [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker).

## Where this repo fits

Typical flow:

`camera -> worker -> web browser`

Future distributed flow:

`camera -> sentinelCam-edge -> sentinelCam-worker -> sentinelCam-web`

## Related repositories

- **Processing backend:** [`sentinelCam-worker`](https://github.com/okixk/sentinelCam-worker)  
  Required. Provides the video stream and control API used by this UI.

- **Edge capture node:** [`sentinelCam-edge`](https://github.com/okixk/sentinelCam-edge)  
  Optional future camera-side component that will feed streams into the worker.

## Requirements

You need a running worker first.

Example worker base URL:

```text
http://127.0.0.1:8080
```

## Quick start

Right now this repo is just a static frontend.

You can simply open `index.html` in a browser, or serve it with any static file server.

Then enter the worker base URL in the input field and click **Connect**.

## Worker endpoints used by this UI

The UI expects the worker to provide:

- `GET /stream.mjpg`
- `GET /api/state`
- `POST /api/cmd`

So if your worker runs on `http://192.168.1.50:8080`, this UI will use:

- `http://192.168.1.50:8080/stream.mjpg`
- `http://192.168.1.50:8080/api/state`
- `http://192.168.1.50:8080/api/cmd`

## Features

- live MJPEG stream viewer
- worker status display
- current preset / detection / pose / FPS / inference state
- model switching with loading feedback
- basic remote worker control from the browser

## Files

- `index.html` — complete frontend UI

## Notes

- This repo is intentionally simple and static.
- It is best used on the same private network as the worker.
- If the worker is remote, make sure the worker host/port is reachable from the browser.

## Status

Current browser frontend for the sentinelCam stack.
