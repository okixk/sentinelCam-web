/* admin.js - Admin dashboard: users, sessions, ops status, and passkeys */

function getCsrf() {
  return document.cookie.match(/csrf_token=([^;]+)/)?.[1] || "";
}

function formatDate(ts) {
  if (!ts) return "-";
  return new Date(parseFloat(ts) * 1000).toLocaleString();
}

function formatDuration(totalSeconds) {
  let seconds = Math.max(0, Math.ceil(parseFloat(totalSeconds) || 0));
  if (seconds <= 0) return "now";
  const days = Math.floor(seconds / 86400);
  seconds -= days * 86400;
  const hours = Math.floor(seconds / 3600);
  seconds -= hours * 3600;
  const minutes = Math.floor(seconds / 60);
  seconds -= minutes * 60;
  const parts = [];
  if (days) parts.push(days + "d");
  if (hours) parts.push(hours + "h");
  if (minutes) parts.push(minutes + "m");
  if (!parts.length || seconds) parts.push(seconds + "s");
  return parts.slice(0, 2).join(" ");
}

function escHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let n = value;
  while (n >= 1024 && idx < units.length - 1) {
    n /= 1024;
    idx += 1;
  }
  return `${n.toFixed(n >= 100 || idx === 0 ? 0 : 1)} ${units[idx]}`;
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

const OPS_POLL_BASE_MS = 10000;
const OPS_POLL_MAX_MS = 30000;
let opsStatusTimer = null;
let opsStatusFailureCount = 0;
let opsStatusInFlight = false;
const SECURITY_POLL_BASE_MS = 10000;
const SECURITY_POLL_MAX_MS = 30000;
let securityStatusTimer = null;
let securityStatusFailureCount = 0;
let securityStatusInFlight = false;

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
    const storage = data.storage || {};
    const disk = storage.disk || {};
    const database = data.database || {};
    const sessions = data.sessions || {};
    const worker = data.worker || {};
    const errors = Array.isArray(data.errors) ? data.errors : [];

    const dbBadge = database.ok
      ? `<span class="status-pill ok">OK · ${database.latency_ms ?? "?"} ms</span>`
      : `<span class="status-pill error">DOWN</span>`;
    const workerBadge = worker.configured
      ? `<span class="status-pill ok">configured</span>`
      : `<span class="status-pill neutral">not configured</span>`;
    const queueSummary = `${thumbnail.pending_count || 0} pending | ${thumbnail.inflight_count || 0} inflight | ${thumbnail.active_tasks || 0} active`;
    const resultSummary = `${thumbnail.completed_count || 0} completed | ${thumbnail.failed_count || 0} failed`;
    const recordingsBytes = storage.recordings_bytes;
    const recordingsSize = recordingsBytes == null ? "-" : formatSize(recordingsBytes);
    const diskTotal = disk.total_bytes ? formatSize(disk.total_bytes) : "-";
    const diskFree = disk.free_bytes ? formatSize(disk.free_bytes) : "-";
    const diskPercent = (disk.total_bytes && disk.used_bytes)
      ? ((disk.used_bytes / disk.total_bytes) * 100).toFixed(1) + "%"
      : "-";
    const uptime = formatDuration(data.uptime_seconds || 0);

    const errorRows = errors.length
      ? errors.slice(0, 10).map(e => `
          <div class="small">
            <strong>${escHtml(e.level || "WARNING")}</strong>
            <span class="ops-error-time">${formatDate(e.timestamp)}</span>
            <span class="ops-error-logger">${escHtml(e.logger || "-")}</span>
            <div class="ops-error-msg">${escHtml(e.message || "")}</div>
          </div>`).join("")
      : '<div class="small">No recent errors recorded.</div>';

    el.innerHTML = `
      <div class="stack-note-card">
        <strong>Process</strong>
        <div class="small">Uptime: ${uptime}</div>
      </div>
      <div class="stack-note-card">
        <strong>Database</strong> ${dbBadge}
        <div class="small">${escHtml(database.host || "-")}:${database.port || "-"} / ${escHtml(database.db || "-")}</div>
        ${database.ok ? "" : `<div class="small error">${escHtml(database.error || "")}</div>`}
      </div>
      <div class="stack-note-card">
        <strong>Recording storage</strong>
        <div class="small">Path: ${escHtml(storage.path || "-")}</div>
        <div class="small">Used by recordings: ${recordingsSize}</div>
        <div class="small">Disk: ${diskFree} free of ${diskTotal} (${diskPercent} used)</div>
      </div>
      <div class="stack-note-card">
        <strong>Sessions</strong>
        <div class="small">Active: ${sessions.active != null ? sessions.active : "-"}</div>
      </div>
      <div class="stack-note-card">
        <strong>Worker</strong> ${workerBadge}
        <div class="small">${escHtml(worker.reason || "-")}</div>
      </div>
      <div class="stack-note-card">
        <strong>Thumbnail queue</strong>
        <div class="small">${queueSummary}</div>
        <div class="small">${resultSummary}</div>
        <div class="small">Last success: ${thumbnail.last_success_at ? formatDate(thumbnail.last_success_at) : "-"}</div>
        <div class="small">Last failure: ${thumbnail.last_failure_at ? formatDate(thumbnail.last_failure_at) : "-"}</div>
      </div>
      <div class="stack-note-card ops-error-card">
        <strong>Recent errors (last 10)</strong>
        ${errorRows}
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

function nextSecurityPollDelay() {
  if (securityStatusFailureCount <= 0) return SECURITY_POLL_BASE_MS;
  const scaled = SECURITY_POLL_BASE_MS * Math.pow(2, securityStatusFailureCount - 1);
  return Math.min(scaled, SECURITY_POLL_MAX_MS);
}

function scheduleSecurityStatusPoll(delayMs = null) {
  window.clearTimeout(securityStatusTimer);
  securityStatusTimer = window.setTimeout(() => {
    loadSecurityStatus({ silent: true }).catch(() => {});
  }, delayMs == null ? nextSecurityPollDelay() : delayMs);
}

function securityFormHasFocus() {
  const form = document.getElementById("security-settings-form");
  return !!form && form.contains(document.activeElement);
}

function setSecurityFormValues(settings, force = false) {
  if (!force && securityFormHasFocus()) return;
  const rateLimit = document.getElementById("security-login-rate-limit");
  const rateWindow = document.getElementById("security-login-rate-window");
  const lockoutThreshold = document.getElementById("security-lockout-threshold");
  const lockoutDuration = document.getElementById("security-lockout-duration");
  if (rateLimit) rateLimit.value = settings.login_rate_limit || 5;
  if (rateWindow) rateWindow.value = settings.login_rate_limit_window_minutes || 15;
  if (lockoutThreshold) lockoutThreshold.value = settings.lockout_threshold || 10;
  if (lockoutDuration) lockoutDuration.value = settings.lockout_duration_minutes || 30;
}

function renderBlockedIps(blockedIps) {
  const el = document.getElementById("blocked-ips-table");
  if (!el) return;
  if (!blockedIps.length) {
    el.innerHTML = '<p class="small">No blocked IPs.</p>';
    return;
  }

  let html = '<div class="table-wrap"><table class="admin-table"><thead><tr><th>IP</th><th>Attempts</th><th>Remaining</th><th>Until</th><th>Action</th></tr></thead><tbody>';
  for (const item of blockedIps) {
    const ip = item.ip || "-";
    html += `<tr>
      <td><code>${escHtml(ip)}</code></td>
      <td>${item.attempts || 0}/${item.limit || "-"}</td>
      <td>${formatDuration(item.remaining_seconds || 0)}</td>
      <td>${item.blocked_until ? formatDate(item.blocked_until) : "-"}</td>
      <td><button type="button" class="secondary admin-inline-button" data-action="unblock-ip" data-ip="${escHtml(ip)}">Unblock</button></td>
    </tr>`;
  }
  html += "</tbody></table></div>";
  el.innerHTML = html;
}

async function loadSecurityStatus(options = {}) {
  if (securityStatusInFlight && !options.force) return;
  securityStatusInFlight = true;
  const tableEl = document.getElementById("blocked-ips-table");
  if (!tableEl) {
    securityStatusInFlight = false;
    return;
  }
  try {
    const resp = await fetch("/api/admin/security", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    securityStatusFailureCount = 0;
    setSecurityFormValues(data.settings || {}, !!options.force);
    renderBlockedIps(data.blocked_ips || []);
    scheduleSecurityStatusPoll(SECURITY_POLL_BASE_MS);
  } catch (err) {
    securityStatusFailureCount += 1;
    if (!options.silent) {
      tableEl.innerHTML = '<span class="small error">Failed to load security status: ' + err.message + "</span>";
    }
    scheduleSecurityStatusPoll(nextSecurityPollDelay());
  } finally {
    securityStatusInFlight = false;
  }
}

async function saveSecuritySettings(event) {
  event.preventDefault();
  const body = {
    login_rate_limit: parseInt(document.getElementById("security-login-rate-limit")?.value || "0", 10),
    login_rate_limit_window_minutes: parseInt(document.getElementById("security-login-rate-window")?.value || "0", 10),
    lockout_threshold: parseInt(document.getElementById("security-lockout-threshold")?.value || "0", 10),
    lockout_duration_minutes: parseInt(document.getElementById("security-lockout-duration")?.value || "0", 10),
  };

  try {
    const resp = await fetch("/api/admin/security/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    toast("Login security settings saved.", { tone: "success", title: "Security updated" });
    await loadSecurityStatus({ force: true });
  } catch (err) {
    toast("Save failed: " + err.message, { tone: "error", title: "Security update failed" });
  }
}

async function unblockIp(ip) {
  try {
    const resp = await fetch("/api/admin/security/blocked-ips/unblock", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify({ ip }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    toast("IP unblocked: " + ip, { tone: "success", title: "IP released" });
    await loadSecurityStatus({ force: true });
  } catch (err) {
    toast("Unblock failed: " + err.message, { tone: "error", title: "IP release failed" });
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
      <strong>App origin</strong>
      <div class="admin-actions" style="margin-top:8px;">
        <code>${escHtml(appOrigin)}</code>
        <button type="button" class="ghost copy-button admin-inline-button" data-copy-text="${escHtml(appOrigin)}">Copy</button>
      </div>
    </div>
    <div class="stack-note-card">
      <strong>VPN</strong>
      <div class="small">Reach the stack remotely through the WireGuard server bundled with the stack. The wg-easy admin UI is served at <code>/vpn/</code> on this host.</div>
    </div>
    <div class="stack-note-card">
      <strong>Recording storage</strong>
      <div class="small">Recordings and thumbnails are stored in the web container's local Docker volume.</div>
    </div>`;
  if (typeof initCopyButtons === "function") initCopyButtons();
}

loadUsers();
loadSessions();
loadSystemInfo();
loadOpsStatus({ force: true }).catch(() => {});
loadSecurityStatus({ force: true }).catch(() => {});

document.addEventListener("click", event => {
  const btn = event.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "save-role") saveRole(parseInt(btn.dataset.userId, 10));
  else if (action === "reset-pw") resetPassword(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "cancel-password-reset") closePasswordResetModal();
  else if (action === "delete-user") deleteUser(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "revoke-session") revokeSession(btn.dataset.sessionId);
  else if (action === "unblock-ip") unblockIp(btn.dataset.ip || "");
});

document.getElementById("create-user-form").addEventListener("submit", createUser);
document.getElementById("security-settings-form")?.addEventListener("submit", saveSecuritySettings);
if (passwordResetForm) passwordResetForm.addEventListener("submit", submitPasswordReset);

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
