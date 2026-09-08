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
from typing import Any

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
