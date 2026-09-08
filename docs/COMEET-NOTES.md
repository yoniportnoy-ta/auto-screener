# Posting notes to Comeet — `POST /api/candidate/note`

Recruiters approve an interview summary in Claude, then post it to the candidate's
Comeet profile through this service, so ATS credentials stay server-side and the
note is attributed to the recruiter who ran the interview rather than a bot.

## Calling it

```
POST https://auto-screener-2va5.onrender.com/api/candidate/note
X-Screener-Token: <SCREENER_API_TOKEN>
Content-Type: application/json

{
  "candidate_uid": "61459710",            // numeric id OR alphanumeric uid — both work
  "text": "**Pros**\n- Strong closer",     // markdown
  "author": {"first_name": "Yoni", "last_name": "Portnoy",
             "email": "yoni.portnoy@riverside.fm"},
  "is_markdown": true
}
```

**Auth** is `_require_extension_token` — the same dependency guarding
`/api/extension/*`. Missing/wrong token → `401`; server without a configured
token → `503` (fails closed).

Response includes `resolved: true` when a numeric id was translated, plus the
`candidate_uid` actually used.

## Three facts that cost real debugging time

**1. The base URL is UNVERSIONED.** `https://api.comeet.co/candidates/{uid}/notes`.
A `/v1` prefix returns Comeet's *marketing-site 404 HTML*, not a JSON API error —
so it reads like a bad uid or bad auth. Diagnostic: `GET` on the correct path
returns **405** (route exists, POST-only).

**2. The API keys on the ALPHANUMERIC uid (`FC.C9A3E`), but every
recruiter-facing surface exposes the NUMERIC id** (`app.comeet.co/.../can/61459710`)
— calendar invite links, Slack summaries, the profile URL. The numeric form
**404s on both GET and POST**, so callers cannot resolve it themselves.
`comeet_notes.resolve_candidate_uid()` accepts either and resolves server-side:
memo table → mined corpus (`corpus_screen_labels.profile_url`) → bounded live
scan of open positions, memoising the result in `candidate_uid_map`.

**3. Comeet renders only `<b>`, `<i>`, `<u>`.** Markdown must be converted or
`**Pros**` appears as literal asterisks on the profile.
`markdown_to_comeet_html()` maps headings→bold, bullets→`•`, links→`text (url)`,
escapes source HTML, and avoids false italics on `snake_case` / `2*3`.

## Safety properties
- Fails closed: `500` naming any absent `COMEET_*` var; `400` without `author.email`.
- Logs uid / author / char-count only — never the secret, JWT, or note body.
- Reuses `ComeetClient`'s JWT minting and retry/backoff (no bare `requests`).
