/* stream.js – WebRTC/MJPEG streaming + capture/record */

const DEFAULT_DIRECT_BASE = "http://127.0.0.1:8080";
const STATE_POLL_INTERVAL_MS = 500;
const ICE_GATHER_TIMEOUT_MS = 4000;
const WEBRTC_RETRY_DELAY_MS = 2000;
const MJPEG_PROBE_TIMEOUT_MS = 5000;
const MODEL_SWITCH_COMMANDS = new Set(["m", "n", "next", "prev", "previous"]);
const QUIT_COMMANDS = new Set(["q", "quit", "exit", "stop"]);
const IS_FILE_PROTOCOL = window.location.protocol === "file:";
const PAGE_ORIGIN = window.location.origin || "";
const PAGE_PROTOCOL = window.location.protocol || "";

let statePollTimer = null;
let codecPollTimer = null;
let cmdSeq = 0;
let pendingSwitch = null;
let lastPollErrorText = "";
let currentTarget = null;
let currentCapabilities = { webrtcAvailable: true, mjpegAvailable: true, backend: "" };
let activePeer = null;
let reconnectTimer = null;
let connectGeneration = 0;
let intentionalDisconnect = false;
let pausedForHidden = false;
let mjpegProbeId = 0;
let lastInboundVideoSample = null;
let webrtcConsecutiveFailures = 0;

// Recording state
let mediaRecorder = null;
let recordedChunks = [];
let recordStartTime = null;
let recordTimerInterval = null;

const baseInputEl = document.getElementById("base");
const videoEl = document.getElementById("stream");
const fallbackEl = document.getElementById("streamFallback");
const placeholderEl = document.getElementById("streamPlaceholder");
const connectionNoteEl = document.getElementById("connectionNote");
const streamModeNoteEl = document.getElementById("streamModeNote");
const codecEl = document.getElementById("stateCodec");
const bitrateEl = document.getElementById("stateBitrate");
const statusEl = document.getElementById("status");
const busyOverlayEl = document.getElementById("busyOverlay");
const busyTitleEl = document.getElementById("busyTitle");
const btnPrevEl = document.getElementById("btnPrev");
const btnNextEl = document.getElementById("btnNext");
const stateConnectionEl = document.getElementById("stateConnection");
const stateStreamModeEl = document.getElementById("stateStreamMode");
const statePresetEl = document.getElementById("statePreset");
const stateDetEl = document.getElementById("stateDet");
const statePoseEl = document.getElementById("statePose");
const stateFpsEl = document.getElementById("stateFps");
const stateInferenceEl = document.getElementById("stateInference");
const captureBtn = document.getElementById("capture-btn");
const recordBtn = document.getElementById("record-btn");
const recordTimerEl = document.getElementById("record-timer");

function getCsrf() {
  return document.cookie.match(/csrf_token=([^;]+)/)?.[1] || '';
}

function createError(message, details = {}) {
  const error = new Error(message);
  Object.assign(error, details);
  return error;
}

function formatError(error) {
  if (!error) return "Unknown error";
  if (typeof error === "string") return error;
  if (error.message) return error.message;
  return String(error);
}

function isModelSwitchCommand(cmd) {
  return MODEL_SWITCH_COMMANDS.has(String(cmd || "").toLowerCase());
}

function isQuitCommand(cmd) {
  return QUIT_COMMANDS.has(String(cmd || "").toLowerCase());
}

function capabilitiesFromState(state) {
  const payload = state && typeof state === "object" ? state : {};
  return {
    webrtcAvailable: typeof payload.webrtc_available === "boolean" ? payload.webrtc_available : true,
    mjpegAvailable: typeof payload.mjpeg_available === "boolean" ? payload.mjpeg_available : true,
    backend: typeof payload.stream_backend === "string" ? payload.stream_backend : ""
  };
}

function normalizeBaseUrl(url) {
  return url.href.replace(/\/+$/, "");
}

function extractServerError(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return "";
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed.error === "string" && parsed.error.trim()) {
      return parsed.error.trim();
    }
  } catch (e) { /* fall through */ }
  return trimmed.replace(/\s+/g, " ");
}

function describeWorkerHttpError(status, serverError, url, target) {
  if (status === 401) {
    return "Worker auth is enabled at " + url + ". Use proxy mode instead.";
  }
  if (status === 404 && /\/api\/webrtc\/offer(?:\?|$)/i.test(url)) {
    return "Worker does not expose WebRTC at " + url + ". Start worker with WebRTC enabled or use MJPEG.";
  }
  if (status === 403 && /origin not allowed/i.test(serverError)) {
    return target.kind === "proxy"
      ? "Worker rejected proxy origin for " + url + ". Check WEB_ALLOWED_ORIGINS on the worker."
      : "Worker rejected browser origin. Use proxy mode or add origin to WEB_ALLOWED_ORIGINS.";
  }
  if (status === 403) {
    return "Worker rejected " + url + (serverError ? ": " + serverError : ".");
  }
  if (target.kind === "proxy" && status === 502) {
    return "Proxy could not reach worker at " + url + ". Start the worker.";
  }
  return "HTTP " + status + " from " + url + (serverError ? ": " + serverError : "");
}

function describeWorkerNetworkError(error, url, target) {
  if (PAGE_PROTOCOL === "https:" && /^http:\/\//i.test(url)) {
    return "Mixed content blocked: page is HTTPS but worker URL is HTTP. Use proxy mode.";
  }
  if (target.kind === "proxy") {
    return "Could not reach proxy at " + url + ". Start web server or fix proxy setup.";
  }
  return "Could not reach " + url + ". Make sure the worker is running.";
}

function resolveTargetBase() {
  const raw = baseInputEl.value.trim();
  if (IS_FILE_PROTOCOL) {
    if (!raw || raw === "/") {
      return { kind: "direct", base: DEFAULT_DIRECT_BASE, display: DEFAULT_DIRECT_BASE };
    }
    let directUrl;
    try { directUrl = new URL(raw); } catch (e) {
      throw new Error("When opened via file://, enter a full http:// worker URL.");
    }
    if (!/^https?:$/.test(directUrl.protocol)) throw new Error("Base URL must use http:// or https://.");
    const normalized = normalizeBaseUrl(directUrl);
    return { kind: "direct", base: normalized, display: normalized };
  }
  if (!raw || raw === "/") {
    return { kind: "proxy", base: "", display: PAGE_ORIGIN || "/" };
  }
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(raw)) {
    throw new Error("Use '/' for proxy mode or enter a full http(s) URL.");
  }
  let url;
  try { url = new URL(raw); } catch (e) {
    throw new Error("Base URL must be '/' or a full http(s) URL.");
  }
  if (!/^https?:$/.test(url.protocol)) throw new Error("Base URL must use http:// or https://.");
  if (url.origin === PAGE_ORIGIN && (url.pathname === "/" || url.pathname === "")) {
    return { kind: "proxy", base: "", display: PAGE_ORIGIN || "/" };
  }
  const normalized = normalizeBaseUrl(url);
  return { kind: "direct", base: normalized, display: normalized };
}

function endpointFor(path, target = currentTarget || resolveTargetBase()) {
  return (target.base || "") + path;
}

async function workerFetch(path, init = {}, options = {}) {
  const target = options.target || currentTarget || resolveTargetBase();
  const url = endpointFor(path, target);
  let response;
  try {
    response = await fetch(url, Object.assign({ cache: "no-store" }, init));
  } catch (error) {
    throw createError(describeWorkerNetworkError(error, url, target), {
      cause: error, url, status: 0, target, isNetworkError: true,
      isMixedContent: PAGE_PROTOCOL === "https:" && /^http:\/\//i.test(url)
    });
  }
  const bodyText = await response.text();
  if (!response.ok) {
    const serverError = extractServerError(bodyText);
    throw createError(describeWorkerHttpError(response.status, serverError, url, target), {
      url, status: response.status, target, bodyText, serverError,
      isAuthError: response.status === 401 || response.status === 403,
      isOriginError: response.status === 403 && /origin not allowed/i.test(serverError)
    });
  }
  if (options.expect === "json") {
    if (!bodyText) return {};
    try { return JSON.parse(bodyText); } catch (e) {
      throw createError("Worker returned invalid JSON from " + url + ".", { cause: e, url, status: response.status, target, bodyText });
    }
  }
  return bodyText;
}

function shouldRetryWebRtc(error) {
  if (!error) return false;
  if (error.status || error.isAuthError || error.isOriginError || error.isMixedContent) return false;
  const message = formatError(error).toLowerCase();
  return !message.includes("does not support webrtc") && !message.includes("mixed content");
}

function shouldSkipMjpegFallback(error) {
  if (!error) return false;
  if (error.isAuthError || error.isOriginError || error.isMixedContent) return true;
  const message = formatError(error).toLowerCase();
  return message.includes("worker tokens") || message.includes("origin") || message.includes("mixed content");
}

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.style.color = isError ? "var(--danger)" : "var(--muted)";
}

function setBusy(on, title = "Switching model...") {
  busyTitleEl.textContent = title;
  busyOverlayEl.classList.toggle("show", !!on);
  btnPrevEl.disabled = !!on;
  btnNextEl.disabled = !!on;
}

function setPlaceholder(text = "Not connected.", show = true) {
  placeholderEl.textContent = text;
  placeholderEl.classList.toggle("hidden", !show);
}

function setConnectionMode(target) {
  stateConnectionEl.textContent = target.kind === "proxy" ? "Proxy" : "Direct";
  if (target.kind === "proxy") {
    connectionNoteEl.textContent = "Same-origin proxy mode. The web server forwards /api/* and /stream.mjpg to the worker.";
    connectionNoteEl.className = "small";
  } else {
    connectionNoteEl.textContent = "Direct browser mode. Use only for local/dev.";
    connectionNoteEl.className = "small warn";
  }
}

function setStreamMode(mode, detail = "") {
  const labelMap = { idle: "Idle", connecting: "Connecting", webrtc: "WebRTC", mjpeg: "MJPEG fallback", error: "Error" };
  stateStreamModeEl.textContent = labelMap[mode] || "Idle";
  stateStreamModeEl.dataset.mode = mode;
  streamModeNoteEl.textContent = detail;
  streamModeNoteEl.className = mode === "error" ? "small error" : mode === "mjpeg" ? "small warn" : "small";
}

function hideMedia() {
  videoEl.classList.remove("show");
  fallbackEl.classList.remove("show");
}

function showVideo() { hideMedia(); videoEl.classList.add("show"); }
function showFallback() { hideMedia(); fallbackEl.classList.add("show"); }

function clearVideoStream() {
  const stream = videoEl.srcObject;
  if (stream && typeof stream.getTracks === "function") {
    stream.getTracks().forEach(track => track.stop());
  }
  videoEl.srcObject = null;
  videoEl.classList.remove("show");
}

function closePeer() {
  const peer = activePeer;
  activePeer = null;
  if (peer) {
    peer.ontrack = null;
    peer.onconnectionstatechange = null;
    peer.oniceconnectionstatechange = null;
    try { peer.close(); } catch (e) { /* ignore */ }
  }
  clearVideoStream();
}

function clearFallbackImage() {
  fallbackEl.classList.remove("show");
  fallbackEl.removeAttribute("src");
  fallbackEl.src = "";
}

function cancelMjpeg() { mjpegProbeId += 1; clearFallbackImage(); }

function clearReconnectTimer() {
  if (reconnectTimer) { window.clearTimeout(reconnectTimer); reconnectTimer = null; }
}

function setCodecLabel(value) { codecEl.textContent = value || "-"; }
function setBitrateLabel(value) { bitrateEl.textContent = value || "-"; }
function resetWebRtcMediaStats() { lastInboundVideoSample = null; setCodecLabel("-"); setBitrateLabel("-"); }

function formatBitrate(bps) {
  if (!Number.isFinite(bps) || bps <= 0) return "-";
  if (bps >= 1000000) return (bps / 1000000).toFixed(2) + " Mbps";
  return Math.round(bps / 1000) + " kbps";
}

function stopStatePolling() {
  if (statePollTimer) { window.clearInterval(statePollTimer); statePollTimer = null; }
}

function stopCodecPolling() {
  if (codecPollTimer) { window.clearInterval(codecPollTimer); codecPollTimer = null; }
}

async function readWebRtcMediaStats(peer) {
  if (!peer || typeof peer.getStats !== "function") return { codec: "", bitrateBps: null };
  try {
    const stats = await peer.getStats();
    let inboundReport = null, codecReport = null;
    for (const report of stats.values()) {
      const kind = report.kind || report.mediaType || "";
      if (report.type === "inbound-rtp" && kind === "video") {
        inboundReport = report;
        if (report.codecId && typeof stats.get === "function") codecReport = stats.get(report.codecId) || null;
        break;
      }
    }
    if (!codecReport) {
      for (const report of stats.values()) {
        if (report.type === "codec" && /video\//i.test(report.mimeType || "")) { codecReport = report; break; }
      }
    }
    let codec = "";
    if (codecReport) {
      const mimeType = String(codecReport.mimeType || "");
      const name = mimeType.includes("/") ? mimeType.split("/").pop() : mimeType;
      codec = (name || "").toUpperCase();
    }
    let bitrateBps = null;
    if (inboundReport && Number.isFinite(inboundReport.bytesReceived)) {
      const timestampMs = typeof inboundReport.timestamp === "number"
        ? inboundReport.timestamp : Date.parse(inboundReport.timestamp || "");
      const current = { peer, bytesReceived: Number(inboundReport.bytesReceived), timestampMs: Number(timestampMs) };
      if (lastInboundVideoSample && lastInboundVideoSample.peer === peer &&
          Number.isFinite(lastInboundVideoSample.timestampMs) &&
          current.timestampMs > lastInboundVideoSample.timestampMs &&
          current.bytesReceived >= lastInboundVideoSample.bytesReceived) {
        const deltaBytes = current.bytesReceived - lastInboundVideoSample.bytesReceived;
        const deltaMs = current.timestampMs - lastInboundVideoSample.timestampMs;
        if (deltaMs > 0) bitrateBps = (deltaBytes * 8 * 1000) / deltaMs;
      }
      lastInboundVideoSample = current;
    } else {
      lastInboundVideoSample = null;
    }
    return { codec, bitrateBps };
  } catch (e) {
    return { codec: "", bitrateBps: null };
  }
}

async function refreshWebRtcMediaLabels() {
  const mode = stateStreamModeEl.dataset.mode;
  if (mode === "mjpeg") { setCodecLabel("MJPEG"); setBitrateLabel("-"); return; }
  if (mode !== "webrtc" || !activePeer || intentionalDisconnect || pausedForHidden) { resetWebRtcMediaStats(); return; }
  const peer = activePeer;
  const generation = connectGeneration;
  const media = await readWebRtcMediaStats(peer);
  if (generation !== connectGeneration || activePeer !== peer) return;
  setCodecLabel(media.codec || "WebRTC");
  setBitrateLabel(formatBitrate(media.bitrateBps));
}

function startCodecPolling() {
  stopCodecPolling();
  codecPollTimer = window.setInterval(() => { refreshWebRtcMediaLabels().catch(() => {}); }, 1500);
  refreshWebRtcMediaLabels().catch(() => {});
}

function updateStateUi(state) {
  statePresetEl.textContent = state && state.preset ? state.preset : "-";
  stateDetEl.textContent = state && state.det ? state.det : "-";
  statePoseEl.textContent = state && state.pose_enabled ? "on" : "off";
  stateFpsEl.textContent = state && Number.isFinite(state.fps) ? state.fps.toFixed(1) : "-";
  stateInferenceEl.textContent = state && state.inference_enabled ? "on" : "off";
}

async function fetchState() {
  const state = await workerFetch("/api/state", {}, { expect: "json" });
  currentCapabilities = capabilitiesFromState(state);
  updateStateUi(state);
  if (state && state.busy) {
    setBusy(true, state.busy_text || "Working...");
    if (pendingSwitch) setStatus((state.busy_text || "Switching model...") + " Please wait...");
  } else if (!pendingSwitch) {
    setBusy(false);
  }
  if (state && state.last_error) {
    setBusy(false); pendingSwitch = null;
    setStatus(state.last_error, true);
  } else if (state && state.worker_alive === false) {
    setBusy(false);
    setStatus("Worker paused or restarting...", true);
  }
  if (pendingSwitch && Number(state && state.cmd_seq_applied || 0) >= pendingSwitch.seq) {
    setBusy(false);
    setStatus("Model switched: " + (state && state.preset ? state.preset : "done"));
    pendingSwitch = null;
  }
  lastPollErrorText = "";
  return state;
}

function startStatePolling() {
  stopStatePolling();
  statePollTimer = window.setInterval(async () => {
    if (!currentTarget || pausedForHidden) return;
    try { await fetchState(); }
    catch (error) {
      if (intentionalDisconnect || pausedForHidden) return;
      const message = "Worker unavailable: " + formatError(error);
      if (message !== lastPollErrorText) { setStatus(message, true); lastPollErrorText = message; }
    }
  }, STATE_POLL_INTERVAL_MS);
}

async function refreshStateNow() {
  try { return await fetchState(); }
  catch (error) { setStatus("State fetch failed: " + formatError(error), true); return null; }
}

async function beginMjpegOnly(messageText) {
  const generation = ++connectGeneration;
  clearReconnectTimer(); closePeer(); cancelMjpeg(); hideMedia();
  setStreamMode("connecting", "Worker reports MJPEG-only streaming.");
  setPlaceholder(messageText || "Connecting to MJPEG stream...", true);
  setStatus(messageText || "Connecting to MJPEG stream...");
  const loaded = await tryMjpegStream(endpointFor("/stream.mjpg"), generation);
  if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) return;
  if (loaded) {
    showFallback(); setPlaceholder("", false);
    setStreamMode("mjpeg", "Worker is running in MJPEG-only mode.");
    setStatus("Connected using MJPEG."); setCodecLabel("MJPEG"); setBitrateLabel("-");
    return;
  }
  cancelMjpeg(); hideMedia();
  setStreamMode("error", "MJPEG stream not reachable.");
  setPlaceholder("Stream unavailable. MJPEG endpoint not reachable.", true);
  setStatus("Could not open MJPEG stream.", true);
}

function waitForIceComplete(peer) {
  return new Promise(resolve => {
    if (peer.iceGatheringState === "complete") { resolve(); return; }
    const onStateChange = () => {
      if (peer.iceGatheringState === "complete") {
        peer.removeEventListener("icegatheringstatechange", onStateChange);
        window.clearTimeout(timeoutId); resolve();
      }
    };
    const timeoutId = window.setTimeout(() => {
      peer.removeEventListener("icegatheringstatechange", onStateChange); resolve();
    }, ICE_GATHER_TIMEOUT_MS);
    peer.addEventListener("icegatheringstatechange", onStateChange);
  });
}

async function openPeerConnection(generation) {
  if (typeof RTCPeerConnection !== "function") throw createError("This browser does not support WebRTC.");
  const peer = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }, { urls: "stun:stun1.l.google.com:19302" }]
  });
  let receivedVideoTrack = false;
  activePeer = peer;
  peer.addTransceiver("video", { direction: "recvonly" });
  peer.ontrack = event => {
    if (generation !== connectGeneration || activePeer !== peer) return;
    receivedVideoTrack = true;
    const stream = event.streams && event.streams[0];
    if (stream) {
      videoEl.srcObject = stream; showVideo(); setPlaceholder("", false);
      setStreamMode("webrtc", "Live video is streaming over WebRTC.");
      setStatus("Receiving WebRTC video."); refreshWebRtcMediaLabels().catch(() => {});
    }
  };
  peer.onconnectionstatechange = () => {
    if (generation !== connectGeneration || activePeer !== peer) return;
    const state = peer.connectionState;
    if (state === "connected") { webrtcConsecutiveFailures = 0; setStatus("WebRTC connected. Waiting for video..."); return; }
    if (state === "failed" || state === "disconnected" || state === "closed") {
      if (intentionalDisconnect || pausedForHidden || document.hidden) return;
      webrtcConsecutiveFailures++;
      if (webrtcConsecutiveFailures > 1) { closePeer(); tryMjpegFallback(generation, new Error("WebRTC " + state + " after retry.")); return; }
      scheduleReconnect(new Error("WebRTC connection " + state + "."));
    }
  };
  const offer = await peer.createOffer();
  if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) throw new Error("Connection cancelled.");
  await peer.setLocalDescription(offer);
  await waitForIceComplete(peer);
  if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) throw new Error("Connection cancelled.");
  const answer = await workerFetch("/api/webrtc/offer", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
    body: JSON.stringify({ sdp: peer.localDescription.sdp, type: peer.localDescription.type })
  }, { expect: "json" });
  if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) throw new Error("Connection cancelled.");
  await peer.setRemoteDescription(answer);
  if (!receivedVideoTrack && generation === connectGeneration && activePeer === peer) {
    setStreamMode("connecting", "WebRTC negotiated. Waiting for video frames...");
    setStatus("WebRTC negotiated. Waiting for video...");
  }
}

function scheduleReconnect(error) {
  if (reconnectTimer || intentionalDisconnect || pausedForHidden || document.hidden) return;
  const detail = formatError(error);
  closePeer(); cancelMjpeg();
  setStreamMode("connecting", "Retrying WebRTC once before MJPEG fallback.");
  setPlaceholder("WebRTC disconnected. Retrying...", true);
  setStatus("WebRTC disconnected: " + detail + " Retrying...", true);
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    if (intentionalDisconnect || pausedForHidden || document.hidden) return;
    beginWebRtc(false, "Retrying WebRTC...");
  }, WEBRTC_RETRY_DELAY_MS);
}

function tryMjpegStream(url, generation) {
  const img = fallbackEl;
  const probeId = ++mjpegProbeId;
  clearFallbackImage();
  return new Promise(resolve => {
    let settled = false;
    const cleanup = () => { window.clearTimeout(timeoutId); img.removeEventListener("load", onLoad); img.removeEventListener("error", onError); };
    const finish = value => { if (settled) return; settled = true; cleanup(); resolve(value); };
    const onLoad = () => { if (probeId !== mjpegProbeId || generation !== connectGeneration) { finish(false); return; } finish(true); };
    const onError = () => finish(false);
    const timeoutId = window.setTimeout(() => finish(false), MJPEG_PROBE_TIMEOUT_MS);
    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);
    img.src = url + (url.includes("?") ? "&" : "?") + "ts=" + Date.now();
  });
}

async function tryMjpegFallback(generation, error) {
  if (currentCapabilities && currentCapabilities.mjpegAvailable === false) {
    hideMedia(); setStreamMode("error", "WebRTC failed and worker did not advertise MJPEG.");
    setPlaceholder("Stream unavailable. No MJPEG fallback.", true);
    setStatus("WebRTC failed: " + formatError(error), true); return;
  }
  const detail = formatError(error);
  setStreamMode("connecting", "Trying MJPEG fallback...");
  setPlaceholder("WebRTC unavailable. Trying MJPEG...", true);
  setStatus("WebRTC failed: " + detail + " Trying MJPEG...", true);
  const loaded = await tryMjpegStream(endpointFor("/stream.mjpg"), generation);
  if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) return;
  if (loaded) {
    showFallback(); setPlaceholder("", false);
    setStreamMode("mjpeg", "Using MJPEG fallback."); setStatus("Using MJPEG fallback.", true);
    setCodecLabel("MJPEG"); setBitrateLabel("-"); return;
  }
  cancelMjpeg(); hideMedia();
  setStreamMode("error", "WebRTC failed and MJPEG fallback unavailable.");
  setPlaceholder("Stream unavailable.", true); setStatus("WebRTC failed: " + detail, true);
}

async function beginWebRtc(allowRetry, messageText) {
  if (!currentTarget) currentTarget = resolveTargetBase();
  const generation = ++connectGeneration;
  clearReconnectTimer(); closePeer(); cancelMjpeg(); hideMedia();
  setStreamMode("connecting", "Negotiating video over WebRTC...");
  setPlaceholder(messageText || "Starting WebRTC...", true);
  setStatus(messageText || "Starting WebRTC...");
  try { await openPeerConnection(generation); }
  catch (error) {
    if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) return;
    closePeer();
    if (allowRetry && !document.hidden && shouldRetryWebRtc(error)) { scheduleReconnect(error); return; }
    if (shouldSkipMjpegFallback(error)) {
      const detail = formatError(error);
      hideMedia(); setStreamMode("error", detail); setPlaceholder("Stream unavailable. " + detail, true);
      setStatus(detail, true); return;
    }
    await tryMjpegFallback(generation, error);
  }
}

async function beginPreferredStream(allowRetry, messageText) {
  if (currentCapabilities && currentCapabilities.webrtcAvailable === false) { await beginMjpegOnly(messageText); return; }
  await beginWebRtc(allowRetry, messageText);
}

function disconnectStream(reason = "Disconnected.", isError = false) {
  intentionalDisconnect = true; pausedForHidden = false; pendingSwitch = null;
  clearReconnectTimer(); stopStatePolling(); stopCodecPolling();
  connectGeneration += 1; closePeer(); cancelMjpeg(); hideMedia();
  setBusy(false); setStreamMode(isError ? "error" : "idle", isError ? reason : "Disconnected.");
  setPlaceholder(reason, true); setStatus(reason, isError); resetWebRtcMediaStats();
}

function pauseStreamForHidden() {
  if (intentionalDisconnect || pausedForHidden) return;
  pausedForHidden = true; clearReconnectTimer(); stopStatePolling(); stopCodecPolling();
  connectGeneration += 1; closePeer(); cancelMjpeg(); hideMedia(); setBusy(false);
  setStreamMode("idle", "Paused while tab is hidden."); setPlaceholder("Paused while tab is hidden.", true);
  setStatus("Paused while tab is hidden."); resetWebRtcMediaStats();
}

async function connect() {
  let target;
  try { target = resolveTargetBase(); }
  catch (error) { disconnectStream(formatError(error), true); return; }
  currentTarget = target; intentionalDisconnect = false; pausedForHidden = false;
  pendingSwitch = null; lastPollErrorText = "";
  currentCapabilities = { webrtcAvailable: true, mjpegAvailable: true, backend: "" };
  webrtcConsecutiveFailures = 0; setBusy(false); clearReconnectTimer();
  setConnectionMode(target); startStatePolling(); startCodecPolling();
  await refreshStateNow();
  await beginPreferredStream(true, "Connecting to " + target.display + "...");
}

async function sendCmd(cmd, busyText = "") {
  try {
    if (!currentTarget) { currentTarget = resolveTargetBase(); setConnectionMode(currentTarget); }
  } catch (error) { setStatus(formatError(error), true); return; }
  const isModelSwitch = isModelSwitchCommand(cmd);
  const isQuit = isQuitCommand(cmd);
  const seq = ++cmdSeq;
  if (isModelSwitch) { pendingSwitch = { seq, cmd }; setBusy(true, busyText || "Switching model..."); setStatus((busyText || "Switching model...") + " This may take a moment."); }
  else if (isQuit) { setStatus("Stopping worker..."); }
  try {
    await workerFetch("/api/cmd", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify({ cmd, seq })
    }, { expect: "json" });
    if (isQuit) { disconnectStream("Worker stopped."); return; }
    if (!isModelSwitch) await fetchState().catch(() => {});
  } catch (error) {
    if (isQuit) { disconnectStream("Worker stop requested.", false); return; }
    if (isModelSwitch) { pendingSwitch = null; setBusy(false); }
    setStatus("Command failed: " + formatError(error), true);
  }
}

// ===== Capture Frame =====
async function captureFrame() {
  const mediaEl = videoEl.classList.contains("show") ? videoEl : fallbackEl;
  let blob;
  try {
    if (mediaEl === videoEl && videoEl.srcObject) {
      const canvas = document.createElement("canvas");
      canvas.width = videoEl.videoWidth || 640;
      canvas.height = videoEl.videoHeight || 480;
      canvas.getContext("2d").drawImage(videoEl, 0, 0);
      blob = await new Promise(res => canvas.toBlob(res, "image/jpeg", 0.92));
    } else {
      // Fetch raw frame from proxy endpoint
      const resp = await fetch("/api/proxy/frame-raw.jpg", { cache: "no-store" });
      if (!resp.ok) throw new Error("Frame fetch failed: " + resp.status);
      blob = await resp.blob();
    }
  } catch (e) {
    setStatus("Capture failed: " + e.message, true); return;
  }
  if (!blob || blob.size === 0) { setStatus("Capture failed: empty frame", true); return; }
  const csrf = getCsrf();
  const fd = new FormData();
  fd.append("type", "image");
  fd.append("overlay_file", blob, "capture.jpg");
  captureBtn.disabled = true;
  try {
    const resp = await fetch("/api/recordings/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: fd });
    if (resp.ok) {
      const data = await resp.json();
      setStatus("Frame saved (recording #" + data.id + ")");
    } else {
      const err = await resp.json().catch(() => ({}));
      setStatus("Upload failed: " + (err.error || resp.status), true);
    }
  } catch (e) {
    setStatus("Upload failed: " + e.message, true);
  } finally {
    captureBtn.disabled = false;
  }
}

// ===== Record Video =====
function toggleRecord() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    stopRecord();
  } else {
    startRecord();
  }
}

function startRecord() {
  let stream;
  if (videoEl.classList.contains("show") && videoEl.srcObject) {
    stream = videoEl.srcObject;
  } else {
    setStatus("Recording requires active WebRTC stream.", true);
    return;
  }
  recordedChunks = [];
  const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9") ? "video/webm;codecs=vp9" : "video/webm";
  try {
    mediaRecorder = new MediaRecorder(stream, { mimeType });
  } catch (e) {
    setStatus("MediaRecorder not supported: " + e.message, true); return;
  }
  mediaRecorder.ondataavailable = e => { if (e.data && e.data.size > 0) recordedChunks.push(e.data); };
  mediaRecorder.onstop = uploadRecording;
  mediaRecorder.start(1000);
  recordStartTime = Date.now();
  recordBtn.textContent = "⏹ Stop";
  recordBtn.style.background = "#991b1b";
  recordTimerEl.style.display = "inline";
  recordTimerInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - recordStartTime) / 1000);
    const m = Math.floor(secs / 60).toString().padStart(2, "0");
    const s = (secs % 60).toString().padStart(2, "0");
    recordTimerEl.textContent = m + ":" + s;
  }, 500);
}

function stopRecord() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  clearInterval(recordTimerInterval);
  recordBtn.textContent = "⏺ Record";
  recordBtn.style.background = "";
  recordTimerEl.style.display = "none";
  recordTimerEl.textContent = "";
}

async function uploadRecording() {
  if (recordedChunks.length === 0) { setStatus("No video data recorded.", true); return; }
  const duration = recordStartTime ? (Date.now() - recordStartTime) / 1000 : undefined;
  const blob = new Blob(recordedChunks, { type: "video/webm" });
  recordedChunks = [];
  const csrf = getCsrf();
  const fd = new FormData();
  fd.append("type", "video");
  fd.append("overlay_file", blob, "recording.webm");
  if (duration) fd.append("duration", duration.toFixed(2));
  recordBtn.disabled = true;
  try {
    const resp = await fetch("/api/recordings/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: fd });
    if (resp.ok) {
      const data = await resp.json();
      setStatus("Video saved (recording #" + data.id + ")");
    } else {
      const err = await resp.json().catch(() => ({}));
      setStatus("Upload failed: " + (err.error || resp.status), true);
    }
  } catch (e) {
    setStatus("Upload failed: " + e.message, true);
  } finally {
    recordBtn.disabled = false;
  }
}

// ===== Event listeners =====
videoEl.addEventListener("loadedmetadata", () => {
  if (intentionalDisconnect || pausedForHidden) return;
  showVideo(); setPlaceholder("", false);
  if (videoEl.srcObject) {
    setStreamMode("webrtc", "Live video is streaming over WebRTC.");
    setStatus("Receiving WebRTC video."); refreshWebRtcMediaLabels().catch(() => {});
  }
});

fallbackEl.addEventListener("error", () => {
  if (stateStreamModeEl.dataset.mode !== "mjpeg" || intentionalDisconnect || pausedForHidden) return;
  cancelMjpeg(); hideMedia();
  setStreamMode("error", "MJPEG fallback disconnected.");
  setPlaceholder("MJPEG fallback disconnected.", true);
  setStatus("MJPEG fallback disconnected.", true);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { pauseStreamForHidden(); return; }
  if (pausedForHidden && !intentionalDisconnect) {
    pausedForHidden = false;
    startStatePolling(); startCodecPolling();
    refreshStateNow().finally(() => {
      beginPreferredStream(true, "Resuming stream...").catch(error => { console.error("Stream resume error:", error); setStatus(formatError(error), true); });
    });
  }
});

window.addEventListener("beforeunload", () => {
  intentionalDisconnect = true; clearReconnectTimer();
  stopStatePolling(); stopCodecPolling(); connectGeneration += 1;
  closePeer(); cancelMjpeg();
});

baseInputEl.addEventListener("keydown", event => {
  if (event.key === "Enter") connect().catch(error => setStatus(formatError(error), true));
});

// ===== Init =====
if (IS_FILE_PROTOCOL) {
  if (!baseInputEl.value.trim() || baseInputEl.value.trim() === "/") baseInputEl.value = DEFAULT_DIRECT_BASE;
  baseInputEl.placeholder = DEFAULT_DIRECT_BASE;
} else {
  if (!baseInputEl.value.trim()) baseInputEl.value = "/";
  baseInputEl.placeholder = "/ or " + DEFAULT_DIRECT_BASE;
}

setConnectionMode(IS_FILE_PROTOCOL ? { kind: "direct", display: DEFAULT_DIRECT_BASE } : { kind: "proxy" });
setStreamMode("idle", "WebRTC first. MJPEG fallback when worker does not expose WebRTC.");
resetWebRtcMediaStats();
setPlaceholder(IS_FILE_PROTOCOL ? "Connecting to local worker..." : "Connecting...", true);
connect().catch(error => setStatus(formatError(error), true));
