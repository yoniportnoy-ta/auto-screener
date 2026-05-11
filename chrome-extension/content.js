/**
 * Auto Screener content script.
 *
 * Runs on https://app.comeet.co/app/req/<position>/can/<numeric_candidate_id>.
 * Pulls the AI score for the candidate from our backend, injects a side panel
 * that shows rating/summary/strengths/gaps, and lets the recruiter submit
 * feedback (1–5 stars + free-text note).
 *
 * Comeet is a SPA, so we re-render whenever the URL changes.
 */

(() => {
  "use strict";

  const PANEL_ID = "auto-screener-panel";
  const COLLAPSED_KEY = "as_panel_collapsed";

  // ─── State ──────────────────────────────────────────────────────────────
  let currentNumericId = null;
  let currentScore = null;        // last response from /api/extension/score
  let selectedRating = 0;         // recruiter rating in the feedback widget
  let submitting = false;

  // ─── Settings loader ────────────────────────────────────────────────────
  function loadSettings() {
    return new Promise((resolve) => {
      chrome.storage.local.get(["backendUrl", "apiToken"], (data) => {
        resolve({
          backendUrl: (data.backendUrl || "https://auto-screener-2va5.onrender.com").replace(/\/+$/, ""),
          apiToken: data.apiToken || "",
        });
      });
    });
  }

  // ─── Backend calls ──────────────────────────────────────────────────────
  async function fetchScore(numericId) {
    const { backendUrl, apiToken } = await loadSettings();
    if (!apiToken) {
      const err = new Error("Auto Screener: API token not set. Open the extension popup to configure it.");
      err.code = "no_token";
      throw err;
    }
    const url = `${backendUrl}/api/extension/score?numeric_id=${encodeURIComponent(numericId)}`;
    const resp = await fetch(url, {
      method: "GET",
      headers: { "X-Screener-Token": apiToken },
    });
    if (resp.status === 404) {
      return null; // never scored yet
    }
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`Score fetch failed (${resp.status}): ${text || resp.statusText}`);
    }
    return resp.json();
  }

  async function scoreNow(positionUid, numericId) {
    const { backendUrl, apiToken } = await loadSettings();
    if (!apiToken) {
      const err = new Error("Auto Screener: API token not set. Open the extension popup to configure it.");
      err.code = "no_token";
      throw err;
    }
    const resp = await fetch(`${backendUrl}/api/extension/score-now`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Screener-Token": apiToken,
      },
      body: JSON.stringify({ position_uid: positionUid, numeric_id: numericId }),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      let detail = text;
      try { detail = JSON.parse(text).detail || text; } catch (_) {}
      throw new Error(`Scan failed (${resp.status}): ${detail || resp.statusText}`);
    }
    return resp.json();
  }

  async function postFeedback(body) {
    const { backendUrl, apiToken } = await loadSettings();
    const resp = await fetch(`${backendUrl}/api/extension/feedback`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Screener-Token": apiToken,
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`Feedback POST failed (${resp.status}): ${text || resp.statusText}`);
    }
    return resp.json();
  }

  // ─── URL helpers ────────────────────────────────────────────────────────
  function extractIdsFromUrl() {
    // Pattern: /app/req/<positionUid>/can/<numericId>(/...)?
    const m = location.pathname.match(/\/app\/req\/([^/]+)\/can\/(\d+)/);
    if (!m) return null;
    return { positionUid: m[1], numericId: m[2] };
  }

  function extractCandidateNameFromDom() {
    // Best-effort: Comeet renders the candidate name in an h1/h2 at the top of the page.
    // We try a few selectors; fall back to empty string.
    const selectors = [
      "h1.candidate-name",
      "h1[class*='candidate']",
      "h2[class*='candidate']",
      "header h1",
      "h1",
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent && el.textContent.trim().length < 120) {
        return el.textContent.trim();
      }
    }
    return "";
  }

  function extractPositionNameFromDom() {
    // Comeet typically shows the position name in a breadcrumb or page subtitle.
    const selectors = [
      "[class*='position-name']",
      "[class*='req-title']",
      "[class*='breadcrumb'] a",
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent) {
        const t = el.textContent.trim();
        if (t && t.length < 200) return t;
      }
    }
    return "";
  }

  // ─── DOM building ───────────────────────────────────────────────────────
  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;

    panel = document.createElement("div");
    panel.id = PANEL_ID;

    const collapsed = localStorage.getItem(COLLAPSED_KEY) === "1";
    if (collapsed) panel.classList.add("as-collapsed");

    panel.innerHTML = `
      <div class="as-header">
        <span class="as-title">AI Screener</span>
        <button class="as-toggle" type="button" title="Collapse">${collapsed ? "▸" : "▾"}</button>
      </div>
      <div class="as-body" id="as-body"></div>
    `;

    document.body.appendChild(panel);

    const toggleBtn = panel.querySelector(".as-toggle");
    const header = panel.querySelector(".as-header");
    const doToggle = () => {
      const nowCollapsed = panel.classList.toggle("as-collapsed");
      localStorage.setItem(COLLAPSED_KEY, nowCollapsed ? "1" : "0");
      toggleBtn.textContent = nowCollapsed ? "▸" : "▾";
    };
    toggleBtn.addEventListener("click", (e) => { e.stopPropagation(); doToggle(); });
    header.addEventListener("click", (e) => {
      if (panel.classList.contains("as-collapsed")) doToggle();
    });

    return panel;
  }

  function ratingLabel(r) {
    return ({ 5: "Superstar", 4: "Great", 3: "OK", 2: "Not a fit", 1: "Way off" })[r] || "—";
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderLoading() {
    const body = document.getElementById("as-body");
    if (!body) return;
    body.innerHTML = `<div class="as-empty">Loading score…</div>`;
  }

  function renderError(msg) {
    const body = document.getElementById("as-body");
    if (!body) return;
    body.innerHTML = `<div class="as-status as-err">${escapeHtml(msg)}</div>`;
  }

  function renderNotScored(onScanClick) {
    const body = document.getElementById("as-body");
    if (!body) return;
    body.innerHTML = `
      <div class="as-empty">
        <strong>Not scored yet.</strong><br>
        Run the AI screener for this candidate now — takes ~10-30 seconds.
      </div>
      <div class="as-actions" style="margin-top:0.6rem;">
        <button type="button" class="as-btn" id="as-scan-now">Scan now</button>
        <span class="as-status" id="as-scan-status"></span>
      </div>
    `;
    const btn = document.getElementById("as-scan-now");
    if (btn && typeof onScanClick === "function") {
      btn.addEventListener("click", onScanClick);
    }
  }

  function renderScanning() {
    const body = document.getElementById("as-body");
    if (!body) return;
    body.innerHTML = `
      <div class="as-empty">
        <strong>Scanning…</strong><br>
        Fetching candidate from Comeet and asking Claude. This usually takes
        10–30 seconds — don't close this tab.
      </div>
      <div class="as-status" id="as-scan-status"></div>
    `;
  }

  function renderFeedbackHtml(aiRating) {
    return `
      <div class="as-feedback">
        <div class="as-label">Your rating</div>
        <div class="as-stars" id="as-stars">
          ${[1,2,3,4,5].map(n => `<span class="as-star" data-val="${n}">★</span>`).join("")}
        </div>
        <div class="as-label">Note (optional)</div>
        <textarea class="as-note" id="as-note" placeholder="Why this rating? E.g. 'agency reseller, not a real product role'"></textarea>
        <div class="as-actions">
          <span class="as-status" id="as-fb-status"></span>
          <button type="button" class="as-btn" id="as-submit" disabled>Submit feedback</button>
        </div>
        ${aiRating != null ? `<div class="as-meta">AI rating on file: ${aiRating}/5 (${ratingLabel(aiRating)})</div>` : ""}
      </div>
    `;
  }

  function renderScore(score) {
    const body = document.getElementById("as-body");
    if (!body) return;
    const r = Number(score.rating) || 0;
    const confidence = score.confidence != null ? `confidence ${Math.round(score.confidence * 100)}%` : "";
    const strengths = Array.isArray(score.strengths) ? score.strengths.filter(Boolean) : [];
    const gaps = Array.isArray(score.gaps) ? score.gaps.filter(Boolean) : [];
    const scoredAt = score.scoredAt ? new Date(score.scoredAt).toLocaleString() : "";
    const currentTag = score.currentTag ? `<div class="as-meta">Tag: ${escapeHtml(score.currentTag)}</div>` : "";

    body.innerHTML = `
      <div class="as-rating-row">
        <span class="as-pill r${r}">${r}/5 · ${escapeHtml(ratingLabel(r))}</span>
        <span class="as-confidence">${escapeHtml(confidence)}</span>
      </div>
      ${score.summary ? `<div class="as-summary">${escapeHtml(score.summary)}</div>` : ""}
      ${strengths.length ? `
        <div class="as-section">
          <div class="as-label yes">Strengths</div>
          <ul class="as-list">${strengths.map(s => `<li>${escapeHtml(s)}</li>`).join("")}</ul>
        </div>` : ""}
      ${gaps.length ? `
        <div class="as-section">
          <div class="as-label no">Gaps</div>
          <ul class="as-list">${gaps.map(s => `<li>${escapeHtml(s)}</li>`).join("")}</ul>
        </div>` : ""}
      ${currentTag}
      ${scoredAt ? `<div class="as-meta">Scored ${escapeHtml(scoredAt)}</div>` : ""}
      ${renderFeedbackHtml(r)}
    `;
    wireFeedbackHandlers();
  }

  function wireFeedbackHandlers() {
    selectedRating = 0;
    const starsWrap = document.getElementById("as-stars");
    const submitBtn = document.getElementById("as-submit");
    const noteEl = document.getElementById("as-note");
    const statusEl = document.getElementById("as-fb-status");
    if (!starsWrap || !submitBtn) return;

    const stars = Array.from(starsWrap.querySelectorAll(".as-star"));
    const paint = (val) => {
      stars.forEach((s) => {
        const v = Number(s.dataset.val);
        s.classList.toggle("as-on", v <= val);
      });
    };
    stars.forEach((s) => {
      s.addEventListener("mouseenter", () => paint(Number(s.dataset.val)));
      s.addEventListener("mouseleave", () => paint(selectedRating));
      s.addEventListener("click", () => {
        selectedRating = Number(s.dataset.val);
        paint(selectedRating);
        submitBtn.disabled = false;
        statusEl.textContent = "";
        statusEl.className = "as-status";
      });
    });

    submitBtn.addEventListener("click", async () => {
      if (submitting || !selectedRating || !currentNumericId) return;
      submitting = true;
      submitBtn.disabled = true;
      statusEl.textContent = "Saving…";
      statusEl.className = "as-status";

      try {
        const candidateUid = (currentScore && currentScore.candidateUid) || "";
        const positionUid = (currentScore && currentScore.positionUid) || (extractIdsFromUrl()?.positionUid ?? "");
        const positionName = (currentScore && currentScore.positionName) || extractPositionNameFromDom();
        const aiRating = currentScore ? Number(currentScore.rating) || null : null;

        if (!candidateUid) {
          // No score on file → we can't post feedback yet because the backend
          // keys feedback by alphanumeric candidate uid (matches the Comeet
          // public API). Surface a clear message.
          throw new Error("Feedback needs the candidate to have been scored at least once. Run a scan first.");
        }

        const resp = await postFeedback({
          candidate_uid: candidateUid,
          candidate_name: extractCandidateNameFromDom(),
          position_uid: positionUid,
          position_name: positionName,
          ai_rating: aiRating,
          recruiter_rating: selectedRating,
          note: (noteEl.value || "").trim(),
        });

        statusEl.textContent = `Saved (#${resp.id ?? "?"})`;
        statusEl.className = "as-status as-ok";
        noteEl.value = "";
      } catch (e) {
        statusEl.textContent = e.message || String(e);
        statusEl.className = "as-status as-err";
        submitBtn.disabled = false;
      } finally {
        submitting = false;
      }
    });
  }

  async function runScanForCurrent() {
    const ids = extractIdsFromUrl();
    if (!ids) return;
    renderScanning();
    try {
      const score = await scoreNow(ids.positionUid, ids.numericId);
      currentScore = score;
      renderScore(score);
    } catch (e) {
      console.warn("[auto-screener] scoreNow failed:", e);
      // Show error AND let them retry.
      const body = document.getElementById("as-body");
      if (body) {
        body.innerHTML = `
          <div class="as-status as-err">${escapeHtml(e.message || String(e))}</div>
          <div class="as-actions" style="margin-top:0.6rem;">
            <button type="button" class="as-btn" id="as-scan-retry">Try again</button>
          </div>
        `;
        const btn = document.getElementById("as-scan-retry");
        if (btn) btn.addEventListener("click", runScanForCurrent);
      }
    }
  }

  // ─── Main refresh ───────────────────────────────────────────────────────
  async function refresh() {
    const ids = extractIdsFromUrl();
    if (!ids) {
      // Not on a candidate page — remove panel if present.
      const existing = document.getElementById(PANEL_ID);
      if (existing) existing.remove();
      currentNumericId = null;
      currentScore = null;
      return;
    }

    if (ids.numericId === currentNumericId) {
      // Already rendered for this candidate. Nothing to do.
      return;
    }
    currentNumericId = ids.numericId;
    currentScore = null;

    ensurePanel();
    renderLoading();

    try {
      const score = await fetchScore(ids.numericId);
      if (!score) {
        // Not scored yet — auto-trigger a scan immediately. We could also
        // offer a manual "Scan now" button, but the user explicitly wanted
        // the auto path so we go straight in.
        await runScanForCurrent();
        return;
      }
      currentScore = score;
      renderScore(score);
    } catch (e) {
      console.warn("[auto-screener]", e);
      renderError(e.message || String(e));
    }
  }

  // ─── SPA navigation watcher ─────────────────────────────────────────────
  // Comeet doesn't fire 'popstate' for in-app navigations. We watch the URL
  // ourselves on a short interval; cheap, and survives whatever router they use.
  let lastUrl = location.href;
  function watchUrl() {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      refresh();
    }
  }
  setInterval(watchUrl, 500);
  window.addEventListener("popstate", refresh);

  // Initial run after the page settles a bit (DOM may still be filling in).
  setTimeout(refresh, 300);
})();
