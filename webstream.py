#!/usr/bin/env python3
"""webstream.py

Lightweight web streaming helpers for sentinelCam-worker.

Goals:
  - Show the annotated output on a web page.
  - Provide a low-latency option via WebRTC (when optional deps are installed).
  - Provide a zero-extra-deps fallback via MJPEG over HTTP.

Notes:
  - In "web" mode, the OpenCV window (and hotkeys) are disabled. This module
    therefore also exposes a tiny control API so you can:
      * next/prev preset
      * toggle pose
      * stop the worker

MJPEG server:
  - Uses only Python stdlib (http.server), compatible with any browser.

WebRTC server:
  - Optional; requires: aiohttp + aiortc + av
  - Much lower end-to-end latency than MJPEG.
  - Codec preference can be set (auto/h264/vp8/vp9/av1). AV1 depends on your
    FFmpeg build and is often CPU-heavy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional

import cv2


# -----------------------------
# Frame hub
# -----------------------------

@dataclass
class FramePacket:
    """Immutable snapshot of one annotated video frame.

    Produced by FrameHub.update() on every camera tick.
    Consumed by MJPEG (jpg) and WebRTC (bgr) endpoints.
    """
    ts: float       # monotonic wall-clock timestamp (time.time())
    bgr: "object"   # numpy.ndarray in BGR colour order (OpenCV convention)
    jpg: bytes      # pre-encoded JPEG bytes for MJPEG streaming


class FrameHub:
    """Thread-safe latest-frame buffer.

    - update() stores the latest annotated frame and a JPEG representation.
    - wait() blocks until a newer frame than `after_ts` exists.
    """

    def __init__(self, jpeg_quality: int = 80):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pkt: Optional[FramePacket] = None
        self._jpeg_quality = int(max(10, min(95, jpeg_quality)))

        # Optional asyncio bridge (used by WebRTC).
        # When bound, update() will signal an asyncio.Event via call_soon_threadsafe.
        self._loop = None
        self._ae = None

    def set_jpeg_quality(self, q: int) -> None:
        """Update JPEG quality used for MJPEG / latest.jpg encodes.

        Safe to call at runtime from other threads.
        """
        try:
            q = int(q)
        except Exception:
            return
        q = int(max(10, min(95, q)))
        with self._lock:
            self._jpeg_quality = q

    def bind_asyncio(self, loop) -> None:
        """Bind an asyncio loop so wait_async() can be truly async.

        Safe to call multiple times; last call wins.
        """
        try:
            import asyncio  # local import to keep module usable without it

            self._loop = loop
            self._ae = asyncio.Event()
        except Exception:
            self._loop = None
            self._ae = None

    def update(self, frame_bgr) -> None:
        """Store a new annotated frame from the producer (webcam) thread.

        Encodes the frame to JPEG immediately so MJPEG clients can
        stream it without re-encoding. Wakes all blocking waiters
        (MJPEG threads and the async WebRTC bridge).
        """
        ts = time.time()
        # Pre-encode to JPEG once; all MJPEG clients share this copy.
        ok, enc = cv2.imencode(
            ".jpg",
            frame_bgr,
            [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality],
        )
        if not ok:
            return
        jpg = enc.tobytes()
        # Swap the latest packet atomically and wake consumers.
        with self._cv:
            self._pkt = FramePacket(ts=ts, bgr=frame_bgr, jpg=jpg)
            self._cv.notify_all()

        # Wake async waiters (WebRTC) without blocking the event loop.
        if self._loop is not None and self._ae is not None:
            try:
                self._loop.call_soon_threadsafe(self._ae.set)
            except Exception:
                pass

    def latest(self) -> Optional[FramePacket]:
        """Return the most recent frame without blocking (non-blocking peek)."""
        with self._lock:
            return self._pkt

    def wait(self, after_ts: float, timeout: float = 5.0) -> Optional[FramePacket]:
        """Wait for a frame newer than after_ts."""
        end = time.time() + timeout
        with self._cv:
            while True:
                pkt = self._pkt
                if pkt is not None and pkt.ts > after_ts:
                    return pkt
                remaining = end - time.time()
                if remaining <= 0:
                    return pkt
                self._cv.wait(timeout=remaining)

    async def wait_async(self, after_ts: float, timeout: float = 5.0) -> Optional[FramePacket]:
        """Async wait for a frame newer than after_ts.

        If bind_asyncio() has been called, this will not block the event loop.
        Otherwise it falls back to a background thread.
        """
        try:
            import asyncio
        except Exception:
            # Extremely defensive; should never happen.
            return self.wait(after_ts=after_ts, timeout=timeout)

        # If not bound, run the blocking wait in a thread.
        if self._loop is None or self._ae is None:
            return await asyncio.to_thread(self.wait, after_ts, timeout)

        loop = asyncio.get_running_loop()
        end = loop.time() + float(timeout)
        while True:
            pkt = self.latest()
            if pkt is not None and pkt.ts > after_ts:
                return pkt

            remaining = end - loop.time()
            if remaining <= 0:
                return pkt

            # Wait for a producer signal.
            try:
                self._ae.clear()
                await asyncio.wait_for(self._ae.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return self.latest()


# -----------------------------
# Control API
# -----------------------------

@dataclass
class ControlAPI:
    """Callbacks provided by webcam.py to control the camera worker.

    get_state  – returns a dict with current preset, fps, pose/overlay/inference flags, etc.
    command    – dispatches a control command (next/prev preset, toggle_pose, stop, …).
    """
    get_state: Callable[[], Dict[str, Any]]
    command: Callable[[Any], None]


# -----------------------------
# Stream quality (runtime adjustable from the web UI)
# -----------------------------

@dataclass
class StreamQuality:
    """Runtime stream quality controls."""
    preset: str = "high"
    bitrate_kbps: int = 2500
    scale: float = 1.0
    fps: int = 30
    # Optional fixed target resolution for the *stream output*.
    # IMPORTANT: This must only be applied at WebRTC track creation time
    # (i.e., via reconnect) because many Windows encoder builds do not
    # tolerate changing resolution mid-stream.
    target_w: Optional[int] = 1280
    target_h: Optional[int] = 720
    jpeg_quality: int = 80
    # NOTE: Quality updates can be triggered from the aiohttp event-loop thread
    # while video frames are produced from a separate producer thread.
    # We also call snapshot() from within update_from().
    # A non-reentrant Lock would deadlock (update_from() -> snapshot()).
    # Use RLock to guarantee safety.
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        """Return a thread-safe copy of all quality settings as a plain dict.

        Used by API responses and by HubVideoTrack to freeze quality at
        connection setup time.
        """
        with self._lock:
            return {
                "preset": self.preset,
                "bitrate_kbps": int(self.bitrate_kbps),
                "scale": float(self.scale),
                "fps": int(self.fps),
                "target_w": int(self.target_w) if self.target_w else None,
                "target_h": int(self.target_h) if self.target_h else None,
                "jpeg_quality": int(self.jpeg_quality),
            }

    def _apply_preset(self, preset: str) -> None:
        """Overwrite all quality fields to match a named preset.

        Supported presets:
          auto   – native resolution, 30 fps, no fixed target
          low    – 640×360, scale 2×, 15 fps, 500 kbps
          medium – 960×540, 24 fps, 1200 kbps
          high   – 1280×720 (HD), 30 fps, 2500 kbps  (default)
          ultra  – 1920×1080 (FHD), 30 fps, 8000 kbps
          custom – only sets the preset label; individual fields untouched
        """
        p = (preset or "").strip().lower()
        if p == "auto":
            self.preset = "auto"
            self.scale = 1.0
            self.fps = 30
            self.target_w = None
            self.target_h = None
            return
        if p == "low":
            self.preset, self.bitrate_kbps, self.scale, self.fps = "low", 500, 2.0, 15
            self.target_w, self.target_h = 640, 360
        elif p == "medium":
            self.preset, self.bitrate_kbps, self.scale, self.fps = "medium", 1200, 1.0, 24
            self.target_w, self.target_h = 960, 540
        elif p == "high":
            self.preset, self.bitrate_kbps, self.scale, self.fps = "high", 2500, 1.0, 30
            self.target_w, self.target_h = 1280, 720
        elif p == "ultra":
            # NOTE: "Ultra" should prioritize Full HD, but we keep fps=30 as a
            # sensible default because many webcams are limited to 30 fps at FHD.
            self.preset, self.bitrate_kbps, self.scale, self.fps = "ultra", 8000, 1.0, 30
            self.target_w, self.target_h = 1920, 1080
        elif p == "custom":
            self.preset = "custom"
        else:
            self.preset = p or self.preset

    def update_from(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Merge partial quality updates from the web UI into current settings.

        Each field is independently clamped to safe bounds. If a ``preset``
        key is present the corresponding preset is applied first, then any
        explicit overrides are layered on top.

        Returns the resulting quality snapshot (safe to send back to the client).
        """
        if not isinstance(data, dict):
            data = {}
        with self._lock:
            if "preset" in data:
                self._apply_preset(str(data.get("preset") or ""))

            def _clamp_int(v, lo, hi, default):
                try: v = int(v)
                except Exception: return int(default)
                return int(max(lo, min(hi, v)))

            def _clamp_float(v, lo, hi, default):
                try: v = float(v)
                except Exception: return float(default)
                return float(max(lo, min(hi, v)))

            if "bitrate_kbps" in data:
                self.bitrate_kbps = _clamp_int(data.get("bitrate_kbps"), 500, 10000, self.bitrate_kbps)
            if "scale" in data:
                self.scale = _clamp_float(data.get("scale"), 1.0, 4.0, self.scale)
            if "fps" in data:
                self.fps = _clamp_int(data.get("fps"), 5, 60, self.fps)
            # Optional fixed stream resolution. 0/None disables.
            if "target_w" in data:
                tw = _clamp_int(data.get("target_w"), 0, 7680, int(self.target_w or 0))
                self.target_w = int(tw) if int(tw) > 0 else None
            if "target_h" in data:
                th = _clamp_int(data.get("target_h"), 0, 4320, int(self.target_h or 0))
                self.target_h = int(th) if int(th) > 0 else None
            if "jpeg_quality" in data:
                self.jpeg_quality = _clamp_int(data.get("jpeg_quality"), 10, 95, self.jpeg_quality)

            return self.snapshot()



# -----------------------------
# MJPEG server (stdlib)
# -----------------------------

def _controls_html() -> str:
    """Return the shared HTML snippet for the control bar and inline JavaScript.

    Contains buttons (prev/next preset, pose, overlay, inference, stop),
    quality controls (preset dropdown, bitrate slider, scale, fps), status
    pills, and the JS logic for command queuing, quality debounce, keyboard
    shortcuts, and periodic state polling via /api/state.
    """
    return """
<div class="controls" id="controls">
  <button id="reconnect" type="button" class="muted" title="Nur WebRTC: Verbindung neu aufbauen">Reconnect</button>
  <button id="prev" data-cmd="prev">◀︎ Prev</button>
  <button id="next" data-cmd="next">Next ▶︎</button>
  <button id="pose" data-cmd="toggle_pose">Pose: …</button>
  <button id="overlay" data-cmd="toggle_overlay">Overlay: …</button>
  <button id="infer" data-cmd="toggle_inference">Model: …</button>

  <span class="pill muted" style="display:inline-flex; gap:8px; align-items:center">
    Quality:
    <select id="qPreset">
      <option value="auto">Auto</option>
      <option value="low">Low</option>
      <option value="medium">Medium</option>
      <option value="high" selected>High</option>
      <option value="ultra">Ultra</option>
      <option value="custom">Custom</option>
    </select>
  </span>

  <span class="pill muted" style="display:inline-flex; gap:8px; align-items:center">
    Bitrate: <input id="qBitrate" type="range" min="500" max="10000" step="50" value="2500" />
    <span id="qBitrateVal">2500</span> kbps
  </span>

  <span class="pill muted" style="display:inline-flex; gap:8px; align-items:center">
    Scale:
    <select id="qScale">
      <option value="1">1×</option>
      <option value="1.5">1.5×</option>
      <option value="2">2×</option>
      <option value="3">3×</option>
      <option value="4">4×</option>
    </select>
  </span>

  <span class="pill muted" style="display:inline-flex; gap:8px; align-items:center">
    FPS:
    <select id="qFps">
      <option value="10">10</option>
      <option value="15">15</option>
      <option value="24">24</option>
      <option value="30" selected>30</option>
      <option value="60">60</option>
    </select>
  </span>

  <button id="stop" data-cmd="stop" class="danger">Stop</button>
  <span class="pill" id="statePill">state: …</span>
  <span class="pill muted" id="videoPill">video: …</span>
  <span class="pill muted" id="bwPill">server out: … kbps</span>
  <span class="pill warn" id="upPill" style="display:none" title=""></span>
  <span class="pill muted" id="actionPill" style="display:none"></span>
</div>
<div class="muted" style="margin-top:6px">
  Hotkeys: <code>M</code>/<code>N</code> preset · <code>P</code> pose · <code>O</code> overlay · <code>I</code> model · <code>Q</code> stop
</div>

<script>
(() => {
  const byId = (id) => document.getElementById(id);

  const btnIds = ["prev","next","pose","overlay","infer","stop"];
  const statePill = byId("statePill");
  const videoPill = byId("videoPill");
  const bwPill = byId("bwPill");
  const upPill = byId("upPill");
  const actionPill = byId("actionPill");

  const qPreset = byId("qPreset");
  const qBitrate = byId("qBitrate");
  const qBitrateVal = byId("qBitrateVal");
  const qScale = byId("qScale");
  const qFps = byId("qFps");

  let seq = 0;
  let inflight = null;
  const q = [];

  // Quality debounce
  let qDebounce = null;
  let qDirtyUntil = 0;

  const PRESETS = {
    auto:   { preset:"auto" },
    low:    { preset:"low",    bitrate_kbps: 500,   scale: 2.0, fps: 15, target_w: 640,  target_h: 360 },
    medium: { preset:"medium", bitrate_kbps: 1200,  scale: 1.0, fps: 24, target_w: 960,  target_h: 540 },
    high:   { preset:"high",   bitrate_kbps: 2500,  scale: 1.0, fps: 30, target_w: 1280, target_h: 720 },
    // Ultra: prefer Full HD @30fps (most webcams are 30fps at 1080p)
    ultra:  { preset:"ultra",  bitrate_kbps: 8000,  scale: 1.0, fps: 30, target_w: 1920, target_h: 1080 },
    custom: { preset:"custom" },
  };

  function setBusy(isBusy) {
    for (const id of btnIds) {
      const b = byId(id);
      if (!b) continue;
      // Stop bleibt immer klickbar (notfalls mehrfach)
      if (id === "stop") continue;
      b.disabled = !!isBusy;
    }
  }

  function showAction(text) {
    if (!actionPill) return;
    if (!text) { actionPill.style.display = "none"; actionPill.textContent = ""; return; }
    actionPill.style.display = "inline-flex";
    actionPill.textContent = text;
  }

  async function postCmd(cmd) {
    seq += 1;
    const item = { cmd, seq };
    q.push(item);
    pump();
  }

  async function pump() {
    if (inflight || q.length === 0) return;
    inflight = q.shift();
    setBusy(true);
    showAction("pending: " + inflight.cmd);

    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), 4000);

    try {
      const r = await fetch("/api/cmd", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(inflight),
        signal: controller.signal
      });
      if (!r.ok) throw new Error("cmd " + r.status);
      // Nur accepted; applied kommt über /api/state
    } catch (e) {
      console.warn("cmd failed", e);
      showAction("error: " + inflight.cmd);
      setTimeout(() => showAction(""), 1200);
      inflight = null;
      setBusy(false);
      // nächstes Kommando trotzdem versuchen
      setTimeout(pump, 0);
    } finally {
      clearTimeout(t);
    }
  }

  function setSelectValue(sel, val) {
    if (!sel) return;
    const s = String(val);
    for (const o of sel.options) {
      if (o.value === s) { sel.value = s; return; }
    }
    sel.value = s;
  }

  function readQualityFromUI() {
    const preset = (qPreset && qPreset.value) ? qPreset.value : "high";
    const bitrate = qBitrate ? Number(qBitrate.value || 2500) : 2500;
    const scale = qScale ? Number(qScale.value || 1) : 1;
    const fps = qFps ? Number(qFps.value || 30) : 30;
    // For non-custom presets we also send fixed target resolution.
    let tw = null, th = null;
    const p = PRESETS[preset] || null;
    if (p && preset !== 'custom' && p.target_w && p.target_h) {
      tw = p.target_w; th = p.target_h;
    }
    return { preset, bitrate_kbps: bitrate, scale, fps, target_w: tw, target_h: th };
  }

  async function postQuality(payload) {
    qDirtyUntil = Date.now() + 1200;
    showAction("pending: quality");

    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), 2500);

    try {
      const r = await fetch("/api/quality", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(payload),
        signal: controller.signal
      });
      if (!r.ok) throw new Error("quality " + r.status);
      const js = await r.json().catch(() => null);
      if (js && js.ok) {
        applyQualityToUI(js);
        // WebRTC: apply encoding-related changes via reconnect for stability.
        if (js.reconnect) {
          showAction("reconnecting…");
          try {
            await reconnectWebRTC(false);
            showAction("ok: reconnected");
            setTimeout(() => showAction(""), 800);
          } catch(e2) {
            console.warn("reconnect failed", e2);
            showAction("error: reconnect");
            setTimeout(() => showAction(""), 1500);
          }
        } else {
          showAction("ok: quality");
          setTimeout(() => showAction(""), 700);
        }
      } else {
        throw new Error("quality bad response");
      }
    } catch (e) {
      console.warn("quality failed", e);
      showAction("error: quality");
      setTimeout(() => showAction(""), 1200);
    } finally {
      clearTimeout(t);
    }
  }

  function scheduleQualityPush(payload) {
    if (qDebounce) clearTimeout(qDebounce);
    qDebounce = setTimeout(() => postQuality(payload), 180);
  }

  function applyQualityToUI(qs) {
    if (!qs) return;
    // Don't fight user while they are dragging/sliding.
    if (Date.now() < qDirtyUntil) return;
    if (qPreset) setSelectValue(qPreset, qs.preset || "high");
    if (qBitrate) qBitrate.value = String(qs.bitrate_kbps || 2500);
    if (qBitrateVal) qBitrateVal.textContent = String(qs.bitrate_kbps || 2500);
    if (qScale) setSelectValue(qScale, qs.scale || 1);
    if (qFps) setSelectValue(qFps, qs.fps || 30);
  }

  function applyState(s) {
    if (!s) return;
    const preset = s.preset ?? "?";
    const poseOn = !!s.pose_enabled;
    const overlayOn = !!s.overlay_enabled;
    const inferOn = !!s.inference_enabled;
    const fps = (s.fps || 0);

    const mode = inferOn ? "infer" : "stream-only";
    const qtxt = s.quality ? ` | q=${s.quality.preset || "?"} ${s.quality.bitrate_kbps || "?"}kbps ${s.quality.scale || "?"}x @${s.quality.fps || "?"}fps` : "";
    if (statePill) statePill.textContent = `preset=${preset} | mode=${mode} | fps=${fps.toFixed(1)}${qtxt}`;

    // Upscaling warning: capture smaller than stream output
    try {
      const cw = Number(s.capture_w || 0), ch = Number(s.capture_h || 0);
      const tw = Number((s.quality && s.quality.target_w) || 0);
      const th = Number((s.quality && s.quality.target_h) || 0);
      const sc = Number((s.quality && s.quality.scale) || 1);
      let ow = tw, oh = th;
      if (tw > 0 && th > 0 && sc > 1.001) {
        ow = Math.max(16, Math.round(tw / sc));
        oh = Math.max(16, Math.round(th / sc));
      }
      if (upPill && cw > 0 && ch > 0 && ow > 0 && oh > 0 && (cw < ow || ch < oh)) {
        upPill.style.display = "inline-flex";
        upPill.textContent = "Capture smaller than target – upscaling";
        upPill.title = `capture=${cw}x${ch} output=${ow}x${oh} target=${tw}x${th} scale=${sc}x`;
      } else if (upPill) {
        upPill.style.display = "none";
        upPill.textContent = "";
        upPill.title = "";
      }
    } catch(e) {}


    const poseBtn = byId("pose");
    const overlayBtn = byId("overlay");
    const inferBtn = byId("infer");

    if (poseBtn) poseBtn.textContent = "Pose: " + (poseOn ? "ON" : "OFF");
    if (overlayBtn) overlayBtn.textContent = "Overlay: " + (overlayOn ? "ON" : "OFF");
    if (inferBtn) inferBtn.textContent = "Model: " + (inferOn ? "ON" : "OFF");

    // Wenn Model aus ist, Overlay/Pose nicht sinnvoll -> deaktivieren.
    if (overlayBtn) overlayBtn.disabled = !inferOn || overlayBtn.disabled;
    if (poseBtn) poseBtn.disabled = !inferOn || poseBtn.disabled;

    const applied = Number(s.cmd_seq_applied || 0);

    if (inflight && applied >= inflight.seq) {
      showAction("ok: " + inflight.cmd);
      setTimeout(() => showAction(""), 600);
      inflight = null;
      setBusy(false);
      setTimeout(pump, 0);
    }

    if (s.quality) applyQualityToUI(s.quality);
  }

  async function pollState() {
    try {
      const r = await fetch("/api/state", { cache: "no-store" });
      if (!r.ok) throw new Error("state " + r.status);
      const s = await r.json();
      applyState(s);
    } catch(e) {
      // ignore
    } finally {
      setTimeout(pollState, 350);
    }
  }

  // Button handlers
  for (const id of btnIds) {
    const b = byId(id);
    if (!b) continue;
    b.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      const cmd = b.getAttribute("data-cmd");
      if (!cmd) return;
      // stop immer sofort senden, auch wenn busy
      if (cmd === "stop") {
        seq += 1;
        fetch("/api/cmd", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({cmd:"stop", seq})}).catch(()=>{});
        showAction("stopping…");
        if (window.__sentinelOnStop) try { window.__sentinelOnStop(); } catch(e) {}
        return;
      }
      postCmd(cmd);
    }, {passive:false});
  }

  // Quality handlers
  function onQChange() {
    const cur = readQualityFromUI();
    if (qBitrateVal) qBitrateVal.textContent = String(cur.bitrate_kbps);
    scheduleQualityPush(cur);
  }

  if (qPreset) qPreset.addEventListener("change", () => {
    const sel = qPreset.value || "high";
    const p = PRESETS[sel] || PRESETS.high;
    if (p.bitrate_kbps && qBitrate) qBitrate.value = String(p.bitrate_kbps);
    if (p.scale && qScale) setSelectValue(qScale, p.scale);
    if (p.fps && qFps) setSelectValue(qFps, p.fps);
    if (qBitrateVal && qBitrate) qBitrateVal.textContent = String(qBitrate.value);
    scheduleQualityPush(readQualityFromUI());
  });

  // Bitrate slider UX:
  //  - while dragging (input), only update the number
  //  - on release/commit (change), push to server
  if (qBitrate) qBitrate.addEventListener("input", () => {
    if (qPreset) qPreset.value = "custom";
    if (qBitrateVal) qBitrateVal.textContent = String(qBitrate.value);
  });
  if (qBitrate) qBitrate.addEventListener("change", () => {
    if (qPreset) qPreset.value = "custom";
    onQChange();
  });
  if (qScale) qScale.addEventListener("change", () => {
    if (qPreset) qPreset.value = "custom";
    onQChange();
  });
  if (qFps) qFps.addEventListener("change", () => {
    if (qPreset) qPreset.value = "custom";
    onQChange();
  });

  // Hotkeys
  window.addEventListener("keydown", (ev) => {
    const k = (ev.key || "").toLowerCase();
    if (k === "m") postCmd("next");
    else if (k === "n") postCmd("prev");
    else if (k === "p") postCmd("toggle_pose");
    else if (k === "o") postCmd("toggle_overlay");
    else if (k === "i") postCmd("toggle_inference");
    else if (k === "q") {
      seq += 1;
      fetch("/api/cmd", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({cmd:"stop", seq})}).catch(()=>{});
      showAction("stopping…");
      if (window.__sentinelOnStop) try { window.__sentinelOnStop(); } catch(e) {}
    }
  });

  // Local resolution indicator to verify that quality changes are applied.
  setInterval(() => {
    try {
      if (!videoPill) return;
      const v = byId('v');
      if (v && v.videoWidth && v.videoHeight) {
        videoPill.textContent = `video=${v.videoWidth}x${v.videoHeight}`;
        return;
      }
      const im = document.querySelector('img');
      if (im && im.naturalWidth && im.naturalHeight) {
        videoPill.textContent = `img=${im.naturalWidth}x${im.naturalHeight}`;
        return;
      }
      videoPill.textContent = 'video: …';
    } catch(e) {}
  }, 600);

  pollState();
})();
</script>
"""


def _page_html(title: str, stream_label: str, codec_label: str, media_html: str, hint: str = "") -> bytes:
    """Build a complete HTML page for either MJPEG or WebRTC mode.

    Assembles the header/chrome, the media element placeholder, and the
    shared control-bar HTML.  Returns UTF-8 encoded bytes ready to serve.
    """
    html = f"""<!doctype html>
<html lang="de">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>{title}</title>
    <style>
      body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 16px; }}
      .wrap {{ max-width: 1100px; margin: 0 auto; }}
      img, video {{ width: 100%; height: auto; border-radius: 12px; background: #111; }}
      .row {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-bottom: 10px; }}
      .pill {{ padding:6px 10px; border:1px solid #ddd; border-radius:999px; font-size: 12px; }}
      .controls {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin: 10px 0 12px; }}
      button {{ padding:8px 12px; border-radius:10px; border:1px solid #ddd; background:#fff; cursor:pointer; }}
      select {{ padding:6px 10px; border-radius:10px; border:1px solid #ddd; background:#fff; }}
      input[type="range"] {{ width: 140px; }}
      button:hover {{ background:#f6f6f6; }}
      button:disabled {{ opacity:.55; cursor:not-allowed; }}
      .danger {{ border-color:#ffb0b0; }}
      .danger:hover {{ background:#fff5f5; }}
      code {{ background:#f6f6f6; padding:2px 6px; border-radius:6px; }}
      .muted {{ opacity:.75; font-size: 13px; }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="row">
        <div class="pill">sentinelCam</div>
        <div class="pill">Stream: {stream_label}</div>
        <div class="pill">Codec: <code>{codec_label}</code></div>
      </div>
      <h2 style="margin:10px 0 10px">{title}</h2>
      {media_html}
      {_controls_html()}
      {hint}
    </div>
  </body>
</html>"""
    return html.encode("utf-8")


class _MjpegHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the MJPEG fallback server.

    Routes:
      GET /              – index page with an <img src="/mjpeg"> tag
      GET /mjpeg          – multipart MJPEG stream (long-lived connection)
      GET /latest.jpg     – single snapshot (honours scale & jpeg_quality)
      GET /health         – simple JSON liveness probe
      GET /api/state      – current worker state (preset, pose, fps, …)
      GET /api/quality    – current quality settings snapshot
      POST /api/quality   – update quality settings
      POST /api/cmd       – send a control command (JSON body)
      POST /api/cmd/{cmd} – send a control command (URL path)
    """
    # Set by the handler_factory closure in run_mjpeg_server()
    hub: FrameHub
    title: str
    control: Optional[ControlAPI]
    quality: StreamQuality

    server_version = "sentinelCam/0.2"

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        """Serialize *payload* as JSON and write a complete HTTP response."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        """Dispatch incoming GET requests to the appropriate handler."""
        # --- Index page ---
        if self.path in ("/", "/index.html"):
            body = _page_html(
                title=self.title,
                stream_label="MJPEG",
                codec_label="jpeg",
                media_html='<img src="/mjpeg" alt="sentinelCam stream" />',
                hint='<p class="muted">Tipp: Für weniger Latenz starte den Worker mit <code>--stream webrtc</code> (optional).</p>',
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/api/state"):
            if self.control is None:
                self._send_json({"ok": False, "error": "control_disabled"}, status=503)
                return
            s = self.control.get_state()
            s["ok"] = True
            self._send_json(s)
            return

        if self.path.startswith("/api/quality"):
            try:
                self._send_json({"ok": True, **(self.quality.snapshot() if self.quality else {})})
            except Exception:
                self._send_json({"ok": False, "error": "quality_unavailable"}, status=503)
            return

        if self.path.startswith("/latest.jpg"):
            pkt = self.hub.latest()
            if pkt is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "no frame yet")
                return
            self.send_response(HTTPStatus.OK)
            q = self.quality.snapshot() if self.quality else {"jpeg_quality": 80, "scale": 1.0}
            frame = pkt.bgr
            scale = float(q.get("scale") or 1.0)
            try:
                if scale > 1.001:
                    frame = cv2.resize(frame, None, fx=1.0/scale, fy=1.0/scale, interpolation=cv2.INTER_AREA)
            except Exception:
                frame = pkt.bgr
            ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(q.get("jpeg_quality") or 80)])
            if not ok:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "encode failed")
                return
            jpg = enc.tobytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.end_headers()
            self.wfile.write(jpg)
            return

        if self.path.startswith("/health"):
            pkt = self.hub.latest()
            payload = {"ok": True, "has_frame": bool(pkt), "ts": pkt.ts if pkt else None}
            self._send_json(payload)
            return

        if self.path.startswith("/mjpeg"):
            self.send_response(HTTPStatus.OK)
            boundary = "frame"
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.end_headers()

            last_ts = 0.0
            try:
                while True:
                    pkt = self.hub.wait(after_ts=last_ts, timeout=5.0)
                    if pkt is None:
                        continue
                    last_ts = pkt.ts
                    q = self.quality.snapshot() if self.quality else {"jpeg_quality": 80, "scale": 1.0}
                    frame = pkt.bgr
                    scale = float(q.get("scale") or 1.0)
                    try:
                        if scale > 1.001:
                            frame = cv2.resize(frame, None, fx=1.0/scale, fy=1.0/scale, interpolation=cv2.INTER_AREA)
                    except Exception:
                        frame = pkt.bgr
                    ok, enc = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), int(q.get("jpeg_quality") or 80)],
                    )
                    if not ok:
                        continue
                    jpg = enc.tobytes()

                    self.wfile.write(f"--{boundary}\r\n".encode("ascii"))
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii"))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return

        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):  # noqa: N802
        """Dispatch incoming POST requests (quality updates, control commands)."""
        # --- Quality update ---
        if self.path == "/api/quality":
            try:
                n = int(self.headers.get('Content-Length', '0') or '0')
                raw = self.rfile.read(n) if n > 0 else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}') if raw else {}
            except Exception:
                data = {}
            try:
                qs = self.quality.update_from(data) if self.quality else {}
                self._send_json({"ok": True, **qs})
            except Exception:
                self._send_json({"ok": False, "error": "quality_update_failed"}, status=500)
            return

        if self.path == "/api/cmd":
            if self.control is None:
                self._send_json({"ok": False, "error": "control_disabled"}, status=503)
                return
            try:
                n = int(self.headers.get('Content-Length', '0') or '0')
                raw = self.rfile.read(n) if n > 0 else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}') if raw else {}
                cmd = str(data.get('cmd', '')).strip().lower()
                seq = int(data.get('seq', 0) or 0)
            except Exception:
                cmd = ''
                seq = 0
            if not cmd:
                self._send_json({"ok": False, "error": "missing_cmd"}, status=400)
                return
            self.control.command({"cmd": cmd, "seq": seq})
            self._send_json({"ok": True, "cmd": cmd, "seq": seq})
            return

        if self.path.startswith("/api/cmd/"):
            if self.control is None:
                self._send_json({"ok": False, "error": "control_disabled"}, status=503)
                return
            cmd = self.path.split("/api/cmd/", 1)[1].strip().lower()
            if not cmd:
                self._send_json({"ok": False, "error": "missing_cmd"}, status=400)
                return
            self.control.command(cmd)
            self._send_json({"ok": True, "cmd": cmd})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        # Suppress default stderr logging to keep stdout clean.
        return


def run_mjpeg_server(
    hub: FrameHub,
    host: str,
    port: int,
    title: str = "sentinelCam",
    control: Optional[ControlAPI] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Start a blocking MJPEG HTTP server (stdlib only, no extra deps).

    Creates a :class:`ThreadingHTTPServer` that serves the index page,
    MJPEG stream, snapshot endpoint, and the control/quality REST API.
    Runs until *stop_event* is set (if provided), then shuts down.
    """

    quality = StreamQuality()

    # Dynamic handler factory: each request gets a fresh handler class
    # whose class-level attributes point to the shared hub/control/quality.
    def handler_factory(*_a, **_k):
        cls = type("MjpegHandler", (_MjpegHandler,), {})
        cls.hub = hub
        cls.title = title
        cls.control = control
        cls.quality = quality
        return cls(*_a, **_k)

    httpd = ThreadingHTTPServer((host, int(port)), handler_factory)
    httpd.timeout = 0.5
    try:
        while stop_event is None or (not stop_event.is_set()):
            httpd.handle_request()
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass


# -----------------------------
# Optional WebRTC server
# -----------------------------

def _require_webrtc_deps():
    """Verify that optional WebRTC dependencies (aiohttp, aiortc, av) are installed.

    Raises RuntimeError with installation instructions if any are missing.
    """
    try:
        import aiohttp  # noqa: F401
        import aiortc  # noqa: F401
        import av  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "WebRTC stream benötigt Zusatz-Pakete. Installiere z.B.:\n"
            "  python -m pip install aiohttp aiortc av\n"
            "Hinweis: AV kann auf manchen Plattformen zusätzliche Systemlibs brauchen (FFmpeg)."
        ) from e


def _install_udp_port_range_patch(loop: asyncio.AbstractEventLoop, min_port: int, max_port: int) -> None:
    """Best-effort: force aioice / ICE sockets to bind within a UDP port range.

    aiortc / aioice normally asks the OS for an ephemeral port (port=0).
    On locked-down networks / firewalls it's often easier to open a known range.

    This patch wraps the event-loop's create_datagram_endpoint() and replaces
    local_addr=(ip, 0) with local_addr=(ip, random_port_in_range).

    NOTE: This is a best-effort workaround: if it cannot bind in the range,
    it will fall back to the OS-selected port.
    """
    if not min_port or not max_port:
        return
    if min_port < 1024 or max_port <= min_port:
        return
    if getattr(loop, "_sentinelcam_udp_port_range", None):
        return

    orig = loop.create_datagram_endpoint

    async def patched_create_datagram_endpoint(protocol_factory, *args, **kwargs):
        local_addr = kwargs.get("local_addr", None)
        # create_datagram_endpoint can pass local_addr positionally too; handle common case.
        if local_addr is None and len(args) >= 1:
            # signature: (protocol_factory, local_addr=None, remote_addr=None, ...)
            local_addr = args[0]

        if (
            isinstance(local_addr, tuple)
            and len(local_addr) >= 2
            and isinstance(local_addr[1], int)
            and local_addr[1] == 0
        ):
            host = local_addr[0]
            # Try a number of random ports.
            for _ in range(200):
                port = random.randint(min_port, max_port)
                try:
                    kwargs2 = dict(kwargs)
                    kwargs2["local_addr"] = (host, port)
                    return await orig(protocol_factory, *args, **kwargs2)
                except OSError as e:
                    err = getattr(e, "errno", None)
                    if err in (98, 10048):  # EADDRINUSE (posix / win)
                        continue
                    # Sometimes Windows reports address already in use without errno mapping.
                    if "in use" in str(e).lower():
                        continue
                    # If it's a different failure, let it bubble up.
                    raise
            # Fallback to OS-selected port
        return await orig(protocol_factory, *args, **kwargs)

    loop.create_datagram_endpoint = patched_create_datagram_endpoint  # type: ignore[assignment]
    setattr(loop, "_sentinelcam_udp_port_range", (min_port, max_port))


def _install_aioice_advertise_ip_patch(advertise_ip: str) -> None:
    """Best-effort: restrict aioice host candidates to a single local IP.

    Useful when the machine has multiple adapters (VPN / VirtualBox / WiFi+LAN),
    and the wrong interface ends up in ICE candidates.
    """
    if not advertise_ip:
        return
    try:
        import aioice.ice as ice  # type: ignore
    except Exception:
        return

    if getattr(ice, "_sentinelcam_advertise_ip", None) == advertise_ip:
        return

    if hasattr(ice, "get_host_addresses"):
        orig = ice.get_host_addresses

        def patched_get_host_addresses(use_ipv4: bool = True, use_ipv6: bool = True):  # type: ignore[override]
            return [advertise_ip]

        ice.get_host_addresses = patched_get_host_addresses  # type: ignore[assignment]
        ice._sentinelcam_advertise_ip = advertise_ip  # type: ignore[attr-defined]
        ice._sentinelcam_advertise_ip_orig = orig  # type: ignore[attr-defined]

async def _await_ice_complete(pc, timeout: float = 5.0) -> None:
    """Wait until ICE gathering is complete (non-trickle SDP exchange)."""
    if getattr(pc, "iceGatheringState", None) == "complete":
        return
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    @pc.on("icegatheringstatechange")
    def _on_state_change():
        try:
            if pc.iceGatheringState == "complete" and not fut.done():
                fut.set_result(True)
        except Exception:
            pass

    try:
        await asyncio.wait_for(fut, timeout=timeout)
    except Exception:
        # best-effort: continue, browser may still succeed with partial candidates
        return


async def run_webrtc_server(
    hub: FrameHub,
    host: str,
    port: int,
    codec: str = "auto",
    title: str = "sentinelCam",
    control: Optional[ControlAPI] = None,
    stop_event: Optional[threading.Event] = None,
    advertise_ip: str = "",
    rtc_min_port: int = 0,
    rtc_max_port: int = 0,
) -> None:
    """Start a blocking WebRTC video server (aiohttp + aiortc).

    Serves an HTML page with WebRTC negotiation JS, an /offer endpoint for
    SDP exchange, and falls back to MJPEG if the browser cannot establish
    a WebRTC connection.  Also exposes the same /api/state, /api/quality,
    and /api/cmd REST endpoints as the MJPEG server.

    Args:
        hub:           Shared frame buffer fed by the camera producer thread.
        host/port:     Bind address for the aiohttp HTTP server.
        codec:         Preferred video codec (auto/h264/vp8/vp9/av1).
        title:         Page title shown in the browser.
        control:       Optional ControlAPI for camera commands.
        stop_event:    Threading event that signals graceful shutdown.
        advertise_ip:  Restrict ICE host candidates to this LAN IP.
        rtc_min_port:  Lower bound of the UDP port range for ICE/RTP.
        rtc_max_port:  Upper bound of the UDP port range for ICE/RTP.
    """

    _require_webrtc_deps()

    # Best-effort LAN helpers:
    # - constrain UDP ports for ICE/RTP (open the same range in Windows firewall)
    # - restrict host candidates to a single advertised LAN IP (avoid VPN / wrong NIC)
    try:
        loop = asyncio.get_running_loop()
        _install_udp_port_range_patch(loop, rtc_min_port, rtc_max_port)
    except Exception:
        pass
    try:
        _install_aioice_advertise_ip_patch(advertise_ip)
    except Exception:
        pass


    # Reduce noisy logs on some platforms
    logging.getLogger("aioice").setLevel(logging.ERROR)
    logging.getLogger("aiortc").setLevel(logging.ERROR)

    from aiohttp import web
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
    from aiortc.rtcrtpsender import RTCRtpSender
    from aiortc import MediaStreamTrack
    import av

    # -- Patch aiortc encoder bitrate limits ----------------------------------
    # aiortc hardcodes very conservative DEFAULT/MIN/MAX_BITRATE constants in
    # its VP8 and H.264 encoder modules.  The library does NOT honour
    # RTCRtpSender.setParameters() or SDP b=AS/b=TIAS for outbound bitrate
    # control — the *only* mechanism that actually changes the encoder bitrate
    # at runtime is REMB feedback from the browser.  So we raise the ceiling
    # here to match our UI slider range, and we update DEFAULT_BITRATE before
    # every PeerConnection so the encoder starts at the requested bitrate.
    import aiortc.codecs.vpx  as _vpx_mod
    import aiortc.codecs.h264 as _h264_mod
    _vpx_mod.MIN_BITRATE  = 200_000      # 200 kbps floor
    _vpx_mod.MAX_BITRATE  = 15_000_000   # 15 Mbps  ceiling
    _h264_mod.MIN_BITRATE = 200_000
    _h264_mod.MAX_BITRATE = 15_000_000

    # IMPORTANT:
    # The frame producer runs in a different thread. If we block the asyncio loop
    # inside MediaStreamTrack.recv(), WebRTC/ICE will stall and you'll get black
    # video + aioice timeouts. Bind the hub to this loop so waiting is async.
    loop = asyncio.get_running_loop()
    hub.bind_asyncio(loop)

    # Suppress a noisy aioice edge-case on some Windows setups where a retry
    # timer races with completion and triggers InvalidStateError.
    # This does not affect streaming; it only avoids scary traceback spam.
    prev_exc_handler = loop.get_exception_handler()

    def _exc_handler(_loop, context):
        exc = context.get("exception")
        try:
            from asyncio import InvalidStateError

            if isinstance(exc, InvalidStateError):
                # aioice has a known edge-case/race on Windows where callbacks
                # try to set_result/set_exception on a Future that is already
                # done, producing scary but harmless InvalidStateError traces.
                h = context.get("handle")
                htxt = str(h) if h is not None else ""
                msg = str(context.get("message") or "")
                src = str(context.get("source_traceback") or "")
                blob = (htxt + " " + msg + " " + src).lower()
                if (
                    "aioice" in blob
                    or "stun.py" in blob
                    or "mdns.py" in blob
                    or "transaction.__retry" in blob
                    or "_call_connection_lost" in blob
                ):
                    return
        except Exception:
            pass
        if prev_exc_handler:
            prev_exc_handler(_loop, context)
        else:
            _loop.default_exception_handler(context)

    loop.set_exception_handler(_exc_handler)

    pcs = set()           # active RTCPeerConnections (for cleanup on shutdown)
    offer_lock = asyncio.Lock()  # serialise SDP offer handling to avoid races

    def _munge_sdp_bandwidth(sdp: str, kbps: int) -> str:
        """Insert/replace bandwidth limits in the video m-section.

        This is a best-effort hint for the browser side.  The actual encoder
        bitrate is controlled by patching aiortc's DEFAULT_BITRATE before
        each connection.
        """
        try:
            kbps = int(kbps)
        except Exception:
            kbps = 2500
        kbps = max(500, min(10000, kbps))

        lines = sdp.splitlines()
        out = []
        in_video = False
        inserted = False
        pending_insert_at = None

        for i, line in enumerate(lines):
            if line.startswith("m="):
                # entering a new media section
                in_video = line.startswith("m=video")
                inserted = inserted if not in_video else False
                pending_insert_at = None
                out.append(line)
                continue

            if in_video:
                # Strip existing bandwidth lines in video section
                if line.startswith("b=AS:") or line.startswith("b=TIAS:"):
                    continue

                # Prefer to insert after the connection line if present
                if pending_insert_at is None and line.startswith("c="):
                    out.append(line)
                    out.append(f"b=AS:{kbps}")
                    out.append(f"b=TIAS:{kbps * 1000}")
                    inserted = True
                    pending_insert_at = -1
                    continue

            out.append(line)

        # If we never inserted (no c= in video section), insert right after m=video
        if not any(l.startswith("b=AS:") for l in out):
            out2 = []
            in_video = False
            for line in out:
                out2.append(line)
                if line.startswith("m=video"):
                    in_video = True
                    continue
                if in_video and line.startswith("i="):
                    # keep going until c=, but if i= exists we can still insert after i=
                    continue
                if in_video and line.startswith("c="):
                    # already handled above
                    in_video = False
            # If still missing, do a simple insertion after first m=video
            if not any(l.startswith("b=AS:") for l in out2):
                out2 = []
                inserted2 = False
                for line in out:
                    out2.append(line)
                    if (not inserted2) and line.startswith("m=video"):
                        out2.append(f"b=AS:{kbps}")
                        out2.append(f"b=TIAS:{kbps * 1000}")
                        inserted2 = True
                out = out2
            else:
                out = out2

        return "\r\n".join(out) + "\r\n"
    closing = False       # set True during shutdown; rejects new offers
    quality = StreamQuality()  # shared quality state; updated by /api/quality

    # NOTE ABOUT BITRATE CONTROL:
    #
    # aiortc's RTCRtpSender does NOT implement setParameters()/getParameters().
    # The only runtime bitrate control is REMB feedback from the browser, which
    # sets encoder.target_bitrate internally.  We therefore control the
    # *initial* encoder bitrate by patching the module-level DEFAULT_BITRATE
    # constant in aiortc.codecs.vpx / .h264 before each PeerConnection is
    # created (see the offer() handler).  Quality changes that affect bitrate
    # trigger a reconnect so a fresh encoder picks up the new default.
    #
    # FPS and resolution are controlled safely by:
    #   - downscaling frames in HubVideoTrack.recv()
    #   - adjusting the send cadence (FPS)


    def _webrtc_index_html() -> bytes:
        media_html = """
<video id="v" autoplay playsinline muted></video>
<img id="fallback" data-src="/mjpeg" style="display:none" alt="mjpeg fallback" />
<p class="muted" id="status">Status: initial… (falls WebRTC nicht klappt, wird auf MJPEG umgeschaltet)</p>
"""
        hint = '<p class="muted">Hotkeys: <code>M</code>/<code>N</code> wechseln Preset · <code>P</code> Pose · <code>Q</code> Stop</p>'
        body = _page_html(
            title=title,
            stream_label="WebRTC",
            codec_label=((codec or "auto").lower().strip() if (codec or "") else "auto") if ((codec or "auto").lower().strip() not in ("auto", "") ) else "h264",
            media_html=media_html,
            hint=hint,
        )
        # Inject WebRTC + control JS (keep simple, no bundler)
        js = """
<script>
  function byId(id) { return document.getElementById(id); }
  function setStatus(t) { const el = byId('status'); if(el) el.textContent = 'Status: ' + t; }

  let __pc = null;
  let __lastBytesIn = null;
  let __lastBytesTs = null;

  let __lastFrameAt = 0;
  let __stallTimer = null;
  let __bitrateTimer = null;
  let __stopping = false;

  window.__sentinelOnStop = async () => {
    __stopping = true;
    try { await closePc(); } catch(e) {}
    setStatus('stopping…');
  };

  async function closePc() {
    try {
      if (__stallTimer) { clearInterval(__stallTimer); __stallTimer = null; }
      if (__pc) {
        try { __pc.ontrack = null; __pc.onconnectionstatechange = null; } catch(e){}
        await __pc.close();
      }
    } catch(e) {}
    __pc = null;
  }

  async function waitIce(pc) {
    if (pc.iceGatheringState === 'complete') return;
    await new Promise((resolve) => {
      function check() {
        if (pc.iceGatheringState === 'complete') {
          pc.removeEventListener('icegatheringstatechange', check);
          resolve();
        }
      }
      pc.addEventListener('icegatheringstatechange', check);
    });
  }

  async function startWebRTC() {
    if (__stopping) throw new Error('stopping');
    // For localhost/lan usage, host-candidates are enough and we avoid STUN.
    // (Also reduces noisy timeouts on some networks.)
    await closePc();
    const pc = new RTCPeerConnection({ iceServers: [] });
    __pc = pc;
    __lastFrameAt = Date.now();

    pc.addTransceiver('video', { direction: 'recvonly' });

    pc.onconnectionstatechange = () => {
      setStatus('connection=' + pc.connectionState);
      if (!__stopping && pc.connectionState === 'failed') {
        console.warn('WebRTC failed -> reconnect');
        reconnectWebRTC();
      }
    };

    pc.ontrack = (ev) => {
      const v = byId('v');
      v.srcObject = ev.streams[0];
      setStatus('receiving video…');

      // If a track is negotiated but no frames arrive, fall back.
      let gotFrame = false;
      function onFrame() {
        gotFrame = true;
        __lastFrameAt = Date.now();
        setStatus('video frames OK');
        if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(onFrame);
      }
      if (v.requestVideoFrameCallback) v.requestVideoFrameCallback(onFrame);
      setTimeout(() => {
        if (!gotFrame) {
          console.warn('No video frames -> fallback');
          // Erst reconnect versuchen, danach fallback.
          reconnectWebRTC(true);
        }
      }, 8000);
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIce(pc);

    const resp = await fetch('/offer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
    });
    if (resp.status === 410 || resp.status === 503) throw new Error('server stopping');
    if (!resp.ok) throw new Error('offer failed');
    const ans = await resp.json();
    await pc.setRemoteDescription(ans);

    // Stall monitor: wenn >2.0s keine Frames ankommen -> reconnect.
    if (__stallTimer) clearInterval(__stallTimer);
    __stallTimer = setInterval(() => {
      const age = Date.now() - (__lastFrameAt || 0);
      if (!__stopping && __pc && __pc.connectionState === 'connected' && age > 2000) {
        console.warn('Video stall (' + age + 'ms) -> reconnect');
        reconnectWebRTC();
      }
    }, 750);

    // Show the *server outbound* bitrate as seen by this client (inbound-rtp video).
    if (__bitrateTimer) clearInterval(__bitrateTimer);
    __bitrateTimer = setInterval(async () => {
      try {
        if (!bwPill) return;
        if (!__pc) { bwPill.textContent = 'server out: … kbps'; return; }
        const stats = await __pc.getStats();
        let bytes = null;
        stats.forEach((r) => {
          const kind = r.kind || r.mediaType;
          if (r.type === 'inbound-rtp' && kind === 'video' && !r.isRemote) {
            bytes = r.bytesReceived;
          }
        });
        if (bytes == null) { bwPill.textContent = 'server out: … kbps'; return; }
        const now = Date.now();
        if (__lastBytesIn != null && __lastBytesTs != null) {
          const dt = (now - __lastBytesTs) / 1000.0;
          const db = bytes - __lastBytesIn;
          if (dt > 0.2 && db >= 0) {
            const kbps = Math.round((db * 8) / 1000 / dt);
            bwPill.textContent = 'server out: ' + kbps + ' kbps';
          }
        }
        __lastBytesIn = bytes;
        __lastBytesTs = now;
      } catch (e) {
        // ignore (stats not ready yet)
      }
    }, 1000);

    return pc;
  }

  function fallbackToMJPEG() {
    try {
      const img = byId('fallback');
      if (img && !img.getAttribute('src')) {
        const ds = img.getAttribute('data-src') || '/mjpeg';
        img.setAttribute('src', ds);
      }
    } catch(e) {}
    byId('v').style.display = 'none';
    byId('fallback').style.display = 'block';
    setStatus('fallback=MJPEG');
  }

  let __reconnecting = false;
  async function reconnectWebRTC(allowFallback=false) {
    if (__stopping) return;
    if (__reconnecting) return;
    __reconnecting = true;
    try {
      setStatus('reconnecting…');
      await closePc();
      byId('fallback').style.display = 'none';
      byId('v').style.display = 'block';
      await startWebRTC();
    } catch(e) {
      console.warn('Reconnect failed', e);
      if (allowFallback) fallbackToMJPEG();
      else setStatus('reconnect failed');
    } finally {
      __reconnecting = false;
    }
  }


  // Start
  const rb = byId('reconnect');
  if (rb) rb.addEventListener('click', () => reconnectWebRTC(true));

  startWebRTC().catch((e) => {
    console.warn('WebRTC failed, falling back to MJPEG', e);
    fallbackToMJPEG();
  });
</script>
"""
        # Insert before </body>
        return body.replace(b"</body>", js.encode("utf-8") + b"\n</body>")

    class HubVideoTrack(MediaStreamTrack):
        """Custom aiortc video track that reads frames from the shared FrameHub.

        Quality settings (fps, scale, target resolution) are *frozen* at
        construction time to avoid mid-stream encoder instability on Windows.
        Quality changes therefore require a WebRTC reconnect.
        """
        kind = "video"

        def __init__(self, quality_ref: StreamQuality):
            super().__init__()
            self._quality = quality_ref
            self._last_ts = 0.0
            self._last_bgr = None
            self._last_vf = None
            # IMPORTANT (Windows / PyAV): many encoders do not like changing
            # resolution mid-stream. If we resize frames dynamically while the
            # encoder is running, some builds produce black frames or can even
            # hang.
            #
            # Therefore, WebRTC quality changes that affect encoding (bitrate,
            # fps, scale, preset) are applied via reconnect. We keep fps/scale
            # fixed for the lifetime of this track.
            self._scale = 1.0
            self._target_w = None
            self._target_h = None
            self._last_vf_src_ts = 0.0       # timestamp of last converted VideoFrame
            self._last_pkt_ts_for_fps = 0.0   # last packet timestamp used for source FPS estimation
            self._src_dt_ema = None            # exponential moving average of source frame interval (seconds)
            self._pts = 0                      # running presentation timestamp counter (90 kHz clock)
            try:
                q = self._quality.snapshot() if self._quality else {}
                self._fps = float(q.get("fps") or 30.0)
                self._scale = float(q.get("scale") or 1.0)
                tw = q.get("target_w")
                th = q.get("target_h")
                self._target_w = int(tw) if tw else None
                self._target_h = int(th) if th else None
            except Exception:
                self._fps = 30.0
                self._scale = 1.0
                self._target_w = None
                self._target_h = None
            self._fps = float(max(5.0, min(60.0, self._fps)))
            # NaN guard: if scale is NaN (comparison with itself fails), reset to 1.0
            if not (self._scale == self._scale):
                self._scale = 1.0
            self._scale = float(max(1.0, min(4.0, self._scale)))
            self._pts_step = int(90000 / self._fps)  # PTS increment per frame (90 kHz RTP clock)
            self._next_due = 0.0  # loop.time() when next frame should be emitted

        async def recv(self):
            """Return a frame at a steady cadence.

            Important for robustness:
              - During preset/model toggles, inference/model loading can stall the
                producer thread for a short time.
              - If we block too long here, WebRTC can appear "frozen" and some
                browsers will need a reload.

            Strategy:
              - Always send at ~30fps by re-sending the last frame if needed.
              - If a newer frame exists, switch to it immediately (no buffering).
            """

            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._next_due == 0.0:
                self._next_due = now
            # Pace ourselves to ~30fps.
            sleep_for = self._next_due - now
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self._next_due = max(self._next_due + (1.0 / self._fps), loop.time())

            # Non-blocking: grab the latest frame if available.
            pkt = hub.latest()
            if pkt is not None and pkt.ts >= self._last_ts:
                # If producer stalled, pkt may be identical; that's fine.
                self._last_ts = pkt.ts
                self._last_bgr = pkt.bgr

                # Estimate source FPS (EMA) so we don't oversend duplicates.
                if self._last_pkt_ts_for_fps > 0:
                    dt = float(pkt.ts - self._last_pkt_ts_for_fps)
                    if 0.001 < dt < 1.0:
                        if self._src_dt_ema is None:
                            self._src_dt_ema = dt
                        else:
                            self._src_dt_ema = 0.9 * self._src_dt_ema + 0.1 * dt
                self._last_pkt_ts_for_fps = float(pkt.ts)

            frame = self._last_bgr
            if frame is None:
                # No frame yet: return a small black frame to keep negotiation alive.
                import numpy as _np

                frame = _np.zeros((480, 640, 3), dtype=_np.uint8)
                self._last_bgr = frame

            # Cap effective output FPS to source FPS (prevents CPU death-spiral on Windows).
            src_fps = None
            if self._src_dt_ema and self._src_dt_ema > 0:
                src_fps = max(5.0, min(60.0, 1.0 / float(self._src_dt_ema)))
            eff_fps = float(self._fps)
            if src_fps is not None:
                eff_fps = float(min(eff_fps, src_fps))
            # Update pacing only when it changed meaningfully.
            if abs(eff_fps - float(self._fps)) > 0.25:
                self._fps = float(max(5.0, min(60.0, eff_fps)))
                self._pts_step = int(90000 / self._fps)
                self._next_due = loop.time()

            # Convert only when we have a new source frame or the scale changed.
            src_ts = float(self._last_ts or 0.0)
            need_new = (self._last_vf is None) or (src_ts > self._last_vf_src_ts + 1e-6)
            if need_new:
                # Determine output size for this connection.
                #
                # Semantics:
                # - If a fixed target_w/target_h is set (from quality preset), that is the *base* output.
                # - scale reduces the output resolution by dividing the base resolution.
                #   This keeps scale meaningful even when a preset sets a target resolution.
                try:
                    h0, w0 = frame.shape[:2]
                except Exception:
                    h0, w0 = 480, 640

                base_w = int(self._target_w) if self._target_w else int(w0)
                base_h = int(self._target_h) if self._target_h else int(h0)
                sc = float(self._scale) if self._scale and self._scale > 1.001 else 1.0
                out_w = int(round(base_w / sc))
                out_h = int(round(base_h / sc))
                out_w = max(16, out_w)
                out_h = max(16, out_h)

                if out_w != w0 or out_h != h0:
                    try:
                        interp = cv2.INTER_AREA if (out_w < w0 or out_h < h0) else cv2.INTER_LINEAR
                        frame = cv2.resize(frame, (out_w, out_h), interpolation=interp)
                    except Exception:
                        pass
                # Offload conversion to avoid starving aiohttp on Windows.
                try:
                    vf = await asyncio.to_thread(av.VideoFrame.from_ndarray, frame, format="bgr24")
                except Exception:
                    vf = av.VideoFrame.from_ndarray(frame, format="bgr24")
                self._last_vf = vf
                self._last_vf_src_ts = src_ts
            else:
                vf = self._last_vf

            from fractions import Fraction

            self._pts += self._pts_step
            # It's safe (and much faster) to reuse the same VideoFrame object
            # when the underlying image didn't change; we only update timing.
            vf.pts = self._pts
            vf.time_base = Fraction(1, 90000)
            return vf

    async def index(_request):
        """Serve the WebRTC index page with embedded negotiation JS."""
        return web.Response(body=_webrtc_index_html(), content_type="text/html")

    async def mjpeg(_request):
        """MJPEG fallback stream, also available in WebRTC mode.

        Returns a long-lived multipart/x-mixed-replace response that
        pushes JPEG frames as they arrive from the hub.
        """
        # Reuse MJPEG endpoint even in WebRTC mode (fallback)
        boundary = "frame"
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": f"multipart/x-mixed-replace; boundary={boundary}",
                "Cache-Control": "no-cache, private",
                "Pragma": "no-cache",
            },
        )
        await resp.prepare(_request)
        last_ts = 0.0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                pkt = await hub.wait_async(after_ts=last_ts, timeout=5.0)
                if pkt is None:
                    await asyncio.sleep(0.01)
                    continue
                last_ts = pkt.ts
                jpg = pkt.jpg
                await resp.write(f"--{boundary}\r\n".encode("ascii"))
                await resp.write(b"Content-Type: image/jpeg\r\n")
                await resp.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii"))
                await resp.write(jpg)
                await resp.write(b"\r\n")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        return resp

    def _set_codec_preference(pc: RTCPeerConnection):
        # Default codec preference:
        # - Prefer H.264 when "auto" is requested (user expectation, and often
        #   hardware-accelerated on many machines).
        # - If H.264 isn't available in the current PyAV/FFmpeg build, fall back
        #   to VP8.
        pref = (codec or "auto").lower().strip()
        if pref in ("auto", ""):
            pref = "h264"
        caps = RTCRtpSender.getCapabilities("video")
        want_map = {
            "h264": "video/H264",
            "vp8": "video/VP8",
            "vp9": "video/VP9",
            "av1": "video/AV1",
        }
        want = want_map.get(pref)
        if not want:
            return
        codecs = [c for c in caps.codecs if getattr(c, "mimeType", "") == want]
        if not codecs and pref == "h264":
            # Fallback
            want = want_map.get("vp8")
            codecs = [c for c in caps.codecs if getattr(c, "mimeType", "") == want]
        if not codecs:
            return
        for t in pc.getTransceivers():
            if t.kind == "video":
                try:
                    t.setCodecPreferences(codecs)
                except Exception:
                    pass

    async def api_state(_request):
        """Return the current worker state + capture dimensions + quality snapshot."""
        if control is None:
            return web.json_response({"ok": False, "error": "control_disabled"}, status=503)
        s = control.get_state()
        # capture dims from latest frame
        cw = ch = 0
        try:
            pkt = hub.latest()
            if pkt is not None and getattr(pkt, "bgr", None) is not None:
                ch, cw = pkt.bgr.shape[:2]
        except Exception:
            pass
        s["capture_w"] = int(cw)
        s["capture_h"] = int(ch)
        s["quality"] = quality.snapshot()
        s["ok"] = True
        return web.json_response(s)

    async def api_quality_get(_request):
        """Return the current quality settings as JSON."""
        return web.json_response({"ok": True, **quality.snapshot()})

    async def api_quality_post(request):
        """Apply quality changes and signal whether a WebRTC reconnect is needed."""
        try:
            data = await request.json()
        except Exception:
            data = {}

        # We apply WebRTC stream-quality changes (fps/scale/bitrate/preset)
        # via reconnect for maximum stability on Windows.
        before = {}
        try:
            before = quality.snapshot()
        except Exception:
            before = {}

        try:
            qs = quality.update_from(data)
        except Exception as e:
            return web.json_response({"ok": False, "error": "quality_update_failed", "detail": str(e)[:160]}, status=500)

        # Apply MJPEG-related controls immediately.
        try:
            if "jpeg_quality" in qs and hasattr(hub, "set_jpeg_quality"):
                hub.set_jpeg_quality(int(qs.get("jpeg_quality") or 80))
        except Exception:
            pass

        reconnect = False
        try:
            for k in ("preset", "bitrate_kbps", "scale", "fps", "target_w", "target_h"):
                if k in qs and before.get(k) != qs.get(k):
                    reconnect = True
                    break
        except Exception:
            reconnect = True

        return web.json_response({"ok": True, "reconnect": reconnect, **qs})

    async def api_cmd_json(request):
        """Handle a control command sent as a JSON body ({cmd, seq})."""
        if control is None:
            return web.json_response({"ok": False, "error": "control_disabled"}, status=503)
        try:
            data = await request.json()
        except Exception:
            data = {}
        cmd = str(data.get("cmd", "")).strip().lower()
        seq = int(data.get("seq", 0) or 0)
        if not cmd:
            return web.json_response({"ok": False, "error": "missing_cmd"}, status=400)
        control.command({"cmd": cmd, "seq": seq})
        return web.json_response({"ok": True, "cmd": cmd, "seq": seq})

    async def api_cmd(request):
        """Handle a control command with the command name in the URL path."""
        if control is None:
            return web.json_response({"ok": False, "error": "control_disabled"}, status=503)
        cmd = request.match_info.get("cmd", "").strip().lower()
        if not cmd:
            return web.json_response({"ok": False, "error": "missing_cmd"}, status=400)
        control.command({"cmd": cmd, "seq": 0})
        return web.json_response({"ok": True, "cmd": cmd, "seq": 0})


    async def offer(request):
        """WebRTC SDP offer/answer exchange.

        The browser sends its SDP offer; we create a PeerConnection, attach
        a HubVideoTrack, set codec/bitrate preferences, and return our SDP
        answer.  Serialised via offer_lock to prevent concurrent encoder
        DEFAULT_BITRATE patches from racing.
        """
        nonlocal closing
        if stop_event is not None and stop_event.is_set():
            return web.json_response({"ok": False, "error": "stopping"}, status=410)
        if closing:
            return web.json_response({"ok": False, "error": "closing"}, status=410)

        try:
            params = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)

        async with offer_lock:
            if stop_event is not None and stop_event.is_set():
                return web.json_response({"ok": False, "error": "stopping"}, status=410)
            if closing:
                return web.json_response({"ok": False, "error": "closing"}, status=410)

            pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
            pcs.add(pc)

            @pc.on("connectionstatechange")
            async def on_connectionstatechange():
                if pc.connectionState in ("failed", "closed"):
                    try:
                        await pc.close()
                    except Exception:
                        pass
                    pcs.discard(pc)

            sender = pc.addTrack(HubVideoTrack(quality))
            _set_codec_preference(pc)

            # Set the encoder's initial bitrate to match the quality slider.
            # aiortc creates the encoder lazily on the first encoded frame;
            # the encoder reads DEFAULT_BITRATE from its module globals in
            # __init__.  By updating that global *now* (under the offer lock,
            # so there is no concurrent connection being set up), the new
            # encoder will start at the correct bitrate.
            try:
                _target_bps = int((quality.snapshot() or {}).get("bitrate_kbps") or 2500) * 1000
                _target_bps = max(200_000, min(15_000_000, _target_bps))
                _vpx_mod.DEFAULT_BITRATE  = _target_bps
                _h264_mod.DEFAULT_BITRATE = _target_bps
            except Exception:
                pass

            try:
                offer_sdp = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
                await pc.setRemoteDescription(offer_sdp)

                answer = await pc.createAnswer()

                # SDP bandwidth hints — best-effort secondary signal.
                # The *primary* bitrate control is the DEFAULT_BITRATE patch
                # above.  These b=AS/b=TIAS lines are kept as a hint for the
                # browser player side.
                try:
                    kbps = int((quality.snapshot() or {}).get("bitrate_kbps") or 2500)
                except Exception:
                    kbps = 2500
                munged = _munge_sdp_bandwidth(answer.sdp, kbps)
                answer = RTCSessionDescription(sdp=munged, type=answer.type)

                await pc.setLocalDescription(answer)

                await _await_ice_complete(pc, timeout=5.0)

                return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})
            except Exception as e:
                msg = str(e)
                try:
                    await pc.close()
                except Exception:
                    pass
                pcs.discard(pc)
                if "RTCPeerConnection is closed" in msg or "is closed" in msg:
                    return web.json_response({"ok": False, "error": "pc_closed"}, status=410)
                return web.json_response({"ok": False, "error": "offer_failed", "detail": msg[:200]}, status=500)

    async def on_shutdown(app):
        """Close all active PeerConnections during graceful server shutdown."""
        nonlocal closing
        closing = True
        async with offer_lock:
            coros = [pc.close() for pc in pcs]
            if coros:
                await asyncio.gather(*coros, return_exceptions=True)
            pcs.clear()

    # --- Register all HTTP routes ---
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/mjpeg", mjpeg)
    app.router.add_post("/offer", offer)          # WebRTC SDP negotiation
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/quality", api_quality_get)
    app.router.add_post("/api/quality", api_quality_post)
    app.router.add_post("/api/cmd", api_cmd_json)
    app.router.add_post("/api/cmd/{cmd}", api_cmd)
    app.on_shutdown.append(on_shutdown)

    # --- Start the HTTP server and block until shutdown ---
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=int(port))
    await site.start()

    # Poll stop_event at 0.5 s intervals; this is the main run-loop.
    while stop_event is None or (not stop_event.is_set()):
        await asyncio.sleep(0.5)

    # Graceful shutdown
    try:
        # On Windows, aiohttp cleanup can occasionally hang if a client keeps
        # a streaming response open. Bound the time so shutdown doesn't get
        # stuck and trigger the watchdog.
        await asyncio.wait_for(runner.cleanup(), timeout=2.5)
    except Exception:
        pass

    # Give underlying UDP transports a moment to settle before asyncio.run()
    # tears down the loop (helps avoid Windows Proactor "InvalidStateError" spam).
    try:
        await asyncio.sleep(0.15)
    except Exception:
        pass
