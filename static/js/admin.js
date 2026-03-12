/* admin.js – Admin dashboard: users, sessions, worker status, system info */

function getCsrf() {
  return document.cookie.match(/csrf_token=([^;]+)/)?.[1] || '';
}

function formatDate(ts) {
  if (!ts) return '–';
  return new Date(parseFloat(ts) * 1000).toLocaleString();
}

// ===== Worker Status =====
async function loadWorkerStatus() {
  const el = document.getElementById('worker-status');
  try {
    const resp = await fetch('/api/state', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    el.innerHTML = `
      <table class="admin-table">
        <tr><th>Preset</th><td>${data.preset || '–'}</td></tr>
        <tr><th>Detection</th><td>${data.det || '–'}</td></tr>
        <tr><th>FPS</th><td>${data.fps != null ? data.fps.toFixed(1) : '–'}</td></tr>
        <tr><th>Pose</th><td>${data.pose_enabled ? 'on' : 'off'}</td></tr>
        <tr><th>Inference</th><td>${data.inference_enabled ? 'on' : 'off'}</td></tr>
        <tr><th>Stream backend</th><td>${data.stream_backend || '–'}</td></tr>
        <tr><th>WebRTC available</th><td>${data.webrtc_available ? 'yes' : 'no'}</td></tr>
      </table>`;
  } catch (err) {
    el.innerHTML = '<span style="color:var(--danger)">Worker unreachable: ' + err.message + '</span>';
  }
}

async function adminCmd(cmd, label) {
  try {
    const resp = await fetch('/api/cmd', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
      body: JSON.stringify({ cmd })
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    setTimeout(loadWorkerStatus, 600);
  } catch (err) {
    alert('Command failed: ' + err.message);
  }
}

// ===== Users =====
async function loadUsers() {
  const el = document.getElementById('users-table');
  try {
    const resp = await fetch('/api/admin/users', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const users = await resp.json();

    if (!users.length) { el.innerHTML = '<p style="color:var(--muted)">No users.</p>'; return; }

    let html = '<table class="admin-table"><thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Last Login</th><th>Actions</th></tr></thead><tbody>';
    for (const u of users) {
      const locked = u.locked_until && u.locked_until > Date.now() / 1000;
      html += `<tr>
        <td>${u.id}</td>
        <td>${escHtml(u.username)}${locked ? ' 🔒' : ''}</td>
        <td>
          <select data-user-id="${u.id}" data-type="role" class="role-select"
            style="padding:4px 8px;font:inherit;border-radius:6px;border:1px solid var(--border);background:var(--panel-2);color:var(--text);">
            <option value="viewer"${u.role === 'viewer' ? ' selected' : ''}>Viewer</option>
            <option value="admin"${u.role === 'admin' ? ' selected' : ''}>Admin</option>
          </select>
        </td>
        <td>${formatDate(u.last_login)}</td>
        <td>
          <button data-action="save-role" data-user-id="${u.id}" class="secondary" style="padding:4px 8px;font-size:0.8rem;">Save Role</button>
          <button data-action="reset-pw" data-user-id="${u.id}" data-username="${escHtml(u.username)}" class="secondary" style="padding:4px 8px;font-size:0.8rem;">Reset PW</button>
          <button data-action="delete-user" data-user-id="${u.id}" data-username="${escHtml(u.username)}" class="danger" style="padding:4px 8px;font-size:0.8rem;">Delete</button>
        </td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = '<span style="color:var(--danger)">Failed: ' + err.message + '</span>';
  }
}

function escHtml(str) {
  return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function saveRole(userId) {
  const select = document.querySelector(`select[data-user-id="${userId}"]`);
  if (!select) return;
  const role = select.value;
  try {
    const resp = await fetch('/api/admin/users/' + userId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
      body: JSON.stringify({ role })
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'HTTP ' + resp.status);
    }
    await loadUsers();
  } catch (err) {
    alert('Save role failed: ' + err.message);
  }
}

async function resetPassword(userId, username) {
  const password = prompt(`New password for "${username}" (min 12 chars):`);
  if (!password) return;
  if (password.length < 12) { alert('Password too short (min 12 chars)'); return; }
  try {
    const resp = await fetch('/api/admin/users/' + userId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
      body: JSON.stringify({ password })
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'HTTP ' + resp.status);
    }
    alert('Password updated.');
  } catch (err) {
    alert('Reset failed: ' + err.message);
  }
}

async function deleteUser(userId, username) {
  if (!confirm(`Delete user "${username}"? This is irreversible.`)) return;
  try {
    const resp = await fetch('/api/admin/users/' + userId, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': getCsrf() }
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'HTTP ' + resp.status);
    }
    await loadUsers();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

async function createUser(event) {
  event.preventDefault();
  const username = document.getElementById('new-username').value.trim();
  const password = document.getElementById('new-password').value;
  const role = document.getElementById('new-role').value;
  if (!username || !password) return;
  if (password.length < 12) { alert('Password too short (min 12 chars)'); return; }
  try {
    const resp = await fetch('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
      body: JSON.stringify({ username, password, role })
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'HTTP ' + resp.status);
    }
    document.getElementById('new-username').value = '';
    document.getElementById('new-password').value = '';
    await loadUsers();
  } catch (err) {
    alert('Create user failed: ' + err.message);
  }
}

// ===== Sessions =====
async function loadSessions() {
  const el = document.getElementById('sessions-table');
  try {
    const resp = await fetch('/api/admin/sessions', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const sessions = await resp.json();

    if (!sessions.length) { el.innerHTML = '<p style="color:var(--muted)">No active sessions.</p>'; return; }

    let html = '<table class="admin-table"><thead><tr><th>User</th><th>IP</th><th>Expires</th><th>Action</th></tr></thead><tbody>';
    for (const s of sessions) {
      html += `<tr>
        <td>${escHtml(s.username)}</td>
        <td>${escHtml(s.ip_address || '–')}</td>
        <td>${formatDate(s.expires_at)}</td>
        <td><button data-action="revoke-session" data-session-id="${s.id}" class="danger" style="padding:4px 8px;font-size:0.8rem;">Revoke</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = '<span style="color:var(--danger)">Failed: ' + err.message + '</span>';
  }
}

async function revokeSession(sessionId) {
  if (!confirm('Revoke this session?')) return;
  try {
    const resp = await fetch('/api/admin/sessions/' + sessionId, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': getCsrf() }
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'HTTP ' + resp.status);
    }
    await loadSessions();
  } catch (err) {
    alert('Revoke failed: ' + err.message);
  }
}

// ===== System Info =====
function loadSystemInfo() {
  const el = document.getElementById('system-info');
  el.innerHTML = `
    <table class="admin-table">
      <tr><th>User Agent</th><td style="word-break:break-all;font-size:0.8rem">${escHtml(navigator.userAgent)}</td></tr>
      <tr><th>Time</th><td>${new Date().toLocaleString()}</td></tr>
    </table>`;
}

// ===== Passkeys =====
async function loadPasskeys() {
  const el = document.getElementById('passkeys-list');
  if (!el) return;
  try {
    const resp = await fetch('/auth/webauthn/credentials', { cache: 'no-store' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const keys = await resp.json();
    if (!keys.length) { el.innerHTML = '<p style="color:var(--muted)">No passkeys registered yet.</p>'; return; }
    let html = '<table class="admin-table"><thead><tr><th>Name</th><th>Sign Count</th><th>Registered</th><th>Action</th></tr></thead><tbody>';
    for (const k of keys) {
      html += `<tr>
        <td>${escHtml(k.name)}</td>
        <td>${k.sign_count}</td>
        <td>${formatDate(k.created_at)}</td>
        <td><button data-action="delete-passkey" data-cred-id="${k.id}" data-name="${escHtml(k.name)}" class="danger" style="padding:4px 8px;font-size:0.8rem;">Delete</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = '<span style="color:var(--danger)">Failed: ' + err.message + '</span>';
  }
}

async function registerPasskey() {
  const btn = document.getElementById('register-passkey-btn');
  if (btn) btn.disabled = true;
  try {
    // 1. Get registration options from server
    const beginResp = await fetch('/auth/webauthn/register/begin', {
      method: 'POST',
      headers: { 'X-CSRF-Token': getCsrf() }
    });
    if (!beginResp.ok) throw new Error('Failed to start registration');
    const options = await beginResp.json();

    // 2. Decode challenge and user.id from base64url
    options.challenge = base64urlToBuffer(options.challenge);
    options.user.id = base64urlToBuffer(options.user.id);
    if (options.excludeCredentials) {
      options.excludeCredentials = options.excludeCredentials.map(c => ({
        ...c, id: base64urlToBuffer(c.id)
      }));
    }

    // 3. Prompt browser credential creation
    const credential = await navigator.credentials.create({ publicKey: options });

    // 4. Encode response for server
    const attestation = {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      response: {
        attestationObject: bufferToBase64url(credential.response.attestationObject),
        clientDataJSON: bufferToBase64url(credential.response.clientDataJSON)
      }
    };

    const name = prompt('Name for this passkey:', 'My Passkey');
    if (name) attestation.name = name;

    // 5. Complete registration
    const completeResp = await fetch('/auth/webauthn/register/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
      body: JSON.stringify(attestation)
    });
    if (!completeResp.ok) {
      const err = await completeResp.json().catch(() => ({}));
      throw new Error(err.detail || 'Registration failed');
    }
    await loadPasskeys();
  } catch (err) {
    if (err.name !== 'AbortError') alert('Passkey registration failed: ' + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deletePasskey(credId, name) {
  if (!confirm(`Delete passkey "${name}"?`)) return;
  try {
    const resp = await fetch('/auth/webauthn/credentials/' + credId, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': getCsrf() }
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    await loadPasskeys();
  } catch (err) {
    alert('Delete failed: ' + err.message);
  }
}

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

// ===== Init =====
loadWorkerStatus();
loadUsers();
loadSessions();
loadSystemInfo();
loadPasskeys();

// ===== Event delegation =====
document.addEventListener('click', event => {
  const btn = event.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === 'admin-cmd') adminCmd(btn.dataset.cmd, btn.dataset.label);
  else if (action === 'save-role') saveRole(parseInt(btn.dataset.userId));
  else if (action === 'reset-pw') resetPassword(parseInt(btn.dataset.userId), btn.dataset.username);
  else if (action === 'delete-user') deleteUser(parseInt(btn.dataset.userId), btn.dataset.username);
  else if (action === 'revoke-session') revokeSession(btn.dataset.sessionId);
  else if (action === 'delete-passkey') deletePasskey(parseInt(btn.dataset.credId), btn.dataset.name);
});

document.getElementById('create-user-form').addEventListener('submit', createUser);

const regBtn = document.getElementById('register-passkey-btn');
if (regBtn) regBtn.addEventListener('click', registerPasskey);

// Poll worker status every 2 seconds
setInterval(loadWorkerStatus, 2000);
