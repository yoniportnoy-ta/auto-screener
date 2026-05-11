/**
 * Popup script — small settings panel for the Auto Screener extension.
 * Persists backend URL + API token + recruiter email to chrome.storage.local.
 */

const DEFAULT_BACKEND = "https://auto-screener-2va5.onrender.com";

const $ = (id) => document.getElementById(id);

function setStatus(msg, kind) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = "status" + (kind ? " " + kind : "");
}

function load() {
  chrome.storage.local.get(["backendUrl", "apiToken"], (data) => {
    $("backend").value = data.backendUrl || DEFAULT_BACKEND;
    $("token").value = data.apiToken || "";
  });
}

function save() {
  const backendUrl = ($("backend").value || DEFAULT_BACKEND).trim().replace(/\/+$/, "");
  const apiToken = ($("token").value || "").trim();
  chrome.storage.local.set({ backendUrl, apiToken }, () => {
    setStatus("Saved.", "ok");
    setTimeout(() => setStatus(""), 1500);
  });
}

async function test() {
  const backendUrl = ($("backend").value || DEFAULT_BACKEND).trim().replace(/\/+$/, "");
  const apiToken = ($("token").value || "").trim();
  if (!apiToken) {
    setStatus("Enter a token first.", "err");
    return;
  }
  setStatus("Testing…");
  try {
    // /api/extension/ping is a cheap token-gated round-trip. It never touches
    // Comeet, so it returns fast regardless of backend load.
    const resp = await fetch(`${backendUrl}/api/extension/ping`, {
      method: "GET",
      headers: { "X-Screener-Token": apiToken },
    });
    if (resp.status === 401) {
      setStatus("Token rejected (401).", "err");
      return;
    }
    if (resp.status === 503) {
      setStatus("Server has no token configured (503).", "err");
      return;
    }
    if (resp.ok) {
      setStatus("Connected — token accepted.", "ok");
      return;
    }
    setStatus(`Backend returned ${resp.status}.`, "err");
  } catch (e) {
    setStatus(`Network error: ${e.message || e}`, "err");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  load();
  $("save").addEventListener("click", save);
  $("test").addEventListener("click", test);
});
