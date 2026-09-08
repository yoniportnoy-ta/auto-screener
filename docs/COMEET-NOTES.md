# Posting notes to Comeet — `POST /api/candidate/note`

Recruiters approve an interview summary in Claude, then post it to the candidate's
Comeet profile through this service, so ATS credentials stay server-side and the
note is attributed to the recruiter who ran the interview rather than a bot.

## Calling it

```
POST https://auto-screener-2va5.onrender.com/api/candidate/note
X-Screener-Token: <YOUR PERSONAL recruiter token>
Content-Type: application/json

{
  "candidate": "61459710",              // uid, numeric id, OR full profile URL
  "text": "**Pros**\n- Strong closer",  // markdown
  "is_markdown": true
}
```

**No `author` field.** The note is attributed to whoever the token identifies —
identity comes from the credential, never the request body, so a token holder
cannot post under a colleague's name. Any `author` sent is ignored.

**Auth** is `_require_recruiter`, backed by `SCREENER_RECRUITER_TOKENS`
(JSON: `token -> {first_name, last_name, email}`). Unknown token → `401`;
no map configured, or malformed JSON → `503` (fails closed, never partially
parsed). Add a person by adding an entry; remove theirs to revoke just them.

> This is deliberately NOT the extension's `SCREENER_API_TOKEN`. That one is
> baked into every `/extension.zip` download from an unauthenticated endpoint,
> so it identifies the *tool*, not a *person*, and cannot carry attribution.
> `/api/extension/*` keeps using it; notes do not.

Response reports the uid actually used, whether resolution happened, and the
attributed author:
`{"ok": true, "candidate_uid": "FC.C9A3E", "given": "...", "resolved": true,
  "author": "yoni.portnoy@riverside.fm", "chars": 133}`

## Three facts that cost real debugging time

**1. The base URL is UNVERSIONED.** `https://api.comeet.co/candidates/{uid}/notes`.
A `/v1` prefix returns Comeet's *marketing-site 404 HTML*, not a JSON API error —
so it reads like a bad uid or bad auth. Diagnostic: `GET` on the correct path
returns **405** (route exists, POST-only).

**2. The API keys on the ALPHANUMERIC uid (`FC.C9A3E`), but every
recruiter-facing surface exposes the NUMERIC id** (`app.comeet.co/.../can/61459710`)
— calendar invite links, Slack summaries, the profile URL. The numeric form
**404s on both GET and POST**, so callers cannot resolve it themselves.
`comeet_notes.resolve_candidate_uid()` accepts a uid, a numeric id, or a full
profile URL and resolves server-side using only the PUBLIC Recruiting API:
memo table → mined corpus (`corpus_screen_labels.profile_url` already stores the
`/can/<numeric>` mapping) → live scan of open positions capped at an 8s
wall-clock budget, memoised in `candidate_uid_map`. The budget matters: an
unresolvable id previously walked every position and took 178s before answering
404. No captcha-gated recruiter-app dependency is used.

**3. Comeet renders only `<b>`, `<i>`, `<u>`.** Markdown must be converted or
`**Pros**` appears as literal asterisks on the profile.
`markdown_to_comeet_html()` maps headings→bold, bullets→`•`, links→`text (url)`,
escapes source HTML, and avoids false italics on `snake_case` / `2*3`.

## Safety properties
- Fails closed: `500` naming any absent `COMEET_*` var; `503` with no recruiter
  tokens configured; `401` on an unknown token; `404` (in <9s) on an
  unresolvable candidate reference.
- Logs uid / author / char-count only — never the secret, JWT, or note body.
- Reuses `ComeetClient`'s JWT minting and retry/backoff (no bare `requests`).
