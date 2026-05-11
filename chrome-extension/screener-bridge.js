/**
 * Bridge content script for the main Auto Screener web UI.
 *
 * Sole job: announce to the page that the extension is installed and what
 * version it's running. The page listens for this and shows a "✓ installed"
 * indicator next to the install instructions, so recruiters can confirm
 * at a glance that their Chrome has the extension loaded (and which version).
 *
 * Messages are scoped to `source: "auto-screener-extension"` so the page
 * can distinguish them from unrelated postMessage traffic.
 */

(() => {
  "use strict";
  try {
    const manifest = chrome.runtime.getManifest();
    const payload = {
      source: "auto-screener-extension",
      type: "hello",
      version: manifest.version || "0.0.0",
      manifestVersion: manifest.manifest_version,
    };

    function broadcast() {
      try {
        window.postMessage(payload, window.location.origin);
      } catch (_) { /* ignore */ }
    }

    // Announce on load and also a couple of times after, because the page's
    // listener might not be wired up at document_idle (depending on render
    // timing of the recruiter UI).
    broadcast();
    setTimeout(broadcast, 250);
    setTimeout(broadcast, 1000);

    // Reply to explicit "are you there?" pings from the page.
    window.addEventListener("message", (event) => {
      if (event.source !== window) return;
      const data = event.data;
      if (data && data.source === "auto-screener-page" && data.type === "ping") {
        broadcast();
      }
    });
  } catch (_) {
    // No-op — if anything throws here we silently fall back to the "not detected" state.
  }
})();
