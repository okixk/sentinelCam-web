/* auth.js – login form + WebAuthn passkey support */

function getCsrf() {
  return document.cookie.match(/csrf_token=([^;]+)/)?.[1] || '';
}

function showError(msg) {
  const el = document.getElementById('error-msg');
  if (el) { el.textContent = msg; el.style.display = 'block'; }
}

function hideError() {
  const el = document.getElementById('error-msg');
  if (el) el.style.display = 'none';
}

// ===== Password login =====
const loginForm = document.getElementById('login-form');
if (loginForm) {
  loginForm.addEventListener('submit', async e => {
    e.preventDefault();
    hideError();
    const btn = document.getElementById('login-btn');
    btn.disabled = true;
    btn.textContent = 'Logging in…';
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    try {
      const resp = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      const data = await resp.json();
      if (data.ok) {
        window.location.href = data.redirect || '/';
      } else {
        showError(data.error || 'Login failed');
        btn.disabled = false;
        btn.textContent = 'Login';
      }
    } catch (err) {
      showError('Network error: ' + err.message);
      btn.disabled = false;
      btn.textContent = 'Login';
    }
  });
}

// ===== WebAuthn passkey =====
function base64urlDecode(str) {
  str = str.replace(/-/g, '+').replace(/_/g, '/');
  while (str.length % 4) str += '=';
  const binary = atob(str);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

function arrayBufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let str = '';
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

function prepareCredential(credential) {
  const clientDataJSON = arrayBufferToBase64url(credential.response.clientDataJSON);
  const authenticatorData = arrayBufferToBase64url(credential.response.authenticatorData);
  const signature = arrayBufferToBase64url(credential.response.signature);
  const userHandle = credential.response.userHandle ? arrayBufferToBase64url(credential.response.userHandle) : null;
  return {
    id: credential.id,
    rawId: arrayBufferToBase64url(credential.rawId),
    response: { clientDataJSON, authenticatorData, signature, userHandle },
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {}
  };
}

function decodeOptions(options) {
  if (options.challenge && typeof options.challenge === 'string') {
    options.challenge = base64urlDecode(options.challenge);
  }
  if (Array.isArray(options.allowCredentials)) {
    options.allowCredentials = options.allowCredentials.map(c => ({
      ...c,
      id: typeof c.id === 'string' ? base64urlDecode(c.id) : c.id
    }));
  }
  return options;
}

async function passkeyLogin() {
  hideError();
  const username = document.getElementById('username').value.trim();
  if (!username) { showError('Enter your username first'); return; }

  const btn = document.getElementById('passkey-btn');
  btn.disabled = true;
  btn.textContent = '⌛ Waiting for authenticator…';

  try {
    // Begin
    const beginResp = await fetch('/auth/webauthn/login/begin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username })
    });
    if (!beginResp.ok) {
      const data = await beginResp.json().catch(() => ({}));
      throw new Error(data.detail || data.error || 'No passkeys for this user');
    }
    const options = await beginResp.json();
    decodeOptions(options);

    // Browser prompt
    const credential = await navigator.credentials.get({ publicKey: options });

    // Complete
    const completeResp = await fetch('/auth/webauthn/login/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, credential: prepareCredential(credential) })
    });
    const result = await completeResp.json();
    if (result.ok) {
      window.location.href = result.redirect || '/';
    } else {
      throw new Error(result.error || 'Authentication failed');
    }
  } catch (err) {
    showError('Passkey error: ' + err.message);
    btn.disabled = false;
    btn.textContent = '🔑 Sign in with Passkey';
  }
}

// Show passkey button only if WebAuthn is supported
if (typeof window.PublicKeyCredential !== 'undefined') {
  const section = document.getElementById('webauthn-section');
  if (section) section.style.display = 'block';
  const btn = document.getElementById('passkey-btn');
  if (btn) btn.addEventListener('click', passkeyLogin);
}
