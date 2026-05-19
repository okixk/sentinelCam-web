/* capture.js - Browser webcam capture that uploads to MinIO via /api/recordings/upload */

(function () {
  const csrf = () => document.cookie.match(/csrf_token=([^;]+)/)?.[1] || "";
  const appUI = window.AppUI || {};
  const toast = typeof appUI.toast === "function" ? appUI.toast : () => null;

  const videoEl = document.getElementById("capture-preview");
  const placeholderEl = document.getElementById("capturePlaceholder");
  const startBtn = document.getElementById("start-camera-btn");
  const stopBtn = document.getElementById("stop-camera-btn");
  const captureBtn = document.getElementById("capture-btn");
  const recordBtn = document.getElementById("record-btn");
  const statusEl = document.getElementById("status");
  const statusBadge = document.getElementById("captureStatusBadge");
  const statusText = document.getElementById("captureStatusText");
  const recordTimer = document.getElementById("record-timer");
  const cameraSelect = document.getElementById("capture-camera");
  const resolutionSelect = document.getElementById("capture-resolution");
  const descriptionInput = document.getElementById("capture-description");

  let stream = null;
  let mediaRecorder = null;
  let recordChunks = [];
  let recordStart = 0;
  let recordTimerId = 0;

  function setStatus(text, state) {
    if (statusEl) statusEl.textContent = text || "";
    if (statusBadge && state) statusBadge.dataset.state = state;
    if (statusText && text) statusText.textContent = text;
  }

  function setRecording(active) {
    if (recordBtn) {
      recordBtn.textContent = active ? "Stop recording" : "Start recording";
      recordBtn.classList.toggle("danger", active);
    }
  }

  function showPreview(show) {
    if (placeholderEl) placeholderEl.style.display = show ? "none" : "";
    if (videoEl) videoEl.style.display = show ? "" : "none";
  }

  function pickMimeType(candidates) {
    if (!window.MediaRecorder || typeof MediaRecorder.isTypeSupported !== "function") {
      return null;
    }
    for (const mime of candidates) {
      if (MediaRecorder.isTypeSupported(mime)) return mime;
    }
    return null;
  }

  function pickExt(mime) {
    if (!mime) return "webm";
    if (mime.startsWith("video/webm")) return "webm";
    if (mime.startsWith("video/mp4")) return "mp4";
    return "webm";
  }

  function rectFromResolution(value) {
    const m = /^(\d+)x(\d+)$/.exec(String(value || "").trim());
    if (!m) return null;
    return { width: { ideal: parseInt(m[1], 10) }, height: { ideal: parseInt(m[2], 10) } };
  }

  async function listCameras() {
    if (!cameraSelect) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const cams = devices.filter(d => d.kind === "videoinput");
      if (!cams.length) return;
      const previous = cameraSelect.value;
      cameraSelect.innerHTML = '<option value="">Default</option>' + cams.map((d, i) =>
        `<option value="${d.deviceId}">${(d.label || "Camera " + (i + 1)).replace(/</g, "&lt;")}</option>`
      ).join("");
      if (previous && cams.some(d => d.deviceId === previous)) {
        cameraSelect.value = previous;
      }
    } catch (err) {
      console.warn("enumerateDevices failed", err);
    }
  }

  async function startCamera() {
    if (stream) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast("Browser does not expose camera APIs.", { tone: "error", title: "No camera" });
      return;
    }
    setStatus("Requesting camera...", "connecting");
    const video = rectFromResolution(resolutionSelect?.value) || { width: { ideal: 1280 }, height: { ideal: 720 } };
    if (cameraSelect && cameraSelect.value) {
      video.deviceId = { exact: cameraSelect.value };
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video, audio: false });
      videoEl.srcObject = stream;
      await videoEl.play().catch(() => {});
      showPreview(true);
      setStatus("Camera live.", "ok");
      startBtn.disabled = true;
      stopBtn.disabled = false;
      captureBtn.disabled = false;
      recordBtn.disabled = false;
      await listCameras();
    } catch (err) {
      setStatus("Camera error: " + err.message, "error");
      toast("Camera error: " + err.message, { tone: "error", title: "Camera failed" });
      stream = null;
    }
  }

  function stopCamera() {
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      try { mediaRecorder.stop(); } catch (_) {}
    }
    if (stream) {
      stream.getTracks().forEach(t => { try { t.stop(); } catch (_) {} });
      stream = null;
    }
    if (videoEl) videoEl.srcObject = null;
    showPreview(false);
    setStatus("Camera off.", "idle");
    startBtn.disabled = false;
    stopBtn.disabled = true;
    captureBtn.disabled = true;
    recordBtn.disabled = true;
    setRecording(false);
    if (recordTimer) recordTimer.textContent = "";
    window.clearInterval(recordTimerId);
    recordTimerId = 0;
  }

  async function uploadBlob(blob, kind, ext, mime) {
    const desc = (descriptionInput?.value || "").trim();
    const form = new FormData();
    form.append("type", kind);
    form.append("overlay_file", new File([blob], `capture_${Date.now()}.${ext}`, { type: mime }));
    if (desc) form.append("description", desc);
    setStatus("Uploading...", "connecting");
    try {
      const resp = await fetch("/api/recordings/upload", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf() },
        body: form,
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "HTTP " + resp.status);
      }
      const data = await resp.json();
      setStatus("Uploaded recording #" + data.id + ".", "ok");
      toast("Saved to gallery (#" + data.id + ").", { tone: "success", title: "Upload complete" });
    } catch (err) {
      setStatus("Upload failed: " + err.message, "error");
      toast("Upload failed: " + err.message, { tone: "error", title: "Upload failed" });
    }
  }

  async function captureImage() {
    if (!stream || !videoEl.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = videoEl.videoWidth;
    canvas.height = videoEl.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(videoEl, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.92));
    if (!blob) {
      toast("Could not encode image.", { tone: "error", title: "Capture failed" });
      return;
    }
    await uploadBlob(blob, "image", "jpg", "image/jpeg");
  }

  function startRecording() {
    if (!stream) return;
    const mime = pickMimeType([
      "video/webm;codecs=vp9",
      "video/webm;codecs=vp8",
      "video/webm",
      "video/mp4",
    ]);
    if (!mime) {
      toast("Browser does not support MediaRecorder.", { tone: "error", title: "Record failed" });
      return;
    }
    try {
      mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
    } catch (err) {
      toast("MediaRecorder error: " + err.message, { tone: "error", title: "Record failed" });
      return;
    }
    recordChunks = [];
    mediaRecorder.ondataavailable = e => { if (e.data && e.data.size) recordChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      const blob = new Blob(recordChunks, { type: mime });
      recordChunks = [];
      const duration = (Date.now() - recordStart) / 1000;
      window.clearInterval(recordTimerId);
      recordTimerId = 0;
      if (recordTimer) recordTimer.textContent = "";
      setRecording(false);
      const form = new FormData();
      form.append("type", "video");
      form.append("overlay_file", new File([blob], `capture_${Date.now()}.${pickExt(mime)}`, { type: mime }));
      form.append("duration", String(duration));
      const desc = (descriptionInput?.value || "").trim();
      if (desc) form.append("description", desc);
      setStatus("Uploading clip (" + duration.toFixed(1) + "s)...", "connecting");
      try {
        const resp = await fetch("/api/recordings/upload", {
          method: "POST",
          headers: { "X-CSRF-Token": csrf() },
          body: form,
        });
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          throw new Error(data.detail || data.error || "HTTP " + resp.status);
        }
        const data = await resp.json();
        setStatus("Uploaded recording #" + data.id + ".", "ok");
        toast("Saved video (#" + data.id + ").", { tone: "success", title: "Upload complete" });
      } catch (err) {
        setStatus("Upload failed: " + err.message, "error");
        toast("Upload failed: " + err.message, { tone: "error", title: "Upload failed" });
      }
    };
    mediaRecorder.start();
    recordStart = Date.now();
    setRecording(true);
    setStatus("Recording...", "connecting");
    recordTimerId = window.setInterval(() => {
      if (!recordTimer) return;
      const elapsed = (Date.now() - recordStart) / 1000;
      recordTimer.textContent = elapsed.toFixed(1) + "s";
      const maxSec = 5 * 60;
      if (elapsed >= maxSec) {
        try { mediaRecorder.stop(); } catch (_) {}
      }
    }, 250);
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      try { mediaRecorder.stop(); } catch (_) {}
    }
  }

  function toggleRecord() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      stopRecording();
    } else {
      startRecording();
    }
  }

  startBtn?.addEventListener("click", startCamera);
  stopBtn?.addEventListener("click", stopCamera);
  captureBtn?.addEventListener("click", captureImage);
  recordBtn?.addEventListener("click", toggleRecord);

  if (resolutionSelect) {
    resolutionSelect.addEventListener("change", () => {
      if (stream) {
        toast("Stop and restart the camera to apply the new resolution.", { tone: "warn", title: "Restart camera" });
      }
    });
  }
  if (cameraSelect) {
    cameraSelect.addEventListener("change", () => {
      if (stream) {
        toast("Stop and restart the camera to switch device.", { tone: "warn", title: "Restart camera" });
      }
    });
  }

  showPreview(false);
  listCameras();
  setStatus("Press \"Start camera\" to begin.", "idle");

  window.addEventListener("beforeunload", stopCamera);
})();
