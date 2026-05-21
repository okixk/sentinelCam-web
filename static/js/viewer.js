/* viewer.js — watches the worker-processed camera stream.
 *
 * Default behaviour: try WebRTC first (lowest latency, best bitrate
 * efficiency), fall back to MJPEG if SDP negotiation fails, the peer
 * connection goes to "failed", or no frames arrive within a short window.
 * Admins can pin a transport with the dropdown next to the camera picker.
 */
(function () {
  const csrf = () => document.cookie.match(/csrf_token=([^;]+)/)?.[1] || "";

  const els = {
    select: document.getElementById("viewerCameraSelect"),
    transport: document.getElementById("viewerTransport"),
    reconnect: document.getElementById("viewerReconnectBtn"),
    video: document.getElementById("viewer-video"),
    mjpeg: document.getElementById("viewer-mjpeg"),
    placeholder: document.getElementById("viewerPlaceholder"),
    status: document.getElementById("viewer-status"),
    badge: document.getElementById("viewerStatusBadge"),
    badgeText: document.getElementById("viewerStatusText"),
  };
  if (!els.video || !els.mjpeg || !els.select) return;

  let currentPc = null;
  let currentCameraId = null;
  let currentMode = null;
  let webrtcFailedFor = new Set();

  function setStatus(text, state) {
    if (els.status) els.status.textContent = text || "";
    if (els.badge && state) els.badge.dataset.state = state;
    if (els.badgeText && text) els.badgeText.textContent = text;
  }

  function showVideo() {
    els.video.hidden = false;
    els.mjpeg.hidden = true;
    if (els.placeholder) els.placeholder.style.display = "none";
  }

  function showMjpeg() {
    els.mjpeg.hidden = false;
    els.video.hidden = true;
    if (els.placeholder) els.placeholder.style.display = "none";
  }

  function showPlaceholder(message) {
    els.video.hidden = true;
    els.mjpeg.hidden = true;
    if (els.placeholder) {
      els.placeholder.style.display = "";
      els.placeholder.textContent = message || "Waiting for camera...";
    }
  }

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
    const previous = els.select.value;
    els.select.innerHTML = "";
    if (!cameras.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No cameras configured";
      els.select.appendChild(opt);
      els.select.disabled = true;
      return null;
    }
    els.select.disabled = false;
    for (const cam of cameras) {
      const opt = document.createElement("option");
      opt.value = String(cam.id);
      opt.textContent = `${cam.name}${cam.live ? "" : " (idle)"}`;
      els.select.appendChild(opt);
    }
    const desired = previous && cameras.some(c => String(c.id) === previous) ? previous : String(cameras[0].id);
    els.select.value = desired;
    return parseInt(desired, 10);
  }

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
        setStatus("Live (WebRTC)", "ok");
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
            showPlaceholder("WebRTC disconnected. Pick MJPEG or hit Reconnect.");
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
    currentMode = "webrtc";
  }

  function startMjpeg(camId, statusText) {
    closePeer();
    const url = `/api/cameras/${camId}/stream.mjpg?ts=${Date.now()}`;
    els.mjpeg.src = url;
    showMjpeg();
    currentMode = "mjpeg";
    setStatus(statusText || "Live (MJPEG)", "ok");
  }

  async function connectToCamera(camId) {
    if (!camId) {
      showPlaceholder("No camera selected.");
      setStatus("No camera selected.", "neutral");
      return;
    }
    currentCameraId = camId;
    const mode = els.transport?.value || "auto";
    setStatus("Connecting...", "connecting");
    closeMjpeg();

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
        showPlaceholder("WebRTC failed. Pick Auto or MJPEG.");
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
        showPlaceholder("No cameras configured. Ask an admin to issue a camera token.");
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

  els.select.addEventListener("change", () => {
    const camId = parseInt(els.select.value, 10);
    if (Number.isFinite(camId)) connectToCamera(camId);
  });
  els.transport?.addEventListener("change", () => {
    webrtcFailedFor.delete(currentCameraId);
    if (currentCameraId) connectToCamera(currentCameraId);
  });
  els.reconnect?.addEventListener("click", () => {
    webrtcFailedFor.delete(currentCameraId);
    if (currentCameraId) connectToCamera(currentCameraId);
  });

  window.addEventListener("beforeunload", () => {
    closePeer();
    closeMjpeg();
  });

  refresh();
  window.setInterval(refresh, 10_000);
})();
