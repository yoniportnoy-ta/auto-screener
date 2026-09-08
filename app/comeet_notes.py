"""Post recruiter notes to a candidate's Comeet profile.

Comeet's note renderer supports ONLY <b>, <i> and <u>. Interview summaries
arrive as markdown, so they must be converted on the way in — otherwise
`**Pros**` lands on the candidate profile as literal asterisks.

Endpoint (verified 2026-09-06): POST https://api.comeet.co/candidates/{uid}/notes
The unversioned base is correct — GET on it returns 405 (route exists, POST-only)
while /v1/... returns the marketing site's 404 HTML.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

_MAX_NOTE_CHARS = 30000


def markdown_to_comeet_html(md: str) -> str:
    """Markdown -> the tiny subset Comeet renders (<b>, <i>, <u>).

    Everything else degrades to readable plain text: headings become bold
    lines, bullets become '• ', links become 'text (url)'. HTML in the source
    is escaped first so a stray '<' can't break the note or inject markup.
    """
    if not md:
        return ""
    text = html.escape(md.replace("\r\n", "\n").replace("\r", "\n"))

    out_lines: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:                       # code blocks: keep verbatim
            out_lines.append(line)
            continue
        # headings -> bold line
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
        if m:
            out_lines.append(f"<b>{m.group(2).strip()}</b>")
            continue
        # bullets (-, *, +) and nested indentation -> bullet char
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            out_lines.append(f"{m.group(1)}• {m.group(2)}")
            continue
        # horizontal rules -> blank
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            out_lines.append("")
            continue
        out_lines.append(line)
    text = "\n".join(out_lines)

    # links [text](url) -> text (url)   [before emphasis, so URLs stay intact]
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)", r"\1 (\2)", text)
    # bold+italic, bold, then italic (longest markers first)
    text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.S)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<b>\1</b>", text, flags=re.S)
    # single * / _ italics: require non-space neighbours so a*b and snake_case survive
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])", r"<i>\1</i>", text)
    # inline code -> plain (Comeet has no <code>)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)

    return text[:_MAX_NOTE_CHARS]


def build_note_payload(text_body: str, author: dict[str, Any], *, is_markdown: bool = True) -> dict[str, Any]:
    """The exact body Comeet expects, attributed to the human recruiter."""
    body = markdown_to_comeet_html(text_body) if is_markdown else (text_body or "")[:_MAX_NOTE_CHARS]
    return {
        "time_created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_by": {
            "type": "user",
            "first_name": (author.get("first_name") or "").strip(),
            "last_name": (author.get("last_name") or "").strip(),
            "email": (author.get("email") or "").strip(),
        },
        "text": body,
    }


# ── Identifier resolution ─────────────────────────────────────────────────
# Every recruiter-facing surface (calendar invite links, Slack summaries, the
# Comeet profile URL) exposes the NUMERIC id — e.g. app.comeet.co/.../can/61459710
# — but the Recruiting API keys notes on the ALPHANUMERIC uid (FC.C9A3E) and
# 404s on the numeric form for both GET and POST. So callers can't resolve it
# themselves; we do it here and accept either form.
_UID_MAP_DDL = (
    "CREATE TABLE IF NOT EXISTS candidate_uid_map ("
    " numeric_id text PRIMARY KEY, candidate_uid text NOT NULL,"
    " resolved_at timestamptz DEFAULT now())"
)


def resolve_candidate_uid(ident: str, *, scan_budget_s: float = 8.0) -> Optional[str]:
    """Accept either identifier form; return the alphanumeric uid the API needs.

    Order: pass through non-numeric input -> memo table -> mined corpus
    (covers ~everything that has been screened) -> live scan of open positions
    capped at `scan_budget_s` (new candidates not yet mined). Returns None when
    unresolvable, so the caller can answer 404 promptly.
    """
    from sqlalchemy import text as _text
    from .db import engine

    ident = (ident or "").strip()
    if not ident:
        return None
    if not ident.isdigit():
        return ident  # already the alphanumeric uid

    with engine.begin() as c:
        c.execute(_text(_UID_MAP_DDL))
        row = c.execute(_text("SELECT candidate_uid FROM candidate_uid_map WHERE numeric_id=:n"),
                        {"n": ident}).first()
        if row:
            return str(row[0])
        # corpus stores profile_url alongside the uid — instant mapping
        try:
            row = c.execute(_text(
                "SELECT candidate_uid FROM corpus_screen_labels "
                "WHERE profile_url LIKE :pat AND candidate_uid IS NOT NULL LIMIT 1"),
                {"pat": f"%/can/{ident}"}).first()
        except Exception:  # noqa: BLE001 — corpus may not be mined yet
            row = None
        if row:
            uid = str(row[0])
            c.execute(_text("INSERT INTO candidate_uid_map (numeric_id, candidate_uid) VALUES (:n,:u) "
                            "ON CONFLICT (numeric_id) DO UPDATE SET candidate_uid=EXCLUDED.candidate_uid"),
                      {"n": ident, "u": uid})
            return uid

    # Fallback: brand-new candidate not yet mined. Scan OPEN positions, but on a
    # HARD WALL-CLOCK DEADLINE — an unresolvable id would otherwise walk every
    # position (measured >120s) and hammer Comeet on every bad request. Better to
    # answer 404 quickly; the 6-hourly corpus mine will pick the candidate up.
    import time as _time
    deadline = _time.monotonic() + scan_budget_s
    from .comeet_client import ComeetClient
    try:
        with ComeetClient() as cc:
            for pos in cc.list_open_positions():
                if _time.monotonic() > deadline:
                    log.info("uid resolution for %s hit the %.0fs scan budget", ident, scan_budget_s)
                    break
                for cand in cc.list_candidates_for_position(str(pos.get("uid") or "")):
                    url = (cand.get("URL") or "").rstrip("/")
                    if url.endswith(f"/can/{ident}"):
                        uid = str(cand.get("uid") or "")
                        if uid:
                            with engine.begin() as c:
                                c.execute(_text(
                                    "INSERT INTO candidate_uid_map (numeric_id, candidate_uid) VALUES (:n,:u) "
                                    "ON CONFLICT (numeric_id) DO UPDATE SET candidate_uid=EXCLUDED.candidate_uid"),
                                    {"n": ident, "u": uid})
                            log.info("resolved numeric id %s -> uid via live scan", ident)
                            return uid
    except Exception as exc:  # noqa: BLE001
        log.warning("uid resolution scan failed for %s: %s", ident, str(exc)[:120])
    return None
