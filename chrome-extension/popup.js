/**
 * Popup script — minimal settings UI.
 *
 * Backend URL and API token are auto-injected by the server at zip-download
 * time (see _token.js), so the popup normally just shows a Test connection
 * button. Advanced section is hidden behind a toggle for the cases where a
 * recruiter needs to point at a different backend.
 */

const EMBEDDED_BACKEND = (typeof window !== "undefined" && window.AS_EMBEDDED_BACKEND) || "";
const EMBEDDED_TOKEN   = (typeof window !== "undefined" && window.AS_EMBEDDED_TOKEN)   || "";
const DEFAULT_BACKEND  = EMBEDDED_BACKEND || "https://auto-screener-2va5.onrender.com";

const $ = (id) => document.getElementById(id);

function setStatus(msg, kind) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = "status" + (kind ? " " + kind : "");
}

function load() {
  chrome.storage.local.get(["backendUrl", "apiToken"], (data) => {
    const backend = data.backendUrl || DEFAULT_BACKEND;
    const token   = data.apiToken   || EMBEDDED_TOKEN || "";
    $("backend").value = backend;
    $("token").value   = token;
    if (!data.apiToken && EMBEDDED_TOKEN) {
      chrome.storage.local.set({ apiToken: EMBEDDED_TOKEN, backendUrl: backend });
    }
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
  const apiToken   = ($("token").value || "").trim();
  if (!apiToken) {
    setStatus("Not configured.", "err");
    return;
  }
  setStatus("Testing…");
  try {
    const resp = await fetch(`${backendUrl}/api/extension/ping`, {
      method: "GET",
      headers: { "X-Screener-Token": apiToken },
    });
    if (resp.status === 401) return setStatus("Token rejected.", "err");
    if (resp.status === 503) return setStatus("Backend misconfigured.", "err");
    if (resp.ok)             return setStatus("Connected ✓", "ok");
    setStatus(`Error ${resp.status}.`, "err");
  } catch (e) {
    setStatus(`Network error.`, "err");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  load();
  $("test").addEventListener("click", test);
  $("save").addEventListener("click", save);
  $("btnAdvanced").addEventListener("click", () => {
    const adv = $("advanced");
    const show = adv.style.display === "none";
    adv.style.display = show ? "block" : "none";
    $("btnAdvanced").textContent = show ? "Advanced ▴" : "Advanced ▾";
  });
});
