/* admin.js - Admin dashboard: users, sessions, ops status, and passkeys */

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

const OPS_POLL_BASE_MS = 10000;
const OPS_POLL_MAX_MS = 30000;
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
    const database = data.database || {};
    const queueSummary = `${thumbnail.pending_count || 0} pending | ${thumbnail.inflight_count || 0} inflight | ${thumbnail.active_tasks || 0} active tasks`;
    const resultSummary = `${thumbnail.completed_count || 0} completed | ${thumbnail.failed_count || 0} failed`;
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
        <strong>Recording storage</strong>
        <div class="small">Type: ${escHtml(storage.type || "local")}</div>
        <div class="small">Path: ${escHtml(storage.path || "-")}</div>
      </div>
      <div class="stack-note-card">
        <strong>Database</strong>
        <div class="small">Host: ${escHtml(database.host || "-")}:${database.port || "-"}</div>
        <div class="small">Database: ${escHtml(database.db || "-")}</div>
      </div>
      <div class="stack-note-card">
        <strong>Latest issues</strong>
        <div class="small">${escHtml(thumbnail.last_error || "No recent errors.")}</div>
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

document.addEventListener("click", event => {
  const btn = event.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "save-role") saveRole(parseInt(btn.dataset.userId, 10));
  else if (action === "reset-pw") resetPassword(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "cancel-password-reset") closePasswordResetModal();
  else if (action === "delete-user") deleteUser(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "revoke-session") revokeSession(btn.dataset.sessionId);
});

document.getElementById("create-user-form").addEventListener("submit", createUser);
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
