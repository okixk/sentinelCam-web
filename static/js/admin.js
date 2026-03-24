/* admin.js - Admin dashboard: users, sessions, worker status, and passkeys */

function getCsrf() {
  return document.cookie.match(/csrf_token=([^;]+)/)?.[1] || "";
}

function formatDate(ts) {
  if (!ts) return "-";
  return new Date(parseFloat(ts) * 1000).toLocaleString();
}

function escHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const appUI = window.AppUI || {};
const toast = typeof appUI.toast === "function" ? appUI.toast : () => null;
const confirmDialog = typeof appUI.confirm === "function" ? appUI.confirm : async () => false;
const promptDialog = typeof appUI.prompt === "function" ? appUI.prompt : async () => null;

const passwordResetModal = document.getElementById("password-reset-modal");
const passwordResetForm = document.getElementById("password-reset-form");
const passwordResetUsername = document.getElementById("password-reset-username");
const passwordResetUsernameInput = document.getElementById("password-reset-username-input");
const passwordResetPassword = document.getElementById("password-reset-password");
const passwordResetConfirm = document.getElementById("password-reset-confirm");
const passwordResetFeedback = document.getElementById("password-reset-feedback");
const passwordResetSubmit = document.getElementById("password-reset-submit");
let passwordResetTarget = null;
let passwordResetCloseTimer = null;

const WORKER_POLL_BASE_MS = 2000;
const WORKER_POLL_MAX_MS = 15000;
const OPS_POLL_BASE_MS = 10000;
const OPS_POLL_MAX_MS = 30000;
let workerStatusTimer = null;
let workerStatusFailureCount = 0;
let workerStatusInFlight = false;
let workerStatusWasOffline = false;
let opsStatusTimer = null;
let opsStatusFailureCount = 0;
let opsStatusInFlight = false;

function setPasswordResetFeedback(message, tone = "neutral") {
  if (!passwordResetFeedback) return;
  passwordResetFeedback.className = "modal-feedback small";
  if (tone === "error") {
    passwordResetFeedback.classList.add("error");
  } else if (tone === "success") {
    passwordResetFeedback.classList.add("ok");
  } else if (tone === "warn") {
    passwordResetFeedback.classList.add("warn");
  }
  passwordResetFeedback.textContent = message || "";
}

function closePasswordResetModal() {
  if (!passwordResetModal) return;
  window.clearTimeout(passwordResetCloseTimer);
  passwordResetCloseTimer = null;
  passwordResetTarget = null;
  passwordResetModal.hidden = true;
  passwordResetModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
  if (passwordResetForm) {
    passwordResetForm.reset();
  }
  if (passwordResetUsernameInput) {
    passwordResetUsernameInput.value = "";
  }
  setPasswordResetFeedback("");
}

function openPasswordResetModal(userId, username) {
  if (!passwordResetModal || !passwordResetForm) return;
  window.clearTimeout(passwordResetCloseTimer);
  passwordResetCloseTimer = null;
  passwordResetTarget = { userId, username };
  if (passwordResetUsername) {
    passwordResetUsername.textContent = username || "this user";
  }
  if (passwordResetUsernameInput) {
    passwordResetUsernameInput.value = username || "";
  }
  passwordResetForm.reset();
  setPasswordResetFeedback("Enter a new password and confirm it.", "neutral");
  passwordResetModal.hidden = false;
  passwordResetModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  window.requestAnimationFrame(() => {
    passwordResetPassword?.focus();
  });
}

function formatRetryDelay(ms) {
  if (ms < 1000) return ms + " ms";
  const seconds = Math.round(ms / 1000);
  return seconds + (seconds === 1 ? " second" : " seconds");
}

function nextWorkerPollDelay() {
  if (workerStatusFailureCount <= 0) return WORKER_POLL_BASE_MS;
  const scaled = WORKER_POLL_BASE_MS * Math.pow(2, workerStatusFailureCount - 1);
  return Math.min(scaled, WORKER_POLL_MAX_MS);
}

function scheduleWorkerStatusPoll(delayMs = null, options = {}) {
  window.clearTimeout(workerStatusTimer);
  workerStatusTimer = window.setTimeout(() => {
    loadWorkerStatus({ silent: options.silent !== false }).catch(() => {});
  }, delayMs == null ? nextWorkerPollDelay() : delayMs);
}

function nextOpsPollDelay() {
  if (opsStatusFailureCount <= 0) return OPS_POLL_BASE_MS;
  const scaled = OPS_POLL_BASE_MS * Math.pow(2, opsStatusFailureCount - 1);
  return Math.min(scaled, OPS_POLL_MAX_MS);
}

function scheduleOpsStatusPoll(delayMs = null) {
  window.clearTimeout(opsStatusTimer);
  opsStatusTimer = window.setTimeout(() => {
    loadOpsStatus({ silent: true }).catch(() => {});
  }, delayMs == null ? nextOpsPollDelay() : delayMs);
}

async function loadOpsStatus(options = {}) {
  if (opsStatusInFlight && !options.force) return;
  opsStatusInFlight = true;
  const el = document.getElementById("ops-status");
  if (!el) {
    opsStatusInFlight = false;
    return;
  }
  try {
    const resp = await fetch("/api/admin/ops", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    opsStatusFailureCount = 0;
    const thumbnail = data.thumbnail || {};
    const worker = data.worker || {};
    const queueSummary = `${thumbnail.pending_count || 0} pending | ${thumbnail.inflight_count || 0} inflight | ${thumbnail.active_tasks || 0} active tasks`;
    const resultSummary = `${thumbnail.completed_count || 0} completed | ${thumbnail.failed_count || 0} failed`;
    const lastWorkerOk = worker.last_ok_at ? formatDate(worker.last_ok_at) : "Never";
    const lastWorkerError = worker.last_error_at ? formatDate(worker.last_error_at) : "None";
    el.innerHTML = `
      <div class="stack-note-card">
        <strong>Thumbnail queue</strong>
        <div class="small">${queueSummary}</div>
        <div class="small">${resultSummary}</div>
      </div>
      <div class="stack-note-card">
        <strong>Thumbnail timings</strong>
        <div class="small">Last success: ${thumbnail.last_success_at ? formatDate(thumbnail.last_success_at) : "-"}</div>
        <div class="small">Last failure: ${thumbnail.last_failure_at ? formatDate(thumbnail.last_failure_at) : "-"}</div>
        <div class="small">Last recording: ${thumbnail.last_recording_id || "-"}</div>
      </div>
      <div class="stack-note-card">
        <strong>Worker reachability</strong>
        <div class="small">Last reachable: ${lastWorkerOk}</div>
        <div class="small">Last attempt path: ${escHtml(worker.last_path || "-")}</div>
        <div class="small">Last status: ${worker.last_status_code != null ? worker.last_status_code : "-"}</div>
      </div>
      <div class="stack-note-card">
        <strong>Latest issues</strong>
        <div class="small">Worker error at: ${lastWorkerError}</div>
        <div class="small">${escHtml(worker.last_error || thumbnail.last_error || "No recent errors.")}</div>
      </div>`;
    scheduleOpsStatusPoll(OPS_POLL_BASE_MS);
  } catch (err) {
    opsStatusFailureCount += 1;
    if (!options.silent) {
      el.innerHTML = '<span class="small error">Failed to load ops status: ' + err.message + "</span>";
    }
    scheduleOpsStatusPoll(nextOpsPollDelay());
  } finally {
    opsStatusInFlight = false;
  }
}

async function submitPasswordReset(event) {
  event.preventDefault();
  if (!passwordResetTarget) {
    setPasswordResetFeedback("No user selected.", "error");
    return;
  }
  const password = (passwordResetPassword?.value || "");
  const confirm = (passwordResetConfirm?.value || "");
  if (password.length < 12) {
    setPasswordResetFeedback("Password too short. Use at least 12 characters.", "error");
    return;
  }
  if (password !== confirm) {
    setPasswordResetFeedback("Passwords do not match.", "error");
    return;
  }

  if (passwordResetSubmit) {
    passwordResetSubmit.disabled = true;
  }
  setPasswordResetFeedback("Updating password...", "neutral");

  try {
    const resp = await fetch("/api/admin/users/" + passwordResetTarget.userId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify({ password })
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    setPasswordResetFeedback("Password updated.", "success");
    toast("Password updated for " + passwordResetTarget.username + ".", { tone: "success", title: "Password reset" });
    await loadUsers();
    passwordResetCloseTimer = window.setTimeout(closePasswordResetModal, 900);
  } catch (err) {
    setPasswordResetFeedback("Reset failed: " + err.message, "error");
    toast("Password reset failed: " + err.message, { tone: "error", title: "Reset failed" });
  } finally {
    if (passwordResetSubmit) {
      passwordResetSubmit.disabled = false;
    }
  }
}

async function loadWorkerStatus(options = {}) {
  if (workerStatusInFlight && !options.force) return;
  workerStatusInFlight = true;
  const el = document.getElementById("worker-status");
  try {
    const resp = await fetch("/api/state", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    const wasOffline = workerStatusWasOffline;
    workerStatusFailureCount = 0;
    workerStatusWasOffline = false;
    const lastError = data.last_error ? escHtml(data.last_error) : "-";
    const lastCommand = data.cmd_last ? escHtml(data.cmd_last) : "-";
    el.innerHTML = `
      <div class="table-wrap">
        <table class="admin-table">
          <tr><th>Preset</th><td>${escHtml(data.preset || "-")}</td></tr>
          <tr><th>Detection</th><td>${escHtml(data.det || "-")}</td></tr>
          <tr><th>FPS</th><td>${data.fps != null ? Number(data.fps).toFixed(1) : "-"}</td></tr>
          <tr><th>Pose</th><td>${data.pose_enabled ? "on" : "off"}</td></tr>
          <tr><th>Inference</th><td>${data.inference_enabled ? "on" : "off"}</td></tr>
          <tr><th>Last command</th><td>${lastCommand}</td></tr>
          <tr><th>Worker error</th><td>${lastError}</td></tr>
      <tr><th>Stream backend</th><td>${escHtml(data.stream_backend || "-")}</td></tr>
      <tr><th>WebRTC available</th><td>${data.webrtc_available ? "yes" : "no"}</td></tr>
        </table>
      </div>`;
    if (wasOffline) {
      toast("Worker is back online.", { tone: "success", title: "Worker online" });
    }
    scheduleWorkerStatusPoll(WORKER_POLL_BASE_MS, { silent: true });
  } catch (err) {
    workerStatusFailureCount += 1;
    const workerStatusDelayMs = nextWorkerPollDelay();
    const retryLabel = formatRetryDelay(workerStatusDelayMs);
    el.innerHTML = '<span class="small error">Worker unreachable: ' + err.message + '. Retrying in ' + retryLabel + '.</span>';
    if (!workerStatusWasOffline) {
      workerStatusWasOffline = true;
      toast("Worker unreachable. Retrying in the background.", { tone: "warn", title: "Worker offline" });
    }
    scheduleWorkerStatusPoll(workerStatusDelayMs, { silent: true });
  } finally {
    workerStatusInFlight = false;
  }
}

async function adminCmd(cmd) {
  try {
    const resp = await fetch("/api/cmd", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify({ cmd })
    });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(payload.error || payload.detail || ("HTTP " + resp.status));
    }
    toast("Command sent: " + cmd, { tone: "success", title: "Worker command" });
    loadWorkerStatus({ force: true, silent: true }).catch(() => {});
    loadOpsStatus({ force: true, silent: true }).catch(() => {});
  } catch (err) {
    toast("Command failed: " + err.message, { tone: "error", title: "Worker command" });
  }
}

async function loadUsers() {
  const el = document.getElementById("users-table");
  try {
    const resp = await fetch("/api/admin/users", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const users = await resp.json();

    if (!users.length) {
      el.innerHTML = '<p class="small">No users yet.</p>';
      return;
    }

    let html = '<div class="table-wrap"><table class="admin-table"><thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Last login</th><th>Actions</th></tr></thead><tbody>';
    for (const user of users) {
      const locked = user.locked_until && user.locked_until > Date.now() / 1000;
      html += `<tr>
        <td>${user.id}</td>
        <td>${escHtml(user.username)}${locked ? " (locked)" : ""}</td>
        <td>
          <select data-user-id="${user.id}" class="admin-inline-select">
            <option value="viewer"${user.role === "viewer" ? " selected" : ""}>Viewer</option>
            <option value="admin"${user.role === "admin" ? " selected" : ""}>Admin</option>
          </select>
        </td>
        <td>${formatDate(user.last_login)}</td>
        <td>
          <div class="admin-actions">
            <button data-action="save-role" data-user-id="${user.id}" class="secondary admin-inline-button">Save role</button>
            <button data-action="reset-pw" data-user-id="${user.id}" data-username="${escHtml(user.username)}" class="secondary admin-inline-button">Reset password</button>
            <button data-action="delete-user" data-user-id="${user.id}" data-username="${escHtml(user.username)}" class="danger admin-inline-button">Delete</button>
          </div>
        </td>
      </tr>`;
    }
    html += "</tbody></table></div>";
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = '<span class="small error">Failed: ' + err.message + "</span>";
  }
}

async function saveRole(userId) {
  const select = document.querySelector(`select[data-user-id="${userId}"]`);
  if (!select) return;
  try {
    const resp = await fetch("/api/admin/users/" + userId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify({ role: select.value })
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    toast("Role updated for user #" + userId + ".", { tone: "success", title: "User saved" });
    await loadUsers();
  } catch (err) {
    toast("Save role failed: " + err.message, { tone: "error", title: "User update failed" });
  }
}

function resetPassword(userId, username) {
  openPasswordResetModal(userId, username);
}

async function deleteUser(userId, username) {
  const confirmed = await confirmDialog({
    title: "Delete user",
    message: `Delete user "${username}"? This is irreversible.`,
    confirmLabel: "Delete user",
    cancelLabel: "Keep user",
    confirmTone: "danger",
    tone: "danger"
  });
  if (!confirmed) return;
  try {
    const resp = await fetch("/api/admin/users/" + userId, {
      method: "DELETE",
      headers: { "X-CSRF-Token": getCsrf() }
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    toast(`User "${username}" deleted.`, { tone: "success", title: "User removed" });
    await loadUsers();
  } catch (err) {
    toast("Delete failed: " + err.message, { tone: "error", title: "User delete failed" });
  }
}

async function createUser(event) {
  event.preventDefault();
  const username = document.getElementById("new-username").value.trim();
  const password = document.getElementById("new-password").value;
  const role = document.getElementById("new-role").value;
  if (!username || !password) return;
  if (password.length < 12) {
    toast("Password too short. Use at least 12 characters.", { tone: "warn", title: "Validation" });
    return;
  }

  try {
    const resp = await fetch("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify({ username, password, role })
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    document.getElementById("new-username").value = "";
    document.getElementById("new-password").value = "";
    toast(`Created ${role} user "${username}".`, { tone: "success", title: "User created" });
    await loadUsers();
  } catch (err) {
    toast("Create user failed: " + err.message, { tone: "error", title: "User creation failed" });
  }
}

async function loadSessions() {
  const el = document.getElementById("sessions-table");
  try {
    const resp = await fetch("/api/admin/sessions", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const sessions = await resp.json();

    if (!sessions.length) {
      el.innerHTML = '<p class="small">No active sessions.</p>';
      return;
    }

    let html = '<div class="table-wrap"><table class="admin-table"><thead><tr><th>User</th><th>IP</th><th>Expires</th><th>Action</th></tr></thead><tbody>';
    for (const session of sessions) {
      html += `<tr>
        <td>${escHtml(session.username)}</td>
        <td>${escHtml(session.ip_address || "-")}</td>
        <td>${formatDate(session.expires_at)}</td>
        <td><button data-action="revoke-session" data-session-id="${session.id}" class="danger admin-inline-button">Revoke</button></td>
      </tr>`;
    }
    html += "</tbody></table></div>";
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = '<span class="small error">Failed: ' + err.message + "</span>";
  }
}

async function revokeSession(sessionId) {
  const confirmed = await confirmDialog({
    title: "Revoke session",
    message: "Revoke this session immediately? The browser will lose access on its next request.",
    confirmLabel: "Revoke session",
    cancelLabel: "Keep session",
    confirmTone: "danger",
    tone: "warning"
  });
  if (!confirmed) return;
  try {
    const resp = await fetch("/api/admin/sessions/" + sessionId, {
      method: "DELETE",
      headers: { "X-CSRF-Token": getCsrf() }
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    toast("Session revoked.", { tone: "success", title: "Session removed" });
    await loadSessions();
  } catch (err) {
    toast("Revoke failed: " + err.message, { tone: "error", title: "Session revoke failed" });
  }
}

function loadSystemInfo() {
  const el = document.getElementById("system-info");
  if (!el) return;
  const appOrigin = window.location.origin || "http://localhost:3000";
  el.innerHTML = `
    <div class="stack-note-card">
      <strong>Proxy origin</strong>
      <div class="admin-actions" style="margin-top:8px;">
        <code>${escHtml(appOrigin)}</code>
        <button type="button" class="ghost copy-button admin-inline-button" data-copy-text="${escHtml(appOrigin)}">Copy</button>
      </div>
    </div>
    <div class="stack-note-card">
      <strong>PowerShell worker</strong>
      <div class="admin-actions" style="margin-top:8px;">
        <code>powershell -ExecutionPolicy Bypass -File .runtime\\live\\start-worker-powershell.ps1</code>
        <button type="button" class="ghost copy-button admin-inline-button" data-copy-text="powershell -ExecutionPolicy Bypass -File .runtime\\live\\start-worker-powershell.ps1">Copy</button>
      </div>
    </div>`;
  if (typeof initCopyButtons === "function") initCopyButtons();
}

async function loadPasskeys() {
  const el = document.getElementById("passkeys-list");
  if (!el) return;
  try {
    const resp = await fetch("/auth/webauthn/credentials", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const keys = await resp.json();

    if (!keys.length) {
      el.innerHTML = '<p class="small">No passkeys registered yet.</p>';
      return;
    }

    let html = '<div class="table-wrap"><table class="admin-table"><thead><tr><th>Name</th><th>Sign count</th><th>Registered</th><th>Action</th></tr></thead><tbody>';
    for (const key of keys) {
      html += `<tr>
        <td>${escHtml(key.name)}</td>
        <td>${key.sign_count}</td>
        <td>${formatDate(key.created_at)}</td>
        <td><button data-action="delete-passkey" data-cred-id="${key.id}" data-name="${escHtml(key.name)}" class="danger admin-inline-button">Delete</button></td>
      </tr>`;
    }
    html += "</tbody></table></div>";
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = '<span class="small error">Failed: ' + err.message + "</span>";
  }
}

function base64urlToBuffer(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  const bin = atob(b64 + pad);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let str = "";
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function registerPasskey() {
  const btn = document.getElementById("register-passkey-btn");
  if (btn) btn.disabled = true;
  try {
    const name = await promptDialog({
      title: "Name passkey",
      message: "Choose a friendly name for this passkey before registration starts.",
      inputLabel: "Passkey name",
      placeholder: "My Passkey",
      value: "My Passkey",
      confirmLabel: "Continue",
      cancelLabel: "Cancel"
    });
    if (name === null) return;

    const beginResp = await fetch("/auth/webauthn/register/begin", {
      method: "POST",
      headers: { "X-CSRF-Token": getCsrf() }
    });
    if (!beginResp.ok) throw new Error("Failed to start registration");
    const options = await beginResp.json();

    options.challenge = base64urlToBuffer(options.challenge);
    options.user.id = base64urlToBuffer(options.user.id);
    if (options.excludeCredentials) {
      options.excludeCredentials = options.excludeCredentials.map(credential => ({
        ...credential,
        id: base64urlToBuffer(credential.id)
      }));
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
    attestation.name = name;

    const completeResp = await fetch("/auth/webauthn/register/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify(attestation)
    });
    if (!completeResp.ok) {
      const err = await completeResp.json().catch(() => ({}));
      throw new Error(err.detail || "Registration failed");
    }

    toast(`Passkey "${name}" registered.`, { tone: "success", title: "Passkey added" });
    await loadPasskeys();
  } catch (err) {
    if (err.name !== "AbortError") {
      toast("Passkey registration failed: " + err.message, { tone: "error", title: "Passkey failed" });
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deletePasskey(credId, name) {
  const confirmed = await confirmDialog({
    title: "Delete passkey",
    message: `Delete passkey "${name}"? This cannot be undone.`,
    confirmLabel: "Delete passkey",
    cancelLabel: "Keep passkey",
    confirmTone: "danger",
    tone: "danger"
  });
  if (!confirmed) return;
  try {
    const resp = await fetch("/auth/webauthn/credentials/" + credId, {
      method: "DELETE",
      headers: { "X-CSRF-Token": getCsrf() }
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    toast(`Passkey "${name}" deleted.`, { tone: "success", title: "Passkey removed" });
    await loadPasskeys();
  } catch (err) {
    toast("Delete failed: " + err.message, { tone: "error", title: "Passkey delete failed" });
  }
}

loadUsers();
loadSessions();
loadSystemInfo();
loadPasskeys();
loadOpsStatus({ force: true }).catch(() => {});

document.addEventListener("click", event => {
  const btn = event.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "admin-cmd") adminCmd(btn.dataset.cmd);
  else if (action === "save-role") saveRole(parseInt(btn.dataset.userId, 10));
  else if (action === "reset-pw") resetPassword(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "cancel-password-reset") closePasswordResetModal();
  else if (action === "delete-user") deleteUser(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "revoke-session") revokeSession(btn.dataset.sessionId);
  else if (action === "delete-passkey") deletePasskey(parseInt(btn.dataset.credId, 10), btn.dataset.name);
});

document.getElementById("create-user-form").addEventListener("submit", createUser);
if (passwordResetForm) passwordResetForm.addEventListener("submit", submitPasswordReset);

const registerBtn = document.getElementById("register-passkey-btn");
if (registerBtn) registerBtn.addEventListener("click", registerPasskey);

if (passwordResetModal) {
  passwordResetModal.addEventListener("click", event => {
    if (event.target === passwordResetModal) {
      closePasswordResetModal();
    }
  });
}

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && passwordResetModal && !passwordResetModal.hidden) {
    closePasswordResetModal();
  }
});

loadWorkerStatus({ silent: true });
