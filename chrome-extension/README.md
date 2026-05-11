# Auto Screener — Chrome extension

Companion Chrome extension for the Auto Screener backend. When a recruiter
opens any candidate page on `app.comeet.co`, this extension injects a side
panel that shows the AI screening summary and lets them post feedback inline
(1–5 stars + optional note).

The panel is a much richer feedback channel than the existing
tag-swap polling — recruiters can disagree, explain, and we capture it as
training signal without them leaving Comeet.

## How it works

1. Content script (`content.js`) runs on `https://app.comeet.co/app/req/*`.
2. It watches the URL for the SPA route `/app/req/<positionUid>/can/<numericId>`.
3. When that route is active, it calls
   `GET /api/extension/score?numeric_id=<id>` on the backend and renders
   the rating, summary, strengths, and gaps.
4. Submitting feedback posts to `POST /api/extension/feedback` with the
   recruiter's rating + note. The backend stores it in the same `feedback`
   table as the recruiter UI uses.

Backend endpoints are gated by the shared `SCREENER_API_TOKEN`; the extension
sends it as the `X-Screener-Token` header on every request.

## Loading the extension (developer mode)

1. Open `chrome://extensions` in Chrome.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and pick the `chrome-extension/` folder in this repo.
4. The Auto Screener icon should now appear in the toolbar. Click it to open
   the popup and configure:
   - **Backend URL** — defaults to the production Render URL.
   - **API token** — the `SCREENER_API_TOKEN` value from the Render dashboard.
   - **Recruiter email** — optional; tagged onto each feedback entry.
5. Click **Test connection** to verify the token and reachability.
6. Visit any candidate page on `app.comeet.co`. The panel appears top-right;
   click the header to collapse.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | MV3 manifest (host permissions, content script wiring). |
| `content.js` | Injected on Comeet pages. Fetches score, renders panel, posts feedback. |
| `styles.css` | Scoped styles for the injected panel (dark theme matching the recruiter UI). |
| `popup.html` / `popup.js` | Toolbar settings popup (backend URL + token + email). |
| `icons/` | 16/48/128 px PNG icons used in the toolbar + extensions page. |

## Updating

The extension auto-reloads when you click the refresh icon next to it on the
`chrome://extensions` page. For published distribution we'd build a zip and
push it to the Chrome Web Store (or use enterprise force-install).

## Notes

- Submitting feedback for a candidate that hasn't been scored yet currently
  shows a "scan first" message — the backend keys feedback by the alphanumeric
  candidate uid, which we only learn after the first scan. A future
  improvement could call the public API to resolve the uid on demand.
- The panel position is fixed top-right and remembers its collapsed state via
  `localStorage`.
- Network calls go directly from the content script to the backend; the
  backend's CORS allow-list includes `https://app.comeet.co` and any
  `chrome-extension://*` origin.
