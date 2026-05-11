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
  chrome.storage.local.get(["backendUrl", "apiToken", "recruiterEmail"], (data) => {
    $("backend").value = data.backendUrl || DEFAULT_BACKEND;
    $("token").value = data.apiToken || "";
    $("email").value = data.recruiterEmail || "";
  });
}

function save() {
  const backendUrl = ($("backend").value || DEFAULT_BACKEND).trim().replace(/\/+$/, "");
  const apiToken = ($("token").value || "").trim();
  const recruiterEmail = ($("email").value || "").trim();
  chrome.storage.local.set({ backendUrl, apiToken, recruiterEmail }, () => {
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
    // Calling /api/extension/score with a bogus numeric id is a safe
    // round-trip: 404 = backend reachable + token accepted.
    const resp = await fetch(`${backendUrl}/api/extension/score?numeric_id=__ping__`, {
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
    if (resp.status === 404) {
      setStatus("Connected — token accepted.", "ok");
      return;
    }
    if (resp.ok) {
      setStatus("Connected.", "ok");
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
