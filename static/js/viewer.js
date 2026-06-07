/* viewer.js — Live page viewer + capture controls.
 *
 * Live transport is fragmented-MP4 (H.264) over HTTPS played via Media
 * Source Extensions. The worker GPU-encodes H.264; the web server relays
 * it with `ffmpeg -c:v copy` (no re-encode). This works over 443/Cloudflare
 * for both remote and VPN clients (WebRTC's UDP cannot traverse that).
 * On any MSE/codec/stream error — or when the user pins "MJPEG" — it falls
 * back to the MJPEG <img> stream. Hidden <select> elements hold the
 * canonical state (camera id / transport / clip duration).
 */
(function () {
  const csrf = () => document.cookie.match(/csrf_token=([^;]+)/)?.[1] || "";

  const els = {
    stage: document.getElementById("viewer-container"),
    select: document.getElementById("viewerCameraSelect"),
    transport: document.getElementById("viewerTransport"),
    reconnect: document.getElementById("viewerReconnectBtn"),
    video: document.getElementById("viewer-video"),
    mjpeg: document.getElementById("viewer-mjpeg"),
    placeholder: document.getElementById("viewerPlaceholder"),
    placeholderText: document.getElementById("viewerPlaceholderText"),
    placeholderHint: document.getElementById("viewerPlaceholderHint"),
    status: document.getElementById("viewer-status"),
    badge: document.getElementById("viewerStatusBadge"),
    badgeText: document.getElementById("viewerStatusText"),
    snapshotBtn: document.getElementById("viewerSnapshotBtn"),
    clipBtn: document.getElementById("viewerClipBtn"),
    clipBtnLabel: document.getElementById("viewerClipBtnLabel"),
    clipBtnProgress: null, // resolved below
    clipDuration: document.getElementById("viewerClipDuration"),
    clipDurationGroup: document.querySelector(".live-clip-duration"),
    cameraButton: document.getElementById("liveCameraButton"),
    cameraName: document.getElementById("liveCameraName"),
    cameraMenu: document.getElementById("liveCameraMenu"),
    cameraPicker: document.getElementById("liveCameraPicker"),
    settingsButton: document.getElementById("liveSettingsButton"),
    settingsMenu: document.getElementById("liveSettingsMenu"),
    settingsRoot: document.getElementById("liveSettings"),
    transportRadios: Array.from(document.querySelectorAll('input[name="viewerTransportChoice"]')),
    hudCamera: document.getElementById("liveHudCamera"),
    hudClock: document.getElementById("liveHudClock"),
    hudMode: document.getElementById("liveHudMode"),
    fullscreenBtn: document.getElementById("viewerFullscreenBtn"),
  };
  els.clipBtnProgress = document.querySelector(".live-action-progress-bar");
  if (!els.video || !els.mjpeg || !els.select) return;

  const appUI = window.AppUI || {};
  const toast = typeof appUI.toast === "function" ? appUI.toast : () => null;

  let currentMse = null;       // active MediaSource
  let currentAbort = null;     // AbortController for the live.mp4 fetch
  let currentCameraId = null;
  let currentCameraName = "";
  let fmp4FailedFor = new Set();
  let recordingTimerId = 0;
  let knownCameras = [];
  const PROGRESS_CIRC = 97.4; // matches the SVG r=15.5 stroke-dasharray

  // -----------------------------
  //  Status + media show/hide
  // -----------------------------

  function setStatus(text, state) {
    if (els.status) els.status.textContent = text || "";
    if (els.badge && state) els.badge.dataset.state = state;
    if (els.badgeText && text) els.badgeText.textContent = text;
  }

  function setHudMode(label) {
    if (!els.hudMode) return;
    if (label) {
      els.hudMode.textContent = label;
      els.hudMode.hidden = false;
    } else {
      els.hudMode.hidden = true;
    }
  }

  function showVideo() {
    els.video.hidden = false;
    els.mjpeg.hidden = true;
    if (els.placeholder) els.placeholder.hidden = true;
  }

  function showMjpeg() {
    els.mjpeg.hidden = false;
    els.video.hidden = true;
    if (els.placeholder) els.placeholder.hidden = true;
  }

  function showPlaceholder(message, hint) {
    els.video.hidden = true;
    els.mjpeg.hidden = true;
    if (els.placeholder) {
      els.placeholder.hidden = false;
      if (els.placeholderText) els.placeholderText.textContent = message || "Waiting for camera…";
      if (els.placeholderHint) els.placeholderHint.textContent = hint || "";
    }
    setHudMode("");
  }

  // -----------------------------
  //  WebRTC / MJPEG plumbing
  // -----------------------------

  function closeFmp4() {
    if (currentAbort) {
      try { currentAbort.abort(); } catch (_) {}
      currentAbort = null;
    }
    if (currentMse) {
      try { if (currentMse.readyState === "open") currentMse.endOfStream(); } catch (_) {}
      currentMse = null;
    }
    try {
      els.video.removeAttribute("src");
      els.video.load?.();
    } catch (_) {}
    stopOverlay();  // hoisted; defined in the detection-overlay section below
  }

  function closeMjpeg() {
    els.mjpeg.removeAttribute("src");
  }

  async function fetchCameras() {
    const resp = await fetch("/api/cameras", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    return Array.isArray(data.items) ? data.items : [];
  }

  function renderCameraOptions(cameras) {
    knownCameras = cameras;
    const previous = els.select.value;
    els.select.innerHTML = "";
    if (!cameras.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No cameras configured";
      els.select.appendChild(opt);
      els.select.disabled = true;
      els.cameraButton.disabled = true;
      els.cameraName.textContent = "No cameras";
      renderCameraMenu(cameras, null);
      return null;
    }
    els.select.disabled = false;
    els.cameraButton.disabled = false;
    for (const cam of cameras) {
      const opt = document.createElement("option");
      opt.value = String(cam.id);
      opt.textContent = `${cam.name}${cam.live ? "" : " (idle)"}`;
      els.select.appendChild(opt);
    }
    const desired = previous && cameras.some(c => String(c.id) === previous) ? previous : String(cameras[0].id);
    els.select.value = desired;
    const camId = parseInt(desired, 10);
    renderCameraMenu(cameras, camId);
    return camId;
  }

  function renderCameraMenu(cameras, activeId) {
    if (!els.cameraMenu) return;
    els.cameraMenu.innerHTML = "";
    for (const cam of cameras) {
      const li = document.createElement("li");
      li.setAttribute("role", "none");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.role = "menuitemradio";
      btn.className = "live-camera-menu-item" + (cam.id === activeId ? " is-active" : "");
      btn.setAttribute("aria-checked", cam.id === activeId ? "true" : "false");
      btn.dataset.cameraId = String(cam.id);
      btn.innerHTML = `
        <span class="live-camera-menu-item-name"></span>
        <span class="live-camera-menu-item-state" data-live="${cam.live ? "true" : "false"}">${cam.live ? "Live" : "Idle"}</span>
      `;
      btn.querySelector(".live-camera-menu-item-name").textContent = cam.name;
      btn.addEventListener("click", () => {
        closeCameraMenu();
        if (cam.id !== currentCameraId) {
          els.select.value = String(cam.id);
          connectToCamera(cam.id);
        }
      });
      li.appendChild(btn);
      els.cameraMenu.appendChild(li);
    }
    const active = cameras.find(c => c.id === activeId);
    if (active) {
      currentCameraName = active.name;
      els.cameraName.textContent = active.name;
      if (els.hudCamera) els.hudCamera.textContent = active.name;
    } else if (!cameras.length) {
      currentCameraName = "";
      if (els.hudCamera) els.hudCamera.textContent = "";
    }
  }

  // -----------------------------
  //  Camera / settings popovers
  // -----------------------------

  function openCameraMenu() {
    if (!els.cameraMenu || els.cameraButton.disabled) return;
    els.cameraMenu.hidden = false;
    els.cameraButton.setAttribute("aria-expanded", "true");
  }
  function closeCameraMenu() {
    if (!els.cameraMenu) return;
    els.cameraMenu.hidden = true;
    els.cameraButton.setAttribute("aria-expanded", "false");
  }
  function toggleCameraMenu() {
    if (els.cameraMenu.hidden) {
      closeSettingsMenu();
      openCameraMenu();
    } else {
      closeCameraMenu();
    }
  }

  function openSettingsMenu() {
    if (!els.settingsMenu) return;
    els.settingsMenu.hidden = false;
    els.settingsButton.setAttribute("aria-expanded", "true");
  }
  function closeSettingsMenu() {
    if (!els.settingsMenu) return;
    els.settingsMenu.hidden = true;
    els.settingsButton.setAttribute("aria-expanded", "false");
  }
  function toggleSettingsMenu() {
    if (els.settingsMenu.hidden) {
      closeCameraMenu();
      openSettingsMenu();
    } else {
      closeSettingsMenu();
    }
  }

  document.addEventListener("click", event => {
    if (els.cameraPicker && !els.cameraPicker.contains(event.target)) closeCameraMenu();
    if (els.settingsRoot && !els.settingsRoot.contains(event.target)) closeSettingsMenu();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      closeCameraMenu();
      closeSettingsMenu();
    }
  });

  // -----------------------------
  //  fMP4 (MSE) / MJPEG connect
  // -----------------------------

  const MSE_SUPPORTED = typeof window.MediaSource !== "undefined";
  const LIVE_BUFFER_GOAL_S = 6;   // evict buffered media older than this
  const LIVE_EDGE_LAG_S = 2.5;    // jump to the live edge if we drift this far back

  // Read the avc1 codec string straight out of the fMP4 init segment's avcC box
  // so the SourceBuffer mime matches the actual stream profile/level exactly.
  function findAvcCodec(bytes) {
    for (let i = 0; i + 8 < bytes.length; i++) {
      if (bytes[i] === 0x61 && bytes[i + 1] === 0x76 && bytes[i + 2] === 0x63 && bytes[i + 3] === 0x43) {
        const hex = n => n.toString(16).padStart(2, "0");
        return `avc1.${hex(bytes[i + 5])}${hex(bytes[i + 6])}${hex(bytes[i + 7])}`;
      }
    }
    return null;
  }

  async function tryFmp4(camId) {
    closeFmp4();
    if (!MSE_SUPPORTED) throw new Error("MediaSource not supported");

    const mediaSource = new MediaSource();
    currentMse = mediaSource;
    els.video.muted = true;
    els.video.autoplay = true;
    els.video.playsInline = true;
    els.video.src = URL.createObjectURL(mediaSource);

    await new Promise((resolve, reject) => {
      const to = window.setTimeout(() => reject(new Error("sourceopen timeout")), 5000);
      mediaSource.addEventListener("sourceopen", () => { window.clearTimeout(to); resolve(); }, { once: true });
    });
    if (currentMse !== mediaSource) throw new Error("superseded");

    const ctrl = new AbortController();
    currentAbort = ctrl;
    const resp = await fetch(`/api/cameras/${camId}/live.mp4`, { cache: "no-store", signal: ctrl.signal });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

    showVideo();
    setStatus("Live · H.264", "ok");
    setHudMode("H.264");
    startOverlay();  // worker boxes are not burned into this stream
    els.video.play?.().catch(() => {});

    // Watchdog: if no frame decodes within 6s the pipeline is wedged.
    const stalled = window.setTimeout(() => {
      if (currentAbort === ctrl && !els.video.videoWidth) {
        try { ctrl.abort(); } catch (_) {}
        fmp4FailedFor.add(camId);
        if (currentCameraId === camId && (els.transport?.value || "auto") === "auto") {
          startMjpeg(camId, "Live H.264 stalled — switched to MJPEG.");
        }
      }
    }, 6000);
    els.video.addEventListener("loadeddata", () => window.clearTimeout(stalled), { once: true });

    const reader = resp.body.getReader();
    let sourceBuffer = null;
    const queue = [];
    let initBuf = new Uint8Array(0);

    const pump = () => {
      if (!sourceBuffer || sourceBuffer.updating || !queue.length) return;
      try { sourceBuffer.appendBuffer(queue.shift()); } catch (err) { console.warn("appendBuffer failed", err); }
    };
    const trim = () => {
      try {
        if (!sourceBuffer || sourceBuffer.updating || !els.video.buffered.length) return;
        const end = els.video.buffered.end(els.video.buffered.length - 1);
        const start = els.video.buffered.start(0);
        if (end - start > LIVE_BUFFER_GOAL_S) sourceBuffer.remove(start, end - LIVE_BUFFER_GOAL_S);
        if (end - els.video.currentTime > LIVE_EDGE_LAG_S) els.video.currentTime = end - 0.3;
      } catch (_) {}
    };

    (async () => {
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done || currentAbort !== ctrl) break;
          if (!sourceBuffer) {
            const merged = new Uint8Array(initBuf.length + value.length);
            merged.set(initBuf, 0); merged.set(value, initBuf.length);
            initBuf = merged;
            const codec = findAvcCodec(initBuf);
            if (!codec) continue;
            const mime = `video/mp4; codecs="${codec}"`;
            if (!MediaSource.isTypeSupported(mime)) throw new Error("codec unsupported: " + mime);
            sourceBuffer = mediaSource.addSourceBuffer(mime);
            try { sourceBuffer.mode = "sequence"; } catch (_) {}
            sourceBuffer.addEventListener("updateend", () => { pump(); trim(); });
            queue.push(initBuf);
            initBuf = new Uint8Array(0);
            pump();
          } else {
            queue.push(value);
            pump();
          }
        }
      } catch (err) {
        if (currentAbort === ctrl) {
          window.clearTimeout(stalled);
          console.warn("fMP4 stream error", err);
          fmp4FailedFor.add(camId);
          if (currentCameraId === camId && (els.transport?.value || "auto") === "auto") {
            startMjpeg(camId, "Live H.264 dropped — switched to MJPEG.");
          }
        }
      }
    })();
  }

  function startMjpeg(camId, statusText) {
    closeFmp4();
    const url = `/api/cameras/${camId}/stream.mjpg?ts=${Date.now()}`;
    els.mjpeg.src = url;
    showMjpeg();
    setStatus(statusText || "Live · MJPEG", "ok");
    setHudMode("MJPEG");
  }

  // -----------------------------
  //  Detection overlay (H.264 view)
  //
  //  The H.264 live lane is the camera's own stream relayed without
  //  re-encoding, so YOLO boxes are NOT burned in (unlike MJPEG). The worker
  //  publishes normalized box coordinates instead; we poll them and draw on
  //  a canvas over the <video>. A ~200-byte JSON poll twice a second costs
  //  nothing compared to re-encoding video.
  // -----------------------------

  const overlayCanvas = document.createElement("canvas");
  overlayCanvas.id = "viewer-overlay";
  overlayCanvas.style.cssText =
    "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;";
  overlayCanvas.hidden = true;
  els.video.insertAdjacentElement("afterend", overlayCanvas);

  const OVERLAY_POLL_MS = 600;
  const OVERLAY_STALE_S = 4;  // clear boxes when detections stop arriving
  const OVERLAY_COLORS = ["#36a2eb", "#ff6384", "#ffce56", "#9966ff", "#4bc0c0", "#ff9f40"];
  let overlayTimer = 0;
  let overlayBoxes = [];

  function overlayColor(label) {
    let h = 0;
    for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) >>> 0;
    return OVERLAY_COLORS[h % OVERLAY_COLORS.length];
  }

  function drawOverlay() {
    if (overlayCanvas.hidden) return;
    const vw = els.video.videoWidth, vh = els.video.videoHeight;
    const ew = els.video.clientWidth, eh = els.video.clientHeight;
    if (!vw || !vh || !ew || !eh) return;
    const dpr = window.devicePixelRatio || 1;
    if (overlayCanvas.width !== Math.round(ew * dpr) || overlayCanvas.height !== Math.round(eh * dpr)) {
      overlayCanvas.width = Math.round(ew * dpr);
      overlayCanvas.height = Math.round(eh * dpr);
    }
    const ctx = overlayCanvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, ew, eh);
    if (!overlayBoxes.length) return;
    // Map normalized stream coords onto the displayed video rectangle,
    // accounting for object-fit letterboxing.
    const scale = Math.min(ew / vw, eh / vh);
    const dw = vw * scale, dh = vh * scale;
    const ox = (ew - dw) / 2, oy = (eh - dh) / 2;
    ctx.font = "12px system-ui, sans-serif";
    ctx.lineWidth = 2;
    for (const b of overlayBoxes) {
      const x = ox + b.x * dw, y = oy + b.y * dh, w = b.w * dw, h = b.h * dh;
      const color = overlayColor(b.label || "");
      ctx.strokeStyle = color;
      ctx.strokeRect(x, y, w, h);
      const text = `${b.label || "?"} ${Math.round((b.conf || 0) * 100)}%`;
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = color;
      ctx.fillRect(x - 1, Math.max(0, y - 17), tw + 8, 17);
      ctx.fillStyle = "#fff";
      ctx.fillText(text, x + 3, Math.max(12, y - 5));
    }
  }

  async function pollDetections() {
    if (!currentCameraId || els.video.hidden) return;
    try {
      const resp = await fetch(`/api/cameras/${currentCameraId}/detections`, { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      overlayBoxes = (data.age_s != null && data.age_s <= OVERLAY_STALE_S && Array.isArray(data.boxes))
        ? data.boxes : [];
    } catch (_) {
      overlayBoxes = [];
    }
    drawOverlay();
  }

  function startOverlay() {
    stopOverlay();
    overlayCanvas.hidden = false;
    overlayTimer = window.setInterval(pollDetections, OVERLAY_POLL_MS);
    pollDetections();
  }

  function stopOverlay() {
    if (overlayTimer) {
      window.clearInterval(overlayTimer);
      overlayTimer = 0;
    }
    overlayBoxes = [];
    const ctx = overlayCanvas.getContext("2d");
    if (ctx) ctx.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height);
    overlayCanvas.hidden = true;
  }

  window.addEventListener("resize", drawOverlay);

  async function connectToCamera(camId) {
    if (!camId) {
      showPlaceholder("No camera selected", "Pick a camera from the dropdown above the video.");
      setStatus("No camera selected.", "neutral");
      setCaptureEnabled(false);
      return;
    }
    currentCameraId = camId;
    setCaptureEnabled(true);
    const mode = els.transport?.value || "auto";
    setStatus("Connecting…", "connecting");
    showPlaceholder(`Connecting to ${currentCameraName || "camera"}…`, "");
    closeMjpeg();

    // Update menu state immediately for snappy UX.
    renderCameraMenu(knownCameras, camId);

    if (mode === "mjpeg") {
      startMjpeg(camId);
      return;
    }
    if (mode === "auto" && fmp4FailedFor.has(camId)) {
      startMjpeg(camId, "Live H.264 failed earlier — staying on MJPEG.");
      return;
    }

    try {
      await tryFmp4(camId);
    } catch (err) {
      console.warn("fMP4 failed", err);
      fmp4FailedFor.add(camId);
      if (mode === "fmp4") {
        setStatus("Live H.264 failed: " + err.message, "error");
        showPlaceholder("Live H.264 failed", "Switch transport to Auto or MJPEG.");
        return;
      }
      startMjpeg(camId, "Live H.264 unavailable — using MJPEG.");
    }
  }

  async function refresh() {
    try {
      const cameras = await fetchCameras();
      const camId = renderCameraOptions(cameras);
      if (camId == null) {
        closeFmp4();
        closeMjpeg();
        showPlaceholder(
          "No cameras configured",
          "Ask an admin to issue a camera token, then point a Pi at /api/ingest/<id>."
        );
        setStatus("No cameras configured.", "neutral");
        return;
      }
      if (camId !== currentCameraId) {
        await connectToCamera(camId);
      }
    } catch (err) {
      setStatus("Failed to load cameras: " + err.message, "error");
    }
  }

  // -----------------------------
  //  Capture buttons
  // -----------------------------

  function setCaptureEnabled(enabled) {
    if (els.snapshotBtn) els.snapshotBtn.disabled = !enabled;
    if (els.clipBtn) els.clipBtn.disabled = !enabled;
    if (els.clipDurationGroup) {
      els.clipDurationGroup.querySelectorAll("button").forEach(b => { b.disabled = !enabled; });
    }
  }

  async function doSnapshot() {
    if (!currentCameraId || !els.snapshotBtn) return;
    els.snapshotBtn.disabled = true;
    const label = els.snapshotBtn.querySelector(".live-action-label");
    const originalLabel = label?.textContent || "Snapshot";
    if (label) label.textContent = "Saving…";
    try {
      const resp = await fetch(`/api/cameras/${currentCameraId}/snapshot`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({}),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.detail?.error || data.detail || data.error || ("HTTP " + resp.status));
      }
      toast(`Saved snapshot #${data.id}.`, { tone: "success", title: "Snapshot" });
    } catch (err) {
      toast("Snapshot failed: " + err.message, { tone: "error", title: "Snapshot failed" });
    } finally {
      if (label) label.textContent = originalLabel;
      els.snapshotBtn.disabled = !currentCameraId;
    }
  }

  function startRecordingAnimation(durationSec) {
    if (!els.clipBtn) return;
    els.clipBtn.classList.add("is-recording");
    let elapsed = 0;
    const totalMs = durationSec * 1000;
    const tickMs = 100;
    if (els.clipBtnLabel) els.clipBtnLabel.textContent = `Recording · ${durationSec}s`;
    setProgress(0);
    recordingTimerId = window.setInterval(() => {
      elapsed += tickMs;
      const fraction = Math.min(1, elapsed / totalMs);
      setProgress(fraction);
      const remaining = Math.max(0, Math.ceil((totalMs - elapsed) / 1000));
      if (els.clipBtnLabel) {
        if (remaining > 0) {
          els.clipBtnLabel.textContent = `Recording · ${remaining}s`;
        } else {
          els.clipBtnLabel.textContent = "Encoding…";
        }
      }
      if (fraction >= 1) {
        window.clearInterval(recordingTimerId);
        recordingTimerId = 0;
      }
    }, tickMs);
  }

  function setProgress(fraction) {
    if (!els.clipBtnProgress) return;
    const offset = PROGRESS_CIRC * (1 - Math.max(0, Math.min(1, fraction)));
    els.clipBtnProgress.style.strokeDashoffset = String(offset);
  }

  function stopRecordingAnimation() {
    if (recordingTimerId) {
      window.clearInterval(recordingTimerId);
      recordingTimerId = 0;
    }
    if (els.clipBtn) els.clipBtn.classList.remove("is-recording");
    setProgress(0);
    if (els.clipBtnLabel) els.clipBtnLabel.textContent = "Record clip";
  }

  async function doClip() {
    if (!currentCameraId || !els.clipBtn) return;
    const duration = parseInt(els.clipDuration?.value || "30", 10) || 30;
    setCaptureEnabled(false);
    startRecordingAnimation(duration);
    try {
      const resp = await fetch(`/api/cameras/${currentCameraId}/clip`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ duration_s: duration }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.detail?.error || data.detail || data.error || ("HTTP " + resp.status));
      }
      toast(`Saved clip #${data.id} (${duration}s).`, { tone: "success", title: "Clip recorded" });
    } catch (err) {
      toast("Clip failed: " + err.message, { tone: "error", title: "Clip failed" });
    } finally {
      stopRecordingAnimation();
      setCaptureEnabled(!!currentCameraId);
    }
  }

  // -----------------------------
  //  HUD clock
  // -----------------------------

  function tickClock() {
    if (!els.hudClock) return;
    const d = new Date();
    const pad = n => String(n).padStart(2, "0");
    els.hudClock.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  // -----------------------------
  //  Fullscreen
  // -----------------------------

  function toggleFullscreen() {
    if (!els.stage) return;
    if (document.fullscreenElement === els.stage) {
      document.exitFullscreen?.();
    } else {
      els.stage.requestFullscreen?.().catch(() => {});
    }
  }

  // -----------------------------
  //  Wiring
  // -----------------------------

  els.snapshotBtn?.addEventListener("click", doSnapshot);
  els.clipBtn?.addEventListener("click", doClip);
  els.cameraButton?.addEventListener("click", toggleCameraMenu);
  els.settingsButton?.addEventListener("click", toggleSettingsMenu);
  els.fullscreenBtn?.addEventListener("click", toggleFullscreen);

  els.select.addEventListener("change", () => {
    const camId = parseInt(els.select.value, 10);
    if (Number.isFinite(camId)) connectToCamera(camId);
  });

  // Transport: bind the new radio group to the hidden <select>.
  els.transportRadios.forEach(radio => {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      els.transport.value = radio.value;
      fmp4FailedFor.delete(currentCameraId);
      if (currentCameraId) connectToCamera(currentCameraId);
    });
  });

  els.reconnect?.addEventListener("click", () => {
    closeSettingsMenu();
    fmp4FailedFor.delete(currentCameraId);
    if (currentCameraId) connectToCamera(currentCameraId);
  });

  // Segmented clip-duration control. Mirrors the hidden <select>.
  els.clipDurationGroup?.querySelectorAll("[data-clip-duration]").forEach(btn => {
    btn.addEventListener("click", () => {
      const value = btn.dataset.clipDuration;
      if (els.clipDuration) els.clipDuration.value = value;
      els.clipDurationGroup.querySelectorAll("[data-clip-duration]").forEach(b => {
        const active = b === btn;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-pressed", active ? "true" : "false");
      });
    });
  });

  // Keyboard shortcuts. Skip when the user is typing somewhere.
  document.addEventListener("keydown", event => {
    if (event.defaultPrevented) return;
    const target = event.target;
    if (target && (target.matches("input, textarea, select") || target.isContentEditable)) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const key = event.key.toLowerCase();
    if (key === "s" && els.snapshotBtn && !els.snapshotBtn.disabled) {
      event.preventDefault();
      doSnapshot();
    } else if (key === "r" && els.clipBtn && !els.clipBtn.disabled) {
      event.preventDefault();
      doClip();
    } else if (key === "f") {
      event.preventDefault();
      toggleFullscreen();
    }
  });

  window.addEventListener("beforeunload", () => {
    closeFmp4();
    closeMjpeg();
  });

  tickClock();
  window.setInterval(tickClock, 1000);
  refresh();
  window.setInterval(refresh, 10_000);
})();
