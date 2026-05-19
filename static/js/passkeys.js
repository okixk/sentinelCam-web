/* passkeys.js - WebAuthn passkey registration / deletion shared between pages */

(function () {
  if (window.__sentinelPasskeysLoaded) return;
  window.__sentinelPasskeysLoaded = true;

  const csrf = () => document.cookie.match(/csrf_token=([^;]+)/)?.[1] || "";
  const fmtDate = ts => (ts ? new Date(parseFloat(ts) * 1000).toLocaleString() : "-");
  const esc = s => String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  const appUI = window.AppUI || {};
  const toast = typeof appUI.toast === "function" ? appUI.toast : () => null;
  const confirmDialog = typeof appUI.confirm === "function" ? appUI.confirm : async () => false;
  const promptDialog = typeof appUI.prompt === "function" ? appUI.prompt : async () => null;

  function b64uToBuffer(b64url) {
    const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
    const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
    const bin = atob(b64 + pad);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return arr.buffer;
  }

  function bufferToB64u(buffer) {
    const bytes = new Uint8Array(buffer);
    let str = "";
    for (const b of bytes) str += String.fromCharCode(b);
    return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function loadList() {
    const el = document.getElementById("passkeys-list");
    if (!el) return;
    try {
      const resp = await fetch("/auth/webauthn/credentials", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const keys = await resp.json();
      if (!keys.length) {
        el.innerHTML = '<span class="small">No passkeys registered yet.</span>';
        return;
      }
      if (el.classList && el.classList.contains("inline-list")) {
        el.innerHTML = keys.map(k => `
          <div class="passkey-item">
            <span>${esc(k.name)}</span>
            <button data-action="delete-passkey" data-cred-id="${k.id}" data-name="${esc(k.name)}" class="danger admin-inline-button">Delete</button>
          </div>`).join("");
        return;
      }
      el.innerHTML = `<div class="table-wrap"><table class="admin-table"><thead><tr><th>Name</th><th>Sign count</th><th>Registered</th><th>Action</th></tr></thead><tbody>${
        keys.map(k => `<tr>
          <td>${esc(k.name)}</td>
          <td>${k.sign_count}</td>
          <td>${fmtDate(k.created_at)}</td>
          <td><button data-action="delete-passkey" data-cred-id="${k.id}" data-name="${esc(k.name)}" class="danger admin-inline-button">Delete</button></td>
        </tr>`).join("")
      }</tbody></table></div>`;
    } catch (err) {
      el.innerHTML = '<span class="small error">Failed: ' + err.message + "</span>";
    }
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
        cancelLabel: "Cancel",
      });
      if (name === null) return;

      const beginResp = await fetch("/auth/webauthn/register/begin", {
        method: "POST",
        headers: { "X-CSRF-Token": csrf() },
      });
      if (!beginResp.ok) throw new Error("Failed to start registration");
      const options = await beginResp.json();

      options.challenge = b64uToBuffer(options.challenge);
      options.user.id = b64uToBuffer(options.user.id);
      if (options.excludeCredentials) {
        options.excludeCredentials = options.excludeCredentials.map(c => ({ ...c, id: b64uToBuffer(c.id) }));
      }

      const credential = await navigator.credentials.create({ publicKey: options });
      const attestation = {
        id: credential.id,
        rawId: bufferToB64u(credential.rawId),
        type: credential.type,
        response: {
          attestationObject: bufferToB64u(credential.response.attestationObject),
          clientDataJSON: bufferToB64u(credential.response.clientDataJSON),
        },
        name,
      };

      const completeResp = await fetch("/auth/webauthn/register/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify(attestation),
      });
      if (!completeResp.ok) {
        const err = await completeResp.json().catch(() => ({}));
        throw new Error(err.detail || "Registration failed");
      }

      toast(`Passkey "${name}" registered.`, { tone: "success", title: "Passkey added" });
      await loadList();
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
      tone: "danger",
    });
    if (!confirmed) return;
    try {
      const resp = await fetch("/auth/webauthn/credentials/" + credId, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrf() },
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      toast(`Passkey "${name}" deleted.`, { tone: "success", title: "Passkey removed" });
      await loadList();
    } catch (err) {
      toast("Delete failed: " + err.message, { tone: "error", title: "Passkey delete failed" });
    }
  }

  loadList();
  const regBtn = document.getElementById("register-passkey-btn");
  if (regBtn) regBtn.addEventListener("click", registerPasskey);
  document.addEventListener("click", evt => {
    const btn = evt.target.closest("[data-action='delete-passkey']");
    if (!btn) return;
    deletePasskey(parseInt(btn.dataset.credId, 10), btn.dataset.name);
  });

  window.SentinelPasskeys = { loadList, registerPasskey, deletePasskey };
})();
