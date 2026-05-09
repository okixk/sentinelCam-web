/* stream.js – WebRTC/MJPEG streaming + capture/record */

const DEFAULT_DIRECT_BASE = "http://127.0.0.1:8080";
const STATE_POLL_FAST_MS = 350;
const STATE_POLL_INTERVAL_MS = 1000;
const STATE_POLL_CONNECTED_MS = 2000;
const STATE_POLL_MAX_INTERVAL_MS = 8000;
const ICE_GATHER_TIMEOUT_MS = 4000;
const WEBRTC_RETRY_DELAY_MS = 2000;
const MJPEG_PROBE_TIMEOUT_MS = 5000;
const RAW_RECORD_SETUP_TIMEOUT_MS = 4000;
const RAW_RECORD_FPS_FALLBACK = 15;
const ALERT_NOTIFY_ENABLED_KEY = "sentinelcam.alerts.notify.enabled";
const ALERT_NOTIFY_COOLDOWN_KEY = "sentinelcam.alerts.notify.cooldown";
const PRE_EVENT_ENABLED_KEY = "sentinelcam.alerts.pre_event.enabled";
const PRE_EVENT_SECONDS_KEY = "sentinelcam.alerts.pre_event.seconds";
const CAPTURE_DESCRIPTIONS_KEY = "sentinelcam.capture.descriptions.enabled";
const STREAM_CONNECTION_MODE_KEY = "sentinelcam.stream.connection.mode";
const STREAM_DIRECT_BASE_KEY = "sentinelcam.stream.direct_base";
const CONTEXT_TRIGGER_KEY = "sentinelcam.context.trigger";
const CONTEXT_COOLDOWN_KEY = "sentinelcam.context.cooldown";
const CONTEXT_PROFILE_KEY = "sentinelcam.context.profile";
const CONTEXT_MODEL_KEY = "sentinelcam.context.model";
const LEGACY_CONTEXT_NOTIFY_ENABLED_KEY = "sentinelcam.context.notify.enabled";
const LEGACY_CONTEXT_NOTIFY_COOLDOWN_KEY = "sentinelcam.context.notify.cooldown";
const MODEL_SWITCH_COMMANDS = new Set(["m", "n", "next", "prev", "previous"]);
const QUIT_COMMANDS = new Set(["q", "quit", "exit", "stop"]);
const IS_FILE_PROTOCOL = window.location.protocol === "file:";
const PAGE_ORIGIN = window.location.origin || "";
const PAGE_PROTOCOL = window.location.protocol || "";

let statePollTimer = null;
let statePollToken = 0;
let statePollFailureCount = 0;
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
let rememberedDirectBase = DEFAULT_DIRECT_BASE;
let lastConnectionError = "";
let lastConnectionStatus = "Preparing connection checks.";

// Recording state
let mediaRecorder = null;
let recordedChunks = [];
let rawMediaRecorder = null;
let rawRecordedChunks = [];
let rawRecorderCleanup = null;
let recordMimeType = "video/webm";
let recordStartTime = null;
let recordTimerInterval = null;
let recordStopPromise = null;
let lastContextUpdatedAt = 0;
let lastAlertNotificationAt = 0;
let contextControlsHydrated = false;
let lastPreEventUploadAt = 0;
let preEventRecorder = null;
let preEventChunks = [];
let preEventMimeType = "";
let lastWorkerState = null;

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
const stateDetectMsEl = document.getElementById("stateDetectMs");
const statePoseMsEl = document.getElementById("statePoseMs");
const stateContextEnabledEl = document.getElementById("stateContextEnabled");
const stateContextTriggerEl = document.getElementById("stateContextTrigger");
const stateContextProfileEl = document.getElementById("stateContextProfile");
const stateContextModelEl = document.getElementById("stateContextModel");
const contextSummaryEl = document.getElementById("contextSummary");
const contextTriggerEl = document.getElementById("context-trigger");
const contextCooldownEl = document.getElementById("context-cooldown");
const contextModelEl = document.getElementById("context-model");
const contextProfileEl = document.getElementById("context-profile");
const notificationCooldownEl = document.getElementById("notification-cooldown");
const notificationToggleEl = document.getElementById("notificationToggle");
const preEventEnabledEl = document.getElementById("pre-event-enabled");
const preEventSecondsEl = document.getElementById("pre-event-seconds");
const captureDescriptionsEl = document.getElementById("capture-descriptions");
const captureBtn = document.getElementById("capture-btn");
const recordBtn = document.getElementById("record-btn");
const fullscreenBtn = document.getElementById("fullscreen-btn");
const recordTimerEl = document.getElementById("record-timer");
const workerHealthBadgeEl = document.getElementById("workerHealthBadge");
const workerHealthTextEl = document.getElementById("workerHealthText");
const connectionModeEl = document.getElementById("connection-mode");
const resolvedTargetEl = document.getElementById("resolvedTarget");
const troubleSummaryEl = document.getElementById("troubleSummary");
const techPageOriginEl = document.getElementById("techPageOrigin");
const techTargetEl = document.getElementById("techTarget");
const techBackendEl = document.getElementById("techBackend");
const techWebRtcEl = document.getElementById("techWebRtc");
const techMjpegEl = document.getElementById("techMjpeg");
const techLastStatusEl = document.getElementById("techLastStatus");
const techLastErrorEl = document.getElementById("techLastError");

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

function setText(el, value) {
  if (el) el.textContent = value;
}

function setTroubleStep(name, state, detail) {
  const card = document.getElementById("trouble-step-" + name);
  const detailEl = document.getElementById("trouble-" + name + "Detail");
  if (card) card.dataset.state = state || "pending";
  if (detailEl) detailEl.textContent = detail || "";
}

function refreshTechnicalDetails() {
  const previewTarget = resolvedTargetEl ? resolvedTargetEl.textContent.trim() : "";
  setText(techPageOriginEl, PAGE_ORIGIN || (IS_FILE_PROTOCOL ? "file://" : PAGE_PROTOCOL || "unknown"));
  setText(techTargetEl, currentTarget ? currentTarget.display : (previewTarget || getBaseInputValue() || "/"));
  setText(techBackendEl, currentCapabilities.backend || (stateStreamModeEl ? stateStreamModeEl.textContent : "-"));
  setText(techWebRtcEl, currentCapabilities.webrtcAvailable === false ? "Unavailable" : "Available");
  setText(techMjpegEl, currentCapabilities.mjpegAvailable === false ? "Unavailable" : "Available");
  setText(techLastStatusEl, lastConnectionStatus || "-");
  setText(techLastErrorEl, lastConnectionError || "None");
}

function setConnectionSummary(text, tone = "neutral") {
  lastConnectionStatus = text || "";
  if (troubleSummaryEl) {
    troubleSummaryEl.textContent = text || "";
    troubleSummaryEl.className = tone === "error" ? "small error" : tone === "warn" ? "small warn" : "small";
  }
  refreshTechnicalDetails();
}

function rememberConnectionError(error) {
  lastConnectionError = formatError(error);
  refreshTechnicalDetails();
}

function clearConnectionError() {
  lastConnectionError = "";
  refreshTechnicalDetails();
}

function updateBaseInputUi() {
  if (!baseInputEl) return;
  const directSelected = connectionModeEl ? connectionModeEl.value === "direct" : (baseInputEl.value.trim() !== "/");
  if (directSelected) {
    baseInputEl.readOnly = false;
    if (!baseInputEl.value.trim() || baseInputEl.value.trim() === "/") {
      baseInputEl.value = rememberedDirectBase || DEFAULT_DIRECT_BASE;
    }
    baseInputEl.placeholder = DEFAULT_DIRECT_BASE;
  } else {
    const currentValue = baseInputEl.value.trim();
    if (currentValue && currentValue !== "/") {
      rememberedDirectBase = currentValue;
      window.localStorage.setItem(STREAM_DIRECT_BASE_KEY, rememberedDirectBase);
    }
    baseInputEl.readOnly = true;
    baseInputEl.value = "/";
    baseInputEl.placeholder = "/";
  }
}

function stageReconnectPreview(target) {
  intentionalDisconnect = true;
  pausedForHidden = false;
  pendingSwitch = null;
  clearReconnectTimer();
  stopStatePolling();
  stopCodecPolling();
  connectGeneration += 1;
  closePeer();
  cancelMjpeg();
  hideMedia();
  currentTarget = null;
  setBusy(false);
  resetWebRtcMediaStats();
  setWorkerHealth("neutral", "Reconnect to test new target");
  setStreamMode("idle", "Connection settings changed. Click Reconnect.");
  setPlaceholder("Click Reconnect to test " + target.display + ".", true);
  setTroubleStep(
    "state",
    "pending",
    target.kind === "proxy"
      ? "Click Reconnect to test proxy."
      : "Click Reconnect to test direct mode."
  );
  setTroubleStep("webrtc", "pending", "WebRTC retries after reconnect.");
  setTroubleStep("video", "pending", "Fallback retries after reconnect.");
  setConnectionSummary(
    target.kind === "proxy"
      ? "Proxy selected. Click Reconnect."
      : "Direct selected. Click Reconnect.",
    target.kind === "proxy" ? "neutral" : "warn"
  );
}

function updateTargetPreview() {
  try {
    const previewTarget = resolveTargetBase();
    if (previewTarget.kind === "direct") rememberedDirectBase = previewTarget.display;
    setText(resolvedTargetEl, previewTarget.display);
    setTroubleStep(
      "route",
      "ok",
      previewTarget.kind === "proxy"
        ? "Browser -> web app proxy -> worker"
        : "Browser -> worker directly at " + previewTarget.display
    );
    if (
      currentTarget &&
      (currentTarget.kind !== previewTarget.kind || currentTarget.display !== previewTarget.display)
    ) {
      stageReconnectPreview(previewTarget);
    } else {
      refreshTechnicalDetails();
    }
  } catch (error) {
    setText(resolvedTargetEl, "Invalid target");
    setTroubleStep("route", "error", formatError(error));
    setTroubleStep("state", "error", "Enter a valid proxy path or full http(s) URL.");
    setConnectionSummary(formatError(error), "error");
    rememberConnectionError(error);
  }
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

function getBaseInputValue() {
  if (!baseInputEl) {
    return IS_FILE_PROTOCOL ? DEFAULT_DIRECT_BASE : "/";
  }
  return baseInputEl.value.trim();
}

function normalizeBaseUrl(url) {
  return url.href.replace(/\/+$/, "");
}

function extractServerErrorDetails(text) {
  const trimmed = (text || "").trim();
  const details = { message: "", upstream: "" };
  if (!trimmed) return details;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.error === "string" && parsed.error.trim()) {
        details.message = parsed.error.trim();
      }
      if (typeof parsed.upstream === "string" && parsed.upstream.trim()) {
        details.upstream = parsed.upstream.trim();
      }
      if (details.message || details.upstream) {
        return details;
      }
    }
  } catch (e) { /* fall through */ }
  details.message = trimmed.replace(/\s+/g, " ");
  return details;
}

function describeWorkerHttpError(status, serverError, url, target) {
  const serverMessage = serverError.message || "";
  const workerUrl = serverError.upstream || url;
  if (status === 401) {
    return "Worker auth is enabled at " + workerUrl + ". Use proxy mode instead.";
  }
  if (status === 404 && /\/api\/webrtc\/offer(?:\?|$)/i.test(url)) {
    return "Worker does not expose WebRTC at " + workerUrl + ". Start worker with WebRTC enabled or use MJPEG.";
  }
  if (status === 403 && /origin not allowed/i.test(serverMessage)) {
    return target.kind === "proxy"
      ? "Worker rejected proxy origin for " + workerUrl + ". Check WEB_ALLOWED_ORIGINS on the worker."
      : "Worker rejected browser origin. Use proxy mode or add origin to WEB_ALLOWED_ORIGINS.";
  }
  if (status === 403) {
    return "Worker rejected " + workerUrl + (serverMessage ? ": " + serverMessage : ".");
  }
  if (target.kind === "proxy" && status === 502) {
    return "Proxy could not reach worker at " + workerUrl + ". Start the worker. If the web app runs in Docker, use WORKER_BASE_URL=http://host.docker.internal:8080. On Linux, the worker must listen on 0.0.0.0 or another non-loopback interface.";
  }
  return "HTTP " + status + " from " + workerUrl + (serverMessage ? ": " + serverMessage : "");
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
  const raw = getBaseInputValue();
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

function rawFrameEndpointFor(target = currentTarget || resolveTargetBase()) {
  return endpointFor(target.kind === "proxy" ? "/api/proxy/frame-raw.jpg" : "/frame-raw.jpg", target);
}

function rawMjpegEndpointFor(target = currentTarget || resolveTargetBase()) {
  return endpointFor("/stream-raw.mjpg", target);
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
    const serverError = extractServerErrorDetails(bodyText);
    throw createError(describeWorkerHttpError(response.status, serverError, url, target), {
      url, status: response.status, target, bodyText, serverError,
      upstreamUrl: serverError.upstream || "",
      isAuthError: response.status === 401 || response.status === 403,
      isOriginError: response.status === 403 && /origin not allowed/i.test(serverError.message || "")
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

function setWorkerHealth(state, text) {
  if (workerHealthBadgeEl) workerHealthBadgeEl.dataset.state = state;
  if (workerHealthTextEl) workerHealthTextEl.textContent = text;
}

function setBusy(on, title = "Switching model...") {
  busyTitleEl.textContent = title;
  busyOverlayEl.classList.toggle("show", !!on);
  if (btnPrevEl) btnPrevEl.disabled = !!on;
  if (btnNextEl) btnNextEl.disabled = !!on;
}

function setPlaceholder(text = "Not connected.", show = true) {
  placeholderEl.textContent = text;
  placeholderEl.classList.toggle("hidden", !show);
}

function setConnectionMode(target) {
  if (stateConnectionEl) {
    stateConnectionEl.textContent = target.kind === "proxy" ? "Proxy" : "Direct";
  }
  if (connectionModeEl) {
    connectionModeEl.value = target.kind === "proxy" ? "proxy" : "direct";
  }
  if (target.kind === "direct") {
    rememberedDirectBase = target.display;
  }
  updateBaseInputUi();
  setText(resolvedTargetEl, target.display);
  if (connectionNoteEl) {
    if (target.kind === "proxy") {
      connectionNoteEl.textContent = "Web app proxy is active.";
      connectionNoteEl.className = "small";
    } else {
      connectionNoteEl.textContent = "Browser connects to the worker directly.";
      connectionNoteEl.className = "small warn";
    }
  }
  setTroubleStep(
    "route",
    "ok",
    target.kind === "proxy"
      ? "Browser -> web app -> worker"
      : "Browser -> worker"
  );
  refreshTechnicalDetails();
}

function setStreamMode(mode, detail = "") {
  const labelMap = { idle: "Idle", connecting: "Connecting", webrtc: "WebRTC", mjpeg: "MJPEG fallback", error: "Error" };
  if (stateStreamModeEl) {
    stateStreamModeEl.textContent = labelMap[mode] || "Idle";
    stateStreamModeEl.dataset.mode = mode;
  }
  if (streamModeNoteEl) {
    streamModeNoteEl.textContent = detail;
    streamModeNoteEl.className = mode === "error" ? "small error" : mode === "mjpeg" ? "small warn" : "small";
  }
}

function hideMedia() {
  videoEl.classList.remove("show");
  fallbackEl.classList.remove("show");
}

function showVideo() { hideMedia(); videoEl.classList.add("show"); }
function showFallback() { hideMedia(); fallbackEl.classList.add("show"); }

function clearVideoStream() {
  stopPreEventRecorder();
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
  if (statePollTimer) { window.clearTimeout(statePollTimer); statePollTimer = null; }
  statePollToken += 1;
  statePollFailureCount = 0;
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

function contextTriggerLabel(value) {
  const labels = {
    interval: "Every interval",
    person_appears: "Person appears",
    person_present: "Person present",
    manual: "Manual only"
  };
  return labels[String(value || "")] || String(value || "-");
}

function migrateAlertNotificationSettings() {
  if (window.localStorage.getItem(ALERT_NOTIFY_ENABLED_KEY) === null) {
    const oldEnabled = window.localStorage.getItem(LEGACY_CONTEXT_NOTIFY_ENABLED_KEY);
    if (oldEnabled !== null) window.localStorage.setItem(ALERT_NOTIFY_ENABLED_KEY, oldEnabled);
  }
  if (window.localStorage.getItem(ALERT_NOTIFY_COOLDOWN_KEY) === null) {
    const oldCooldown = window.localStorage.getItem(LEGACY_CONTEXT_NOTIFY_COOLDOWN_KEY);
    if (oldCooldown !== null) window.localStorage.setItem(ALERT_NOTIFY_COOLDOWN_KEY, oldCooldown);
  }
}

function getNotificationCooldownSeconds() {
  const raw = parseInt(notificationCooldownEl?.value || window.localStorage.getItem(ALERT_NOTIFY_COOLDOWN_KEY) || "60", 10);
  return Number.isFinite(raw) ? Math.max(5, raw) : 60;
}

function notificationsEnabled() {
  return window.localStorage.getItem(ALERT_NOTIFY_ENABLED_KEY) === "1";
}

function preEventEnabled() {
  return window.localStorage.getItem(PRE_EVENT_ENABLED_KEY) === "1";
}

function preEventSeconds() {
  const raw = parseInt(preEventSecondsEl?.value || window.localStorage.getItem(PRE_EVENT_SECONDS_KEY) || "30", 10);
  return Number.isFinite(raw) ? Math.max(5, Math.min(120, raw)) : 30;
}

function captureDescriptionsEnabled() {
  return window.localStorage.getItem(CAPTURE_DESCRIPTIONS_KEY) === "1";
}

function syncLocalAlertSettings() {
  if (preEventEnabledEl) preEventEnabledEl.value = preEventEnabled() ? "1" : "0";
  if (preEventSecondsEl) preEventSecondsEl.value = window.localStorage.getItem(PRE_EVENT_SECONDS_KEY) || "30";
  if (captureDescriptionsEl) captureDescriptionsEl.value = captureDescriptionsEnabled() ? "1" : "0";
}

function refreshNotificationButton() {
  if (!notificationToggleEl) return;
  const supported = "Notification" in window;
  if (!supported) {
    notificationToggleEl.textContent = "Notifications unavailable";
    notificationToggleEl.disabled = true;
    return;
  }
  const enabled = notificationsEnabled() && Notification.permission === "granted";
  notificationToggleEl.textContent = enabled ? "Alerts on" : "Alerts off";
  notificationToggleEl.classList.toggle("secondary", !enabled);
}

async function toggleNotifications() {
  if (!("Notification" in window)) {
    setStatus("Browser notifications are not supported here.", true);
    return;
  }
  if (notificationsEnabled()) {
    window.localStorage.setItem(ALERT_NOTIFY_ENABLED_KEY, "0");
    refreshNotificationButton();
    setStatus("Camera alerts disabled.");
    return;
  }
  const permission = Notification.permission === "granted"
    ? "granted"
    : await Notification.requestPermission();
  if (permission === "granted") {
    window.localStorage.setItem(ALERT_NOTIFY_ENABLED_KEY, "1");
    setStatus("Camera alerts enabled.");
  } else {
    window.localStorage.setItem(ALERT_NOTIFY_ENABLED_KEY, "0");
    setStatus("Notification permission was not granted.", true);
  }
  refreshNotificationButton();
}

function contextHasPerson(context) {
  const objects = Array.isArray(context?.objects) ? context.objects : [];
  return objects.some(obj => String(obj?.label || "").toLowerCase() === "person");
}

function buildCameraAlert(context) {
  const summary = String(context?.summary || "").trim();
  const personVisible = contextHasPerson(context) || /\b(person|someone|people|man|woman)\b/i.test(summary);
  const title = personVisible ? "Someone is at the camera" : "Camera alert";
  let body = personVisible
    ? "Review the live feed: expected person or possible risk?"
    : "Review the live feed for a new camera event.";
  if (summary) body += " " + summary;
  return { title, body: body.slice(0, 220) };
}

function maybeNotifyCameraAlert(context) {
  if (!context || !context.summary) return;
  const updatedAt = Number(context.updated_at || 0);
  if (!Number.isFinite(updatedAt) || updatedAt <= 0 || updatedAt === lastContextUpdatedAt) return;
  lastContextUpdatedAt = updatedAt;
  if (contextHasPerson(context)) triggerPreEventUpload(context).catch(() => {});
  if (!notificationsEnabled() || !("Notification" in window) || Notification.permission !== "granted") return;
  const now = Date.now() / 1000;
  if (now - lastAlertNotificationAt < getNotificationCooldownSeconds()) return;
  lastAlertNotificationAt = now;
  const alert = buildCameraAlert(context);
  try {
    new Notification(alert.title, {
      body: alert.body,
      tag: "sentinelcam-camera-alert",
      renotify: false,
      silent: false
    });
  } catch (_) {
    // Browser notification failures should not interrupt stream polling.
  }
}

function hydrateContextControls(state) {
  if (!state || contextControlsHydrated) return;
  if (contextTriggerEl) contextTriggerEl.value = window.localStorage.getItem(CONTEXT_TRIGGER_KEY) || state.context_trigger || "person_appears";
  if (contextCooldownEl) {
    const savedCooldown = window.localStorage.getItem(CONTEXT_COOLDOWN_KEY);
    contextCooldownEl.value = savedCooldown || (Number.isFinite(Number(state.context_cooldown)) ? String(Math.round(Number(state.context_cooldown))) : "60");
  }
  if (contextProfileEl) contextProfileEl.value = window.localStorage.getItem(CONTEXT_PROFILE_KEY) || "auto";
  if (notificationCooldownEl) {
    notificationCooldownEl.value = window.localStorage.getItem(ALERT_NOTIFY_COOLDOWN_KEY) || notificationCooldownEl.value || "60";
  }
  syncLocalAlertSettings();
  contextControlsHydrated = true;
}

function updateContextModelOptions(state) {
  if (!contextModelEl) return;
  const current = contextModelEl.value || "auto";
  const stateModel = state && state.context_model ? String(state.context_model) : "";
  const selected = contextControlsHydrated ? current : (window.localStorage.getItem(CONTEXT_MODEL_KEY) || "auto");
  const models = Array.isArray(state?.context_models) ? state.context_models.map(String).filter(Boolean) : [];
  const values = ["auto", ...models];
  if (stateModel && !values.includes(stateModel)) values.push(stateModel);
  if (selected && !values.includes(selected)) values.push(selected);
  contextModelEl.innerHTML = "";
  values.forEach(value => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value === "auto" ? "Auto" : value;
    contextModelEl.appendChild(option);
  });
  contextModelEl.value = values.includes(selected) ? selected : "auto";
}

function updateStateUi(state) {
  lastWorkerState = state || null;
  statePresetEl.textContent = state && state.preset ? state.preset : "-";
  stateDetEl.textContent = state && state.det ? state.det : "-";
  statePoseEl.textContent = state && state.pose_enabled ? "on" : "off";
  stateFpsEl.textContent = state && Number.isFinite(state.fps) ? state.fps.toFixed(1) : "-";
  stateInferenceEl.textContent = state && state.inference_enabled ? "on" : "off";
  setText(stateDetectMsEl, state?.perf && Number.isFinite(Number(state.perf.detect_ms)) ? Math.round(Number(state.perf.detect_ms)) + " ms" : "-");
  setText(statePoseMsEl, state?.perf && Number.isFinite(Number(state.perf.pose_ms)) ? Math.round(Number(state.perf.pose_ms)) + " ms" : "-");
  const context = state && state.context ? state.context : {};
  setText(stateContextEnabledEl, state && state.context_enabled ? "on" : "off");
  setText(stateContextTriggerEl, contextTriggerLabel(state && state.context_trigger));
  setText(stateContextProfileEl, state && state.context_profile ? state.context_profile : "-");
  setText(stateContextModelEl, state && state.context_model ? state.context_model : "-");
  updateContextModelOptions(state);
  if (contextSummaryEl) {
    if (context.summary) {
      const latency = Number.isFinite(Number(context.latency_ms)) ? " (" + Math.round(Number(context.latency_ms)) + " ms)" : "";
      contextSummaryEl.textContent = context.summary + latency;
      contextSummaryEl.className = "small";
    } else if (context.error) {
      contextSummaryEl.textContent = "Analysis error: " + context.error;
      contextSummaryEl.className = "small error";
    } else {
      contextSummaryEl.textContent = state && state.context_enabled ? "Waiting for trigger..." : "Camera analysis is off.";
      contextSummaryEl.className = "small";
    }
  }
  hydrateContextControls(state);
  maybeNotifyCameraAlert(context);
}

async function fetchState() {
  return fetchStateWithGuard({});
}

async function fetchStateWithGuard(options = {}) {
  const pollToken = Number.isFinite(options.pollToken) ? options.pollToken : null;
  const targetSnapshot = typeof options.targetSnapshot === "string" ? options.targetSnapshot : "";
  const state = await workerFetch("/api/state", {}, { expect: "json" });
  if (pollToken !== null && pollToken !== statePollToken) return null;
  if (
    targetSnapshot &&
    (!currentTarget || (currentTarget.kind + "|" + currentTarget.display) !== targetSnapshot)
  ) {
    return null;
  }
  currentCapabilities = capabilitiesFromState(state);
  updateStateUi(state);
  clearConnectionError();
  setTroubleStep("state", "ok", "State API reachable.");
  if (currentCapabilities.webrtcAvailable === false) {
    setTroubleStep("webrtc", "warn", "Worker reports MJPEG-only streaming.");
  } else if ((stateStreamModeEl && stateStreamModeEl.dataset.mode) !== "webrtc") {
    setTroubleStep("webrtc", "pending", "Worker can accept WebRTC offers.");
  }
  if (state && state.busy) {
    setBusy(true, state.busy_text || "Working...");
    if (pendingSwitch) setStatus((state.busy_text || "Switching model...") + " Please wait...");
  } else if (!pendingSwitch) {
    setBusy(false);
  }
  if (state && state.last_error) {
    setBusy(false); pendingSwitch = null;
    setWorkerHealth("warning", "Worker reported an issue");
    setTroubleStep("state", "warn", "Worker reported: " + state.last_error);
    rememberConnectionError(state.last_error);
    setConnectionSummary(state.last_error, "warn");
    setStatus(state.last_error, true);
  } else if (state && state.worker_alive === false) {
    setBusy(false);
    setWorkerHealth("warning", "Worker is restarting");
    setTroubleStep("state", "warn", "Worker paused or restarting.");
    setConnectionSummary("Worker paused or restarting.", "warn");
    setStatus("Worker paused or restarting...", true);
  } else {
    setWorkerHealth(
      currentCapabilities.webrtcAvailable === false ? "warning" : "online",
      currentCapabilities.webrtcAvailable === false ? "Worker online (MJPEG only)" : "Worker online"
    );
    if ((stateStreamModeEl && stateStreamModeEl.dataset.mode) === "mjpeg") {
      setConnectionSummary("Worker reachable. Stream is running with MJPEG fallback.", "warn");
    } else if ((stateStreamModeEl && stateStreamModeEl.dataset.mode) === "webrtc") {
      setConnectionSummary("Worker reachable. Live video is flowing over WebRTC.");
    } else {
      setConnectionSummary("Worker reachable. Preparing the best available video path.");
    }
  }
  if (pendingSwitch && Number(state && state.cmd_seq_applied || 0) >= pendingSwitch.seq) {
    setBusy(false);
    setStatus("Model switched: " + (state && state.preset ? state.preset : "done"));
    pendingSwitch = null;
  }
  lastPollErrorText = "";
  refreshTechnicalDetails();
  return state;
}

function nextStatePollDelay(state = null) {
  if (statePollFailureCount > 0) {
    const scaled = STATE_POLL_INTERVAL_MS * Math.pow(2, statePollFailureCount);
    return Math.min(scaled, STATE_POLL_MAX_INTERVAL_MS);
  }
  if (pendingSwitch || (state && state.busy)) return STATE_POLL_FAST_MS;
  const mode = stateStreamModeEl ? stateStreamModeEl.dataset.mode : "";
  if (
    state &&
    !state.last_error &&
    state.worker_alive !== false &&
    (mode === "webrtc" || mode === "mjpeg")
  ) {
    return STATE_POLL_CONNECTED_MS;
  }
  return STATE_POLL_INTERVAL_MS;
}

function scheduleNextStatePoll(delayMs, pollToken) {
  if (pollToken !== statePollToken) return;
  if (statePollTimer) window.clearTimeout(statePollTimer);
  statePollTimer = window.setTimeout(async () => {
    if (
      pollToken !== statePollToken ||
      !currentTarget ||
      pausedForHidden ||
      intentionalDisconnect
    ) {
      return;
    }
    const targetSnapshot = currentTarget.kind + "|" + currentTarget.display;
    let state = null;
    try {
      state = await fetchStateWithGuard({ pollToken, targetSnapshot });
      if (pollToken !== statePollToken) return;
      if (state !== null) {
        statePollFailureCount = 0;
      }
    } catch (error) {
      if (intentionalDisconnect || pausedForHidden || pollToken !== statePollToken) return;
      statePollFailureCount += 1;
      const message = "Worker unavailable: " + formatError(error);
      setWorkerHealth("error", "Worker unavailable");
      setTroubleStep("state", "error", formatError(error));
      setConnectionSummary("State API is not reachable. " + formatError(error), "error");
      rememberConnectionError(error);
      if (message !== lastPollErrorText) { setStatus(message, true); lastPollErrorText = message; }
    } finally {
      if (
        pollToken === statePollToken &&
        currentTarget &&
        !pausedForHidden &&
        !intentionalDisconnect
      ) {
        scheduleNextStatePoll(nextStatePollDelay(state), pollToken);
      }
    }
  }, delayMs);
}

function startStatePolling() {
  stopStatePolling();
  const pollToken = statePollToken;
  scheduleNextStatePoll(STATE_POLL_INTERVAL_MS, pollToken);
}

async function refreshStateNow() {
  try {
    const state = await fetchState();
    statePollFailureCount = 0;
    return state;
  }
  catch (error) {
    if (intentionalDisconnect || pausedForHidden) return null;
    setWorkerHealth("error", "Worker unavailable");
    setTroubleStep("state", "error", formatError(error));
    setConnectionSummary("State fetch failed. " + formatError(error), "error");
    rememberConnectionError(error);
    setStatus("State fetch failed: " + formatError(error), true);
    return null;
  }
}

async function beginMjpegOnly(messageText) {
  const generation = ++connectGeneration;
  clearReconnectTimer(); closePeer(); cancelMjpeg(); hideMedia();
  setWorkerHealth("connecting", "Connecting with MJPEG");
  setStreamMode("connecting", "Worker reports MJPEG-only streaming.");
  setTroubleStep("webrtc", "warn", "Worker reported MJPEG-only mode.");
  setTroubleStep("video", "pending", "Trying MJPEG stream...");
  setConnectionSummary(messageText || "Connecting with MJPEG because WebRTC is unavailable.", "warn");
  setPlaceholder(messageText || "Connecting to MJPEG stream...", true);
  setStatus(messageText || "Connecting to MJPEG stream...");
  const loaded = await tryMjpegStream(endpointFor("/stream.mjpg"), generation);
  if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) return;
  if (loaded) {
    showFallback(); setPlaceholder("", false);
    setWorkerHealth("warning", "Worker online (MJPEG only)");
    setStreamMode("mjpeg", "Worker is running in MJPEG-only mode.");
    clearConnectionError();
    setTroubleStep("video", "warn", "MJPEG stream is live.");
    setConnectionSummary("Connected using MJPEG fallback.", "warn");
    setStatus("Connected using MJPEG."); setCodecLabel("MJPEG"); setBitrateLabel("-");
    return;
  }
  cancelMjpeg(); hideMedia();
  setWorkerHealth("error", "MJPEG stream unavailable");
  setStreamMode("error", "MJPEG stream not reachable.");
  setTroubleStep("video", "error", "MJPEG endpoint not reachable.");
  setConnectionSummary("Could not open MJPEG stream.", "error");
  rememberConnectionError("MJPEG stream not reachable.");
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
  setTroubleStep("webrtc", "pending", "Negotiating WebRTC offer...");
  setTroubleStep("video", "pending", "Waiting for the first video frame...");
  setConnectionSummary("Negotiating WebRTC with the worker...");
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
      setWorkerHealth("online", "Receiving WebRTC video");
      setStreamMode("webrtc", "Live video is streaming over WebRTC.");
      clearConnectionError();
      setTroubleStep("webrtc", "ok", "WebRTC negotiated successfully.");
      setTroubleStep("video", "ok", "Receiving live WebRTC frames.");
      setConnectionSummary("Receiving live video over WebRTC.");
      setStatus("Receiving WebRTC video."); refreshWebRtcMediaLabels().catch(() => {});
    }
  };
  peer.onconnectionstatechange = () => {
    if (generation !== connectGeneration || activePeer !== peer) return;
    const state = peer.connectionState;
    if (state === "connected") {
      webrtcConsecutiveFailures = 0;
      setWorkerHealth("online", "Worker online");
      setTroubleStep("webrtc", "ok", "WebRTC connected. Waiting for video frames.");
      setConnectionSummary("WebRTC connected. Waiting for video...");
      setStatus("WebRTC connected. Waiting for video...");
      return;
    }
    if (state === "failed" || state === "disconnected" || state === "closed") {
      if (intentionalDisconnect || pausedForHidden || document.hidden) return;
      webrtcConsecutiveFailures++;
      setWorkerHealth("warning", "WebRTC interrupted");
      setTroubleStep("webrtc", "warn", "WebRTC " + state + ". Retrying once before fallback.");
      setTroubleStep("video", "pending", "Waiting for reconnect or MJPEG fallback...");
      rememberConnectionError("WebRTC " + state + ".");
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
    setWorkerHealth("connecting", "Waiting for video");
    setStreamMode("connecting", "WebRTC negotiated. Waiting for video frames...");
    setTroubleStep("webrtc", "ok", "WebRTC negotiated. Waiting for video frames.");
    setTroubleStep("video", "pending", "Transport connected. Waiting for frames...");
    setConnectionSummary("WebRTC negotiated. Waiting for live video...");
    setStatus("WebRTC negotiated. Waiting for video...");
  }
}

function scheduleReconnect(error) {
  if (reconnectTimer || intentionalDisconnect || pausedForHidden || document.hidden) return;
  const detail = formatError(error);
  closePeer(); cancelMjpeg();
  setWorkerHealth("connecting", "Retrying WebRTC");
  setStreamMode("connecting", "Retrying WebRTC once before MJPEG fallback.");
  setTroubleStep("webrtc", "warn", "WebRTC disconnected. Retrying once.");
  setTroubleStep("video", "pending", "Waiting for reconnect...");
  setConnectionSummary("WebRTC disconnected. Retrying once before MJPEG fallback.", "warn");
  rememberConnectionError(error);
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
    setWorkerHealth("error", "No MJPEG fallback");
    setTroubleStep("webrtc", "warn", "WebRTC failed: " + formatError(error));
    setTroubleStep("video", "error", "Worker did not advertise MJPEG fallback.");
    setConnectionSummary("Stream unavailable. No MJPEG fallback was advertised.", "error");
    rememberConnectionError(error);
    setPlaceholder("Stream unavailable. No MJPEG fallback.", true);
    setStatus("WebRTC failed: " + formatError(error), true); return;
  }
  const detail = formatError(error);
  setWorkerHealth("connecting", "Trying MJPEG fallback");
  setStreamMode("connecting", "Trying MJPEG fallback...");
  setTroubleStep("webrtc", "warn", "WebRTC failed: " + detail);
  setTroubleStep("video", "pending", "Trying MJPEG fallback...");
  setConnectionSummary("WebRTC failed. Trying MJPEG fallback...", "warn");
  rememberConnectionError(error);
  setPlaceholder("WebRTC unavailable. Trying MJPEG...", true);
  setStatus("WebRTC failed: " + detail + " Trying MJPEG...", true);
  const loaded = await tryMjpegStream(endpointFor("/stream.mjpg"), generation);
  if (generation !== connectGeneration || intentionalDisconnect || pausedForHidden) return;
  if (loaded) {
    showFallback(); setPlaceholder("", false);
    setWorkerHealth("warning", "Using MJPEG fallback");
    setStreamMode("mjpeg", "Using MJPEG fallback.");
    clearConnectionError();
    setTroubleStep("video", "warn", "MJPEG fallback is live.");
    setConnectionSummary("Using MJPEG fallback.", "warn");
    setStatus("Using MJPEG fallback.");
    setCodecLabel("MJPEG"); setBitrateLabel("-"); return;
  }
  cancelMjpeg(); hideMedia();
  setWorkerHealth("error", "Stream unavailable");
  setStreamMode("error", "WebRTC failed and MJPEG fallback unavailable.");
  setTroubleStep("video", "error", "MJPEG fallback was unavailable.");
  setConnectionSummary("WebRTC failed and MJPEG fallback was unavailable.", "error");
  setPlaceholder("Stream unavailable.", true); setStatus("WebRTC failed: " + detail, true);
}

async function beginWebRtc(allowRetry, messageText) {
  if (!currentTarget) currentTarget = resolveTargetBase();
  const generation = ++connectGeneration;
  clearReconnectTimer(); closePeer(); cancelMjpeg(); hideMedia();
  setWorkerHealth("connecting", "Negotiating WebRTC");
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
      setWorkerHealth("error", "Worker connection failed");
      setTroubleStep("webrtc", "error", detail);
      setTroubleStep("video", "error", "No video path available.");
      setConnectionSummary(detail, "error");
      rememberConnectionError(error);
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
  setWorkerHealth(isError ? "error" : "neutral", isError ? "Worker connection failed" : "Disconnected");
  setTroubleStep("state", isError ? "error" : "neutral", isError ? reason : "Connection closed.");
  setTroubleStep("webrtc", "neutral", "No active negotiation.");
  setTroubleStep("video", "neutral", "No active stream.");
  if (isError) rememberConnectionError(reason); else clearConnectionError();
  setConnectionSummary(reason, isError ? "error" : "neutral");
  setPlaceholder(reason, true); setStatus(reason, isError); resetWebRtcMediaStats();
}

function pauseStreamForHidden() {
  if (intentionalDisconnect || pausedForHidden) return;
  pausedForHidden = true; clearReconnectTimer(); stopStatePolling(); stopCodecPolling();
  connectGeneration += 1; closePeer(); cancelMjpeg(); hideMedia(); setBusy(false);
  setWorkerHealth("neutral", "Paused while tab is hidden");
  setTroubleStep("state", "neutral", "Polling paused while the tab is hidden.");
  setTroubleStep("webrtc", "neutral", "WebRTC paused while the tab is hidden.");
  setTroubleStep("video", "neutral", "Video paused while the tab is hidden.");
  setConnectionSummary("Paused while the tab is hidden.");
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
  setWorkerHealth("connecting", "Connecting to worker");
  clearConnectionError();
  setConnectionMode(target);
  setTroubleStep("state", "pending", "Checking state...");
  setTroubleStep("webrtc", "pending", "Waiting for WebRTC...");
  setTroubleStep("video", "pending", "No frames yet.");
  setConnectionSummary("Checking worker...");
  startStatePolling(); startCodecPolling();
  await refreshStateNow();
  if (
    intentionalDisconnect ||
    pausedForHidden ||
    !currentTarget ||
    currentTarget.kind !== target.kind ||
    currentTarget.display !== target.display
  ) {
    return;
  }
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

async function sendWorkerCommandPayload(payload) {
  try {
    if (!currentTarget) { currentTarget = resolveTargetBase(); setConnectionMode(currentTarget); }
  } catch (error) {
    setStatus(formatError(error), true);
    return null;
  }
  const seq = ++cmdSeq;
  const body = Object.assign({}, payload, { seq });
  const response = await workerFetch("/api/cmd", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
    body: JSON.stringify(body)
  }, { expect: "json" });
  await fetchState().catch(() => {});
  return response;
}

async function saveContextSettings() {
  const trigger = contextTriggerEl ? contextTriggerEl.value : "interval";
  const cooldown = Math.max(1, parseInt(contextCooldownEl?.value || "30", 10) || 30);
  const profile = contextProfileEl ? contextProfileEl.value : "auto";
  const model = (contextModelEl?.value || "auto").trim() || "auto";
  window.localStorage.setItem(CONTEXT_TRIGGER_KEY, trigger);
  window.localStorage.setItem(CONTEXT_COOLDOWN_KEY, String(cooldown));
  window.localStorage.setItem(CONTEXT_PROFILE_KEY, profile);
  window.localStorage.setItem(CONTEXT_MODEL_KEY, model);
  if (notificationCooldownEl) {
    window.localStorage.setItem(ALERT_NOTIFY_COOLDOWN_KEY, String(getNotificationCooldownSeconds()));
  }
  if (preEventEnabledEl) window.localStorage.setItem(PRE_EVENT_ENABLED_KEY, preEventEnabledEl.value === "1" ? "1" : "0");
  if (preEventSecondsEl) window.localStorage.setItem(PRE_EVENT_SECONDS_KEY, String(preEventSeconds()));
  if (captureDescriptionsEl) window.localStorage.setItem(CAPTURE_DESCRIPTIONS_KEY, captureDescriptionsEl.value === "1" ? "1" : "0");
  syncPreEventRecorder();
  try {
    await sendWorkerCommandPayload({
      cmd: "context_config",
      enabled: true,
      trigger,
      cooldown,
      profile,
      model
    });
    setStatus("Context settings applied.");
  } catch (error) {
    setStatus("Context settings failed: " + formatError(error), true);
  }
}

async function analyzeContextNow() {
  try {
    await sendWorkerCommandPayload({ cmd: "context_analyze" });
    setStatus("One-shot context analysis requested.");
  } catch (error) {
    setStatus("Context analysis failed: " + formatError(error), true);
  }
}

async function stopContextAi() {
  try {
    await sendWorkerCommandPayload({ cmd: "context_emergency_stop" });
    setStatus("Camera analysis stopped.");
  } catch (error) {
    setStatus("Camera analysis stop failed: " + formatError(error), true);
  }
}

async function toggleFullscreen() {
  const target = document.getElementById("stream-container") || videoEl || fallbackEl;
  if (!target || !document.fullscreenEnabled) {
    setStatus("Fullscreen is not available in this browser.", true);
    return;
  }
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
      setStatus("Exited fullscreen.");
    } else {
      await target.requestFullscreen();
      setStatus("Fullscreen enabled.");
    }
  } catch (error) {
    setStatus("Fullscreen failed: " + formatError(error), true);
  }
}

async function describeCaptureIfEnabled() {
  if (!captureDescriptionsEnabled()) return "";
  const before = Number(lastWorkerState?.context?.updated_at || 0);
  try {
    setStatus("Describing capture...");
    await sendWorkerCommandPayload({ cmd: "context_analyze" });
    const deadline = Date.now() + 12000;
    while (Date.now() < deadline) {
      await new Promise(resolve => window.setTimeout(resolve, 600));
      const state = await fetchState().catch(() => null);
      const context = state?.context || {};
      const updatedAt = Number(context.updated_at || 0);
      if (context.summary && (!before || updatedAt > before)) {
        return String(context.summary).slice(0, 600);
      }
    }
  } catch (_) {
    // Capture upload should still succeed if description generation fails.
  }
  return String(lastWorkerState?.context?.summary || "").slice(0, 600);
}

// ===== Capture Frame =====
async function captureFrame() {
  const mediaEl = videoEl.classList.contains("show") ? videoEl : fallbackEl;
  let overlayBlob;
  try {
    if (mediaEl === videoEl && videoEl.srcObject) {
      const canvas = document.createElement("canvas");
      canvas.width = videoEl.videoWidth || 640;
      canvas.height = videoEl.videoHeight || 480;
      canvas.getContext("2d").drawImage(videoEl, 0, 0);
      overlayBlob = await new Promise(res => canvas.toBlob(res, "image/jpeg", 0.92));
    } else if (fallbackEl && fallbackEl.naturalWidth) {
      const canvas = document.createElement("canvas");
      canvas.width = fallbackEl.naturalWidth;
      canvas.height = fallbackEl.naturalHeight;
      canvas.getContext("2d").drawImage(fallbackEl, 0, 0);
      overlayBlob = await new Promise(res => canvas.toBlob(res, "image/jpeg", 0.92));
    }
  } catch (e) {
    setStatus("Capture failed: " + e.message, true); return;
  }
  if (!overlayBlob || overlayBlob.size === 0) { setStatus("Capture failed: empty frame", true); return; }

  // Fetch raw frame (without overlay) for toggle support
  let rawBlob = null;
  try {
    const rawTarget = currentTarget || resolveTargetBase();
    const rawUrl = rawFrameEndpointFor(rawTarget);
    const rawResp = await fetch(rawUrl, { cache: "no-store" });
    if (rawResp.ok) rawBlob = await rawResp.blob();
  } catch (_) { /* raw frame optional */ }

  const csrf = getCsrf();
  const fd = new FormData();
  fd.append("type", "image");
  fd.append("overlay_file", overlayBlob, "capture.jpg");
  if (rawBlob && rawBlob.size > 0) fd.append("raw_file", rawBlob, "capture_raw.jpg");
  captureBtn.disabled = true;
  try {
    const description = await describeCaptureIfEnabled();
    if (description) fd.append("description", description);
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
    startRecord().catch(error => setStatus("Recording failed: " + formatError(error), true));
  }
}

function preferredRecordingMimeType() {
  if (typeof MediaRecorder === "undefined") return "video/webm";
  if (MediaRecorder.isTypeSupported("video/webm;codecs=vp9")) return "video/webm;codecs=vp9";
  if (MediaRecorder.isTypeSupported("video/webm")) return "video/webm";
  if (MediaRecorder.isTypeSupported("video/mp4")) return "video/mp4";
  return "video/webm";
}

function recordingExtensionForMime(mimeType) {
  return String(mimeType || "").toLowerCase().includes("mp4") ? "mp4" : "webm";
}

function startPreEventRecorder() {
  if (!preEventEnabled() || typeof MediaRecorder === "undefined") return;
  if (preEventRecorder && preEventRecorder.state !== "inactive") return;
  if (!videoEl.classList.contains("show") || !videoEl.srcObject) return;
  const mimeType = preferredRecordingMimeType();
  try {
    preEventRecorder = new MediaRecorder(videoEl.srcObject, { mimeType });
  } catch (_) {
    preEventRecorder = null;
    return;
  }
  preEventMimeType = mimeType;
  preEventChunks = [];
  preEventRecorder.ondataavailable = event => {
    if (!event.data || event.data.size <= 0) return;
    preEventChunks.push({ blob: event.data, ts: Date.now() });
    const cutoff = Date.now() - (preEventSeconds() * 1000);
    preEventChunks = preEventChunks.filter(chunk => chunk.ts >= cutoff);
  };
  preEventRecorder.onstop = () => {
    preEventRecorder = null;
  };
  try {
    preEventRecorder.start(1000);
  } catch (_) {
    preEventRecorder = null;
    preEventChunks = [];
  }
}

function stopPreEventRecorder() {
  if (preEventRecorder && preEventRecorder.state !== "inactive") {
    try { preEventRecorder.stop(); } catch (_) {}
  }
  preEventRecorder = null;
  preEventChunks = [];
}

function syncPreEventRecorder() {
  if (preEventEnabled()) startPreEventRecorder();
  else stopPreEventRecorder();
}

async function triggerPreEventUpload(context) {
  if (!preEventEnabled() || !preEventChunks.length) return;
  const now = Date.now() / 1000;
  if (now - lastPreEventUploadAt < Math.max(30, getNotificationCooldownSeconds())) return;
  lastPreEventUploadAt = now;
  const chunks = preEventChunks.map(chunk => chunk.blob);
  const mimeType = preEventMimeType || preferredRecordingMimeType();
  const extension = recordingExtensionForMime(mimeType);
  const blob = new Blob(chunks, { type: mimeType });
  if (!blob.size) return;
  const fd = new FormData();
  fd.append("type", "video");
  fd.append("overlay_file", blob, `pre_event.${extension}`);
  fd.append("duration", String(Math.min(preEventSeconds(), chunks.length)));
  fd.append("description", buildCameraAlert(context).body);
  const resp = await fetch("/api/recordings/upload", { method: "POST", headers: { "X-CSRF-Token": getCsrf() }, body: fd });
  if (resp.ok) {
    const data = await resp.json().catch(() => ({}));
    setStatus("Pre-event clip saved" + (data.id ? " (#" + data.id + ")" : "") + ".");
  }
}

function stopMediaRecorderInstance(recorder) {
  return new Promise(resolve => {
    if (!recorder || recorder.state === "inactive") {
      resolve();
      return;
    }
    const onStop = () => {
      recorder.removeEventListener("stop", onStop);
      resolve();
    };
    recorder.addEventListener("stop", onStop, { once: true });
    try {
      recorder.stop();
    } catch (_) {
      recorder.removeEventListener("stop", onStop);
      resolve();
    }
  });
}

function cleanupRawRecorderResources() {
  const cleanup = rawRecorderCleanup;
  rawRecorderCleanup = null;
  if (typeof cleanup === "function") cleanup();
  rawMediaRecorder = null;
}

function overlayTrackFps(stream) {
  try {
    const track = typeof stream.getVideoTracks === "function" ? stream.getVideoTracks()[0] : null;
    const fromSettings = Number(track?.getSettings?.().frameRate);
    if (Number.isFinite(fromSettings) && fromSettings > 0) return Math.min(30, Math.max(5, fromSettings));
  } catch (_) {
    // Fall back to UI state below.
  }
  const fromUi = parseFloat(stateFpsEl?.textContent || "");
  if (Number.isFinite(fromUi) && fromUi > 0) return Math.min(30, Math.max(5, fromUi));
  return RAW_RECORD_FPS_FALLBACK;
}

async function createRawRecorderSession(mimeType, width, height, fps) {
  const img = new Image();
  img.decoding = "async";
  img.crossOrigin = "anonymous";
  img.referrerPolicy = "no-referrer";

  const canvas = document.createElement("canvas");
  canvas.width = Math.max(2, width || 640);
  canvas.height = Math.max(2, height || 360);
  const ctx = canvas.getContext("2d", { alpha: false });
  if (!ctx) throw new Error("Raw recorder could not create a canvas.");

  const target = currentTarget || resolveTargetBase();
  const rawStreamUrl = rawMjpegEndpointFor(target) + "?ts=" + Date.now();

  await new Promise((resolve, reject) => {
    let settled = false;
    const timeoutId = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(new Error("Raw stream timed out."));
    }, RAW_RECORD_SETUP_TIMEOUT_MS);
    const cleanup = () => {
      window.clearTimeout(timeoutId);
      img.removeEventListener("load", onLoad);
      img.removeEventListener("error", onError);
    };
    const onLoad = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve();
    };
    const onError = () => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(new Error("Raw stream could not be opened."));
    };
    img.addEventListener("load", onLoad);
    img.addEventListener("error", onError);
    img.src = rawStreamUrl;
  });

  let rafId = 0;
  let stopped = false;
  const draw = () => {
    if (stopped) return;
    try {
      if (img.naturalWidth > 0 && img.naturalHeight > 0) {
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      }
    } catch (_) {
      // Keep the recorder running with the last good frame.
    }
    rafId = window.requestAnimationFrame(draw);
  };
  draw();

  const canvasStream = canvas.captureStream(Math.max(5, Math.min(30, fps || RAW_RECORD_FPS_FALLBACK)));
  const recorder = new MediaRecorder(canvasStream, { mimeType });
  const chunks = [];
  recorder.ondataavailable = event => {
    if (event.data && event.data.size > 0) chunks.push(event.data);
  };

  return {
    recorder,
    chunks,
    cleanup: () => {
      stopped = true;
      if (rafId) window.cancelAnimationFrame(rafId);
      try { img.src = ""; } catch (_) { /* ignore */ }
      try { canvasStream.getTracks().forEach(track => track.stop()); } catch (_) { /* ignore */ }
      canvas.width = 0;
      canvas.height = 0;
    }
  };
}

async function startRecord() {
  let stream;
  if (videoEl.classList.contains("show") && videoEl.srcObject) {
    stream = videoEl.srcObject;
  } else {
    setStatus("Recording requires active WebRTC stream.", true);
    return;
  }

  if (recordStopPromise) return;

  const mimeType = preferredRecordingMimeType();
  recordBtn.disabled = true;
  setStatus("Preparing recording...");

  recordedChunks = [];
  rawRecordedChunks = [];
  recordMimeType = mimeType;

  const width = videoEl.videoWidth || 640;
  const height = videoEl.videoHeight || 360;
  const fps = overlayTrackFps(stream);

  let overlayRecorder = null;
  let rawSession = null;
  try {
    overlayRecorder = new MediaRecorder(stream, { mimeType });
  } catch (e) {
    recordBtn.disabled = false;
    setStatus("MediaRecorder not supported: " + e.message, true);
    return;
  }

  try {
    rawSession = await createRawRecorderSession(mimeType, width, height, fps);
  } catch (error) {
    setStatus("Raw companion stream unavailable. Recording overlay only.", true);
  }

  mediaRecorder = overlayRecorder;
  mediaRecorder.ondataavailable = event => {
    if (event.data && event.data.size > 0) recordedChunks.push(event.data);
  };

  rawMediaRecorder = rawSession ? rawSession.recorder : null;
  rawRecorderCleanup = rawSession ? rawSession.cleanup : null;
  if (rawSession) {
    rawMediaRecorder.ondataavailable = event => {
      if (event.data && event.data.size > 0) rawRecordedChunks.push(event.data);
    };
  }

  mediaRecorder.start(1000);
  if (rawMediaRecorder) {
    try {
      rawMediaRecorder.start(1000);
    } catch (error) {
      cleanupRawRecorderResources();
      setStatus("Raw companion stream could not start. Recording overlay only.", true);
    }
  }
  recordStartTime = Date.now();
  recordBtn.textContent = "Stop recording";
  recordBtn.classList.add("danger");
  recordBtn.disabled = false;
  recordTimerEl.style.display = "inline-block";
  recordTimerInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - recordStartTime) / 1000);
    const m = Math.floor(secs / 60).toString().padStart(2, "0");
    const s = (secs % 60).toString().padStart(2, "0");
    recordTimerEl.textContent = m + ":" + s;
  }, 500);
  setStatus(rawMediaRecorder ? "Recording overlay + raw video." : "Recording overlay video.");
}

function stopRecord() {
  if (recordStopPromise) return;
  clearInterval(recordTimerInterval);
  recordBtn.textContent = "Record clip";
  recordBtn.classList.remove("danger");
  recordTimerEl.style.display = "none";
  recordTimerEl.textContent = "";
  recordBtn.disabled = true;

  recordStopPromise = Promise.all([
    stopMediaRecorderInstance(mediaRecorder),
    stopMediaRecorderInstance(rawMediaRecorder),
  ])
    .then(() => uploadRecording())
    .finally(() => {
      recordStopPromise = null;
      recordBtn.disabled = false;
    });
}

async function uploadRecording() {
  const overlayChunks = recordedChunks.slice();
  const rawChunks = rawRecordedChunks.slice();
  recordedChunks = [];
  rawRecordedChunks = [];

  if (overlayChunks.length === 0) {
    cleanupRawRecorderResources();
    mediaRecorder = null;
    setStatus("No video data recorded.", true);
    return;
  }

  const duration = recordStartTime ? (Date.now() - recordStartTime) / 1000 : undefined;
  recordStartTime = null;
  const mimeType = recordMimeType || "video/webm";
  const extension = recordingExtensionForMime(mimeType);
  const blob = new Blob(overlayChunks, { type: mimeType });
  const rawBlob = rawChunks.length ? new Blob(rawChunks, { type: mimeType }) : null;
  mediaRecorder = null;
  cleanupRawRecorderResources();
  const csrf = getCsrf();
  const fd = new FormData();
  fd.append("type", "video");
  fd.append("overlay_file", blob, `recording.${extension}`);
  if (rawBlob && rawBlob.size > 0) {
    fd.append("raw_file", rawBlob, `recording_raw.${extension}`);
  }
  if (duration) fd.append("duration", duration.toFixed(2));
  try {
    const resp = await fetch("/api/recordings/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: fd });
    if (resp.ok) {
      const data = await resp.json();
      setStatus(rawBlob && rawBlob.size > 0
        ? "Video saved with overlay + raw variants (recording #" + data.id + ")"
        : "Video saved (recording #" + data.id + ")");
    } else {
      const err = await resp.json().catch(() => ({}));
      setStatus("Upload failed: " + (err.error || resp.status), true);
    }
  } catch (e) {
    setStatus("Upload failed: " + e.message, true);
  }
}

// ===== Event listeners =====
videoEl.addEventListener("loadedmetadata", () => {
  if (intentionalDisconnect || pausedForHidden) return;
  showVideo(); setPlaceholder("", false);
  syncPreEventRecorder();
  if (videoEl.srcObject) {
    setWorkerHealth("online", "Receiving WebRTC video");
    setStreamMode("webrtc", "Live video is streaming over WebRTC.");
    setStatus("Receiving WebRTC video."); refreshWebRtcMediaLabels().catch(() => {});
  }
});

fallbackEl.addEventListener("load", () => {
  if (intentionalDisconnect || pausedForHidden) return;
});

fallbackEl.addEventListener("error", () => {
  if (stateStreamModeEl.dataset.mode !== "mjpeg" || intentionalDisconnect || pausedForHidden) return;
  cancelMjpeg(); hideMedia();
  setWorkerHealth("error", "MJPEG fallback disconnected");
  setStreamMode("error", "MJPEG fallback disconnected.");
  setTroubleStep("video", "error", "MJPEG fallback disconnected.");
  setConnectionSummary("MJPEG fallback disconnected.", "error");
  rememberConnectionError("MJPEG fallback disconnected.");
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

function isTypingTarget(target) {
  if (!target) return false;
  const tag = String(target.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
}

document.addEventListener("keydown", event => {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || isTypingTarget(event.target)) return;
  const key = String(event.key || "").toLowerCase();
  if (key === "c") {
    event.preventDefault();
    analyzeContextNow();
  } else if (key === "i") {
    event.preventDefault();
    sendCmd("i");
  } else if (key === "p") {
    event.preventDefault();
    sendCmd("p");
  } else if (key === "o") {
    event.preventDefault();
    sendCmd("o");
  } else if (key === "m") {
    event.preventDefault();
    sendCmd("m", "Switching to next model...");
  } else if (key === "n") {
    event.preventDefault();
    sendCmd("n", "Switching to previous model...");
  } else if (key === "q") {
    event.preventDefault();
    sendCmd("q", "Stopping worker...");
  }
});

if (baseInputEl) {
  baseInputEl.addEventListener("input", () => {
    if (!baseInputEl.readOnly) {
      rememberedDirectBase = baseInputEl.value.trim() || rememberedDirectBase;
      window.localStorage.setItem(STREAM_DIRECT_BASE_KEY, rememberedDirectBase);
    }
    updateTargetPreview();
  });
  baseInputEl.addEventListener("keydown", event => {
    if (event.key === "Enter") connect().catch(error => setStatus(formatError(error), true));
  });
}

if (connectionModeEl) {
  connectionModeEl.addEventListener("change", () => {
    window.localStorage.setItem(STREAM_CONNECTION_MODE_KEY, connectionModeEl.value);
    updateBaseInputUi();
    updateTargetPreview();
  });
}

// ===== Init =====
migrateAlertNotificationSettings();
rememberedDirectBase = window.localStorage.getItem(STREAM_DIRECT_BASE_KEY) || DEFAULT_DIRECT_BASE;
if (connectionModeEl) {
  const savedMode = window.localStorage.getItem(STREAM_CONNECTION_MODE_KEY);
  connectionModeEl.value = IS_FILE_PROTOCOL ? "direct" : (savedMode === "direct" ? "direct" : "proxy");
}
if (notificationCooldownEl) {
  notificationCooldownEl.value = window.localStorage.getItem(ALERT_NOTIFY_COOLDOWN_KEY) || notificationCooldownEl.value || "60";
  notificationCooldownEl.addEventListener("change", () => {
    window.localStorage.setItem(ALERT_NOTIFY_COOLDOWN_KEY, String(getNotificationCooldownSeconds()));
  });
}
syncLocalAlertSettings();
if (preEventEnabledEl) {
  preEventEnabledEl.addEventListener("change", () => {
    window.localStorage.setItem(PRE_EVENT_ENABLED_KEY, preEventEnabledEl.value === "1" ? "1" : "0");
    syncPreEventRecorder();
  });
}
if (preEventSecondsEl) {
  preEventSecondsEl.addEventListener("change", () => {
    window.localStorage.setItem(PRE_EVENT_SECONDS_KEY, String(preEventSeconds()));
  });
}
if (captureDescriptionsEl) {
  captureDescriptionsEl.addEventListener("change", () => {
    window.localStorage.setItem(CAPTURE_DESCRIPTIONS_KEY, captureDescriptionsEl.value === "1" ? "1" : "0");
  });
}
if (contextTriggerEl) {
  contextTriggerEl.addEventListener("change", () => {
    window.localStorage.setItem(CONTEXT_TRIGGER_KEY, contextTriggerEl.value || "person_appears");
  });
}
if (contextCooldownEl) {
  contextCooldownEl.addEventListener("change", () => {
    const value = Math.max(1, parseInt(contextCooldownEl.value || "60", 10) || 60);
    contextCooldownEl.value = String(value);
    window.localStorage.setItem(CONTEXT_COOLDOWN_KEY, String(value));
  });
}
if (contextProfileEl) {
  contextProfileEl.addEventListener("change", () => {
    window.localStorage.setItem(CONTEXT_PROFILE_KEY, contextProfileEl.value || "auto");
  });
}
if (contextModelEl) {
  contextModelEl.addEventListener("change", () => {
    window.localStorage.setItem(CONTEXT_MODEL_KEY, (contextModelEl.value || "auto").trim() || "auto");
  });
}
refreshNotificationButton();
if (baseInputEl) {
  if (IS_FILE_PROTOCOL && (!baseInputEl.value.trim() || baseInputEl.value.trim() === "/")) {
    baseInputEl.value = rememberedDirectBase || DEFAULT_DIRECT_BASE;
  } else if (!IS_FILE_PROTOCOL && connectionModeEl?.value === "direct" && (!baseInputEl.value.trim() || baseInputEl.value.trim() === "/")) {
    baseInputEl.value = rememberedDirectBase || DEFAULT_DIRECT_BASE;
  } else if (!IS_FILE_PROTOCOL && !baseInputEl.value.trim()) {
    baseInputEl.value = "/";
  }
}
updateBaseInputUi();
const initialConnectionKind = connectionModeEl?.value === "direct" ? "direct" : "proxy";
setConnectionMode(
  initialConnectionKind === "direct"
    ? { kind: "direct", display: getBaseInputValue() || rememberedDirectBase || DEFAULT_DIRECT_BASE }
    : { kind: "proxy", display: PAGE_ORIGIN || "/" }
);
updateTargetPreview();
setStreamMode("idle", "WebRTC first, MJPEG fallback if needed.");
setWorkerHealth("connecting", "Connecting to worker");
resetWebRtcMediaStats();
setTroubleStep("state", "pending", "Waiting for the first worker check.");
setTroubleStep("webrtc", "pending", "WebRTC will be tried first.");
setTroubleStep("video", "pending", "MJPEG fallback stays on standby.");
setConnectionSummary("Preparing connection checks...");
setPlaceholder(IS_FILE_PROTOCOL ? "Connecting to local worker..." : "Connecting...", true);
connect().catch(error => setStatus(formatError(error), true));

// ===== Event delegation for data-action buttons =====
document.addEventListener("click", event => {
  const btn = event.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "capture") captureFrame();
  else if (action === "record") toggleRecord();
  else if (action === "fullscreen") toggleFullscreen();
  else if (action === "cmd") sendCmd(btn.dataset.cmd, btn.dataset.busy || undefined);
  else if (action === "connect") connect().catch(error => setStatus(formatError(error), true));
  else if (action === "context-save") saveContextSettings();
  else if (action === "context-analyze") analyzeContextNow();
  else if (action === "context-stop") stopContextAi();
  else if (action === "notification-toggle") toggleNotifications();
  else if (action === "delete-passkey") deletePasskey(parseInt(btn.dataset.credId), btn.dataset.name);
});

// ===== Passkey Management =====
function base64urlToBuffer(b64url) {
  const b64 = b64url.replace(/-/g, '+').replace(/_/g, '/');
  const pad = b64.length % 4 === 0 ? '' : '='.repeat(4 - (b64.length % 4));
  const bin = atob(b64 + pad);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let str = '';
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function loadPasskeys() {
  const el = document.getElementById('passkeys-list');
  if (!el) return;
  try {
    const resp = await fetch('/auth/webauthn/credentials', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const keys = await resp.json();
    if (!keys.length) {
      el.innerHTML = '<span class="small">No passkeys registered yet.</span>';
      return;
    }
    let html = '';
    for (const k of keys) {
      const name = String(k.name || 'Passkey').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
      html += `<div class="passkey-item">
        <span>${name}</span>
        <button data-action="delete-passkey" data-cred-id="${k.id}" data-name="${name}" class="danger admin-inline-button">Delete</button>
      </div>`;
    }
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = '<span class="small error">Failed: ' + err.message + '</span>';
  }
}

async function registerPasskey() {
  const btn = document.getElementById('register-passkey-btn');
  if (btn) btn.disabled = true;
  try {
    const appUI = window.AppUI || {};
    const promptDialog = typeof appUI.prompt === "function" ? appUI.prompt : async () => null;
    const toast = typeof appUI.toast === "function" ? appUI.toast : () => null;
    const beginResp = await fetch('/auth/webauthn/register/begin', {
      method: 'POST', headers: { 'X-CSRF-Token': getCsrf() }
    });
    if (!beginResp.ok) throw new Error('Failed to start registration');
    const options = await beginResp.json();

    options.challenge = base64urlToBuffer(options.challenge);
    options.user.id = base64urlToBuffer(options.user.id);
    if (options.excludeCredentials) {
      options.excludeCredentials = options.excludeCredentials.map(c => ({ ...c, id: base64urlToBuffer(c.id) }));
    }

    const credential = await navigator.credentials.create({ publicKey: options });
    const attestation = {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      response: {
        attestationObject: bufferToBase64url(credential.response.attestationObject),
        clientDataJSON: bufferToBase64url(credential.response.clientDataJSON)
      }
    };
    const name = await promptDialog({
      title: "Name passkey",
      message: "Choose a friendly name for this passkey.",
      inputLabel: "Passkey name",
      placeholder: "My Passkey",
      value: "My Passkey",
      confirmLabel: "Save passkey",
      cancelLabel: "Cancel"
    });
    if (name) attestation.name = name;

    const completeResp = await fetch('/auth/webauthn/register/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
      body: JSON.stringify(attestation)
    });
    if (!completeResp.ok) {
      const err = await completeResp.json().catch(() => ({}));
      throw new Error(err.detail || 'Registration failed');
    }
    setStatus("Passkey registered successfully!");
    toast("Passkey registered successfully.", { tone: "success", title: "Passkey added" });
    await loadPasskeys();
  } catch (err) {
    if (err.name !== 'AbortError') {
      setStatus('Passkey error: ' + err.message, true);
      toast("Passkey error: " + err.message, { tone: "error", title: "Passkey failed" });
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deletePasskey(credId, name) {
  const appUI = window.AppUI || {};
  const confirmDialog = typeof appUI.confirm === "function"
    ? appUI.confirm
    : async () => false;
  const toast = typeof appUI.toast === "function" ? appUI.toast : () => null;
  const confirmed = await confirmDialog({
    title: "Delete passkey",
    message: 'Delete passkey "' + name + '"? This cannot be undone.',
    confirmLabel: "Delete passkey",
    cancelLabel: "Keep passkey",
    confirmTone: "danger",
    tone: "danger"
  });
  if (!confirmed) return;
  try {
    const resp = await fetch('/auth/webauthn/credentials/' + credId, {
      method: 'DELETE', headers: { 'X-CSRF-Token': getCsrf() }
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    toast('Passkey "' + name + '" deleted.', { tone: "success", title: "Passkey removed" });
    await loadPasskeys();
  } catch (err) {
    setStatus('Delete failed: ' + err.message, true);
    toast('Delete failed: ' + err.message, { tone: "error", title: "Passkey delete failed" });
  }
}

loadPasskeys();
const _regBtn = document.getElementById('register-passkey-btn');
if (_regBtn) _regBtn.addEventListener('click', registerPasskey);
