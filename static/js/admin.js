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

async function loadWorkerStatus() {
  const el = document.getElementById("worker-status");
  try {
    const resp = await fetch("/api/state", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
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
  } catch (err) {
    el.innerHTML = '<span class="small error">Worker unreachable: ' + err.message + "</span>";
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
    window.setTimeout(loadWorkerStatus, 600);
  } catch (err) {
    alert("Command failed: " + err.message);
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
    await loadUsers();
  } catch (err) {
    alert("Save role failed: " + err.message);
  }
}

async function resetPassword(userId, username) {
  const password = prompt(`New password for "${username}" (min 12 chars):`);
  if (!password) return;
  if (password.length < 12) {
    alert("Password too short (min 12 chars)");
    return;
  }
  try {
    const resp = await fetch("/api/admin/users/" + userId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify({ password })
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    alert("Password updated.");
  } catch (err) {
    alert("Reset failed: " + err.message);
  }
}

async function deleteUser(userId, username) {
  if (!confirm(`Delete user "${username}"? This is irreversible.`)) return;
  try {
    const resp = await fetch("/api/admin/users/" + userId, {
      method: "DELETE",
      headers: { "X-CSRF-Token": getCsrf() }
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    await loadUsers();
  } catch (err) {
    alert("Delete failed: " + err.message);
  }
}

async function createUser(event) {
  event.preventDefault();
  const username = document.getElementById("new-username").value.trim();
  const password = document.getElementById("new-password").value;
  const role = document.getElementById("new-role").value;
  if (!username || !password) return;
  if (password.length < 12) {
    alert("Password too short (min 12 chars)");
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
    await loadUsers();
  } catch (err) {
    alert("Create user failed: " + err.message);
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
  if (!confirm("Revoke this session?")) return;
  try {
    const resp = await fetch("/api/admin/sessions/" + sessionId, {
      method: "DELETE",
      headers: { "X-CSRF-Token": getCsrf() }
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || "HTTP " + resp.status);
    }
    await loadSessions();
  } catch (err) {
    alert("Revoke failed: " + err.message);
  }
}

function loadSystemInfo() {
  const el = document.getElementById("system-info");
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
      <strong>Windows local worker</strong>
      <div class="admin-actions" style="margin-top:8px;">
        <code>.\\scripts\\start-local-worker.ps1</code>
        <button type="button" class="ghost copy-button admin-inline-button" data-copy-text=".\\scripts\\start-local-worker.ps1">Copy</button>
      </div>
    </div>
    <div class="stack-note-card">
      <strong>Linux local worker</strong>
      <div class="admin-actions" style="margin-top:8px;">
        <code>bash ./scripts/start-local-worker.sh</code>
        <button type="button" class="ghost copy-button admin-inline-button" data-copy-text="bash ./scripts/start-local-worker.sh">Copy</button>
      </div>
    </div>
    <div class="stack-note-card">
      <strong>Linux Docker worker</strong>
      <div class="admin-actions" style="margin-top:8px;">
        <code>bash ./scripts/start-docker-worker.sh</code>
        <button type="button" class="ghost copy-button admin-inline-button" data-copy-text="bash ./scripts/start-docker-worker.sh">Copy</button>
      </div>
      <span class="small">Use WORKER_VIDEO_DEVICE or WORKER_SOURCE in .env when you need a different input.</span>
    </div>
    <div class="stack-note-card">
      <strong>Current time</strong>
      <span class="small">${new Date().toLocaleString()}</span>
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

    const name = prompt("Name for this passkey:", "My Passkey");
    if (name) attestation.name = name;

    const completeResp = await fetch("/auth/webauthn/register/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": getCsrf() },
      body: JSON.stringify(attestation)
    });
    if (!completeResp.ok) {
      const err = await completeResp.json().catch(() => ({}));
      throw new Error(err.detail || "Registration failed");
    }

    await loadPasskeys();
  } catch (err) {
    if (err.name !== "AbortError") alert("Passkey registration failed: " + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deletePasskey(credId, name) {
  if (!confirm(`Delete passkey "${name}"?`)) return;
  try {
    const resp = await fetch("/auth/webauthn/credentials/" + credId, {
      method: "DELETE",
      headers: { "X-CSRF-Token": getCsrf() }
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    await loadPasskeys();
  } catch (err) {
    alert("Delete failed: " + err.message);
  }
}

loadWorkerStatus();
loadUsers();
loadSessions();
loadSystemInfo();
loadPasskeys();

document.addEventListener("click", event => {
  const btn = event.target.closest("[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "admin-cmd") adminCmd(btn.dataset.cmd);
  else if (action === "save-role") saveRole(parseInt(btn.dataset.userId, 10));
  else if (action === "reset-pw") resetPassword(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "delete-user") deleteUser(parseInt(btn.dataset.userId, 10), btn.dataset.username);
  else if (action === "revoke-session") revokeSession(btn.dataset.sessionId);
  else if (action === "delete-passkey") deletePasskey(parseInt(btn.dataset.credId, 10), btn.dataset.name);
});

document.getElementById("create-user-form").addEventListener("submit", createUser);

const registerBtn = document.getElementById("register-passkey-btn");
if (registerBtn) registerBtn.addEventListener("click", registerPasskey);

window.setInterval(loadWorkerStatus, 2000);
