"use strict";

const $ = (id) => document.getElementById(id);

// Storage keys carried by older builds. Removed on load so they don't linger
// in browser.storage.local forever after the migration to server-side scoring.
const DEPRECATED_KEYS = [
  "apiKey", "resume", "resumePath", "resumeLoadedAt", "growthKeywords",
];

// Hosts already covered by the static `host_permissions` in manifest.json —
// no runtime grant needed (or possible). Kept in sync with the manifest;
// don't add the cluster hostname here, that goes through optional perms.
function isStaticallyPermittedHost(url) {
  try {
    const h = new URL(url).hostname;
    return h === "127.0.0.1" || h === "localhost";
  } catch {
    return false;
  }
}

// Ensure the extension can fetch from the given backend origin.
//
// Firefox enforces that `browser.permissions.request()` is called from a
// user-gesture context; any `await` between the gesture and the request
// invalidates that. So the function does all setup synchronously and uses
// `request()` as its only async call. We skip `permissions.contains()`
// entirely — Firefox's request() is a silent no-op if the permission is
// already granted (saving the extra await that broke the previous version).
async function ensureBackendPermission(url) {
  let origin;
  try { origin = new URL(url).origin; } catch { return { ok: false, reason: "invalid URL" }; }
  if (isStaticallyPermittedHost(url)) return { ok: true };
  const pattern = `${origin}/*`;
  try {
    const granted = await browser.permissions.request({ origins: [pattern] });
    return granted
      ? { ok: true }
      : { ok: false, reason: "permission denied — extension can't reach this URL until granted" };
  } catch (err) {
    return { ok: false, reason: `permissions error: ${err.message}` };
  }
}

async function load() {
  // Best-effort cleanup of pre-server-side-scoring storage keys. Safe even
  // if some keys were never set.
  try { await browser.storage.local.remove(DEPRECATED_KEYS); } catch { /* ignore */ }

  const { backendUrl } = await browser.storage.local.get(["backendUrl"]);
  if (backendUrl) $("backend-url").value = backendUrl;
}

async function save() {
  const backendUrl = $("backend-url").value.trim().replace(/\/$/, "");
  const status = $("save-status");

  if (!backendUrl) {
    status.classList.add("error");
    status.textContent = "Backend URL is required.";
    return;
  }

  // For non-localhost backends, prompt the user to grant cross-origin
  // permission before saving. Without it, the popup and the score POST
  // would silently fail with a CORS error.
  const perm = await ensureBackendPermission(backendUrl);
  if (!perm.ok) {
    status.classList.add("error");
    status.textContent = `Backend URL not saved: ${perm.reason}.`;
    return;
  }

  await browser.storage.local.set({ backendUrl });
  status.classList.remove("error");
  status.textContent = "Saved.";
  setTimeout(() => (status.textContent = ""), 2000);
}

async function testBackend() {
  const url = $("backend-url").value.trim().replace(/\/$/, "");
  const status = $("backend-status");
  status.classList.remove("error");
  if (!url) {
    status.classList.add("error");
    status.textContent = "Set a backend URL first.";
    return;
  }
  // Request permission first if needed — same gesture-required pattern as save().
  const perm = await ensureBackendPermission(url);
  if (!perm.ok) {
    status.classList.add("error");
    status.textContent = `Can't reach ${url}: ${perm.reason}.`;
    return;
  }
  status.textContent = "Pinging…";
  try {
    const r = await fetch(`${url}/jobs?status=open`, { method: "GET" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const list = await r.json();
    status.textContent = `Reachable. Currently ${list.length} open jobs in inbox.`;
  } catch (err) {
    status.classList.add("error");
    status.textContent = `Couldn't reach ${url}: ${err.message}`;
  }
}

$("save").addEventListener("click", save);
$("test-backend").addEventListener("click", testBackend);

load();
