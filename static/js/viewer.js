/* viewer.js — Live page viewer + capture controls.
 *
 * Tries WebRTC first, falls back to MJPEG on negotiation errors, ICE
 * stalls (>5s), or "failed"/"disconnected" peer state. The transport
 * pinner, reconnect button, camera picker, snapshot/record buttons, and
 * fullscreen toggle live inside the new HUD-style UI. Hidden <select>
 * elements still hold the canonical state so the WebRTC/MJPEG logic
 * below can read camera id / transport / clip duration without caring
 * about which control surface the user touched.
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

  let currentPc = null;
  let currentCameraId = null;
  let currentCameraName = "";
  let webrtcFailedFor = new Set();
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

  function closePeer() {
    if (currentPc) {
      try { currentPc.close(); } catch (_) {}
      currentPc = null;
    }
    if (els.video.srcObject) {
      const tracks = els.video.srcObject.getTracks?.() || [];
      tracks.forEach(t => { try { t.stop(); } catch (_) {} });
      els.video.srcObject = null;
    }
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
  //  WebRTC / MJPEG connect
  // -----------------------------

  async function tryWebRTC(camId) {
    closePeer();
    const pc = new RTCPeerConnection({ iceServers: [] });
    currentPc = pc;
    const stalledTimer = window.setTimeout(() => {
      if (currentPc !== pc) return;
      if (!els.video.videoWidth) {
        console.warn("WebRTC stalled, falling back");
        try { pc.close(); } catch (_) {}
        if (currentPc === pc) currentPc = null;
        webrtcFailedFor.add(camId);
        if ((els.transport?.value || "auto") === "auto") {
          startMjpeg(camId, "WebRTC stalled — falling back to MJPEG.");
        }
      }
    }, 5000);

    pc.addEventListener("track", event => {
      if (event.track.kind === "video") {
        els.video.srcObject = event.streams[0];
        showVideo();
        setStatus("Live · WebRTC", "ok");
        setHudMode("WebRTC");
        window.clearTimeout(stalledTimer);
      }
    });
    pc.addEventListener("connectionstatechange", () => {
      if (currentPc !== pc) return;
      if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
        window.clearTimeout(stalledTimer);
        if (pc.connectionState !== "closed") {
          webrtcFailedFor.add(camId);
        }
        if (currentPc === pc) {
          currentPc = null;
          if ((els.transport?.value || "auto") === "auto") {
            startMjpeg(camId, "WebRTC dropped — switched to MJPEG.");
          } else if ((els.transport?.value || "auto") === "webrtc") {
            setStatus("WebRTC disconnected.", "error");
            showPlaceholder("WebRTC disconnected", "Switch transport to MJPEG or hit Reconnect.");
          }
        }
      }
    });

    pc.addTransceiver("video", { direction: "recvonly" });
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const resp = await fetch(`/api/cameras/${camId}/webrtc/offer`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      const msg = data?.detail?.error || data?.detail || data?.error || ("HTTP " + resp.status);
      throw new Error(msg);
    }
    const answer = await resp.json();
    await pc.setRemoteDescription(new RTCSessionDescription(answer));
  }

  function startMjpeg(camId, statusText) {
    closePeer();
    const url = `/api/cameras/${camId}/stream.mjpg?ts=${Date.now()}`;
    els.mjpeg.src = url;
    showMjpeg();
    setStatus(statusText || "Live · MJPEG", "ok");
    setHudMode("MJPEG");
  }

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
    if (mode === "auto" && webrtcFailedFor.has(camId)) {
      startMjpeg(camId, "WebRTC failed earlier — staying on MJPEG.");
      return;
    }

    try {
      await tryWebRTC(camId);
    } catch (err) {
      console.warn("WebRTC failed", err);
      webrtcFailedFor.add(camId);
      if (mode === "webrtc") {
        setStatus("WebRTC failed: " + err.message, "error");
        showPlaceholder("WebRTC failed", "Switch transport to Auto or MJPEG.");
        return;
      }
      startMjpeg(camId, "WebRTC failed — falling back to MJPEG.");
    }
  }

  async function refresh() {
    try {
      const cameras = await fetchCameras();
      const camId = renderCameraOptions(cameras);
      if (camId == null) {
        closePeer();
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
      webrtcFailedFor.delete(currentCameraId);
      if (currentCameraId) connectToCamera(currentCameraId);
    });
  });

  els.reconnect?.addEventListener("click", () => {
    closeSettingsMenu();
    webrtcFailedFor.delete(currentCameraId);
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
    closePeer();
    closeMjpeg();
  });

  tickClock();
  window.setInterval(tickClock, 1000);
  refresh();
  window.setInterval(refresh, 10_000);
})();
