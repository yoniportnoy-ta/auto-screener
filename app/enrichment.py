"""Candidate profile enrichment — career timeline, LinkedIn, education.

The calibration UI needs more than the AI's scoring prose ("the blurb")
to let recruiters actually judge a candidate fresh — they want to see
the timeline of where this person has worked, what they did there, and
their LinkedIn link.

Strategy:
  - LinkedIn URL: pulled directly from the Comeet candidate object
    (Comeet has it as a first-class field). Free, instant.
  - Career timeline + education: extracted by Claude from the CV PDF.
    ~$0.01 per candidate, ~2-3s. Cached in Postgres keyed by
    candidate_uid, so subsequent views (or other recruiters) hit the
    cache.

Failures cache too — if a candidate has no CV on file or Claude can't
parse, we write a row with extraction_error set so we don't retry on
every queue refresh. Forcing a re-extraction = delete the row.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx
from sqlalchemy import select

from .config import settings
from .db import db_session
from .models import CandidateEnrichment

log = logging.getLogger(__name__)


def get_or_extract(candidate_uid: str) -> dict[str, Any]:
    """Cache-first enrichment lookup. Returns a stable dict shape for the UI:

        {
          "candidateUid": str,
          "linkedinUrl": str | None,
          "careerTimeline": [{company, role, start, end, highlights[]}, ...],
          "education": [{school, degree, year}, ...],
          "error": str | None,
          "cached": bool,         # True if we served from the cache (no Claude call)
        }
    """
    candidate_uid = (candidate_uid or "").strip()
    if not candidate_uid:
        return _empty(candidate_uid, error="missing candidate_uid")

    cached = _read_cache(candidate_uid)
    if cached is not None:
        return cached

    # Cache miss — do the work.
    payload = _extract_fresh(candidate_uid)
    _write_cache(candidate_uid, payload)
    payload["cached"] = False
    return payload


def _empty(candidate_uid: str, error: str | None = None) -> dict[str, Any]:
    return {
        "candidateUid": candidate_uid,
        "linkedinUrl": None,
        "profileUrl": None,
        "careerTimeline": [],
        "education": [],
        "error": error,
        "cached": False,
    }


def _read_cache(candidate_uid: str) -> dict[str, Any] | None:
    with db_session() as ses:
        row = ses.scalar(
            select(CandidateEnrichment).where(
                CandidateEnrichment.candidate_uid == candidate_uid
            )
        )
        if row is None:
            return None
        return {
            "candidateUid": candidate_uid,
            "linkedinUrl": row.linkedin_url,
            "profileUrl": row.profile_url,
            "careerTimeline": row.career_timeline_json or [],
            "education": row.education_json or [],
            "error": row.extraction_error,
            "cached": True,
        }


def _write_cache(candidate_uid: str, payload: dict[str, Any]) -> None:
    with db_session() as ses:
        row = ses.scalar(
            select(CandidateEnrichment).where(
                CandidateEnrichment.candidate_uid == candidate_uid
            )
        )
        if row is None:
            row = CandidateEnrichment(candidate_uid=candidate_uid)
            ses.add(row)
        row.linkedin_url = payload.get("linkedinUrl")
        row.profile_url = payload.get("profileUrl")
        row.career_timeline_json = payload.get("careerTimeline") or []
        row.education_json = payload.get("education") or []
        row.extraction_error = payload.get("error")
        ses.commit()


def _extract_fresh(candidate_uid: str) -> dict[str, Any]:
    """Do the actual fetch + Claude extraction. Returns the same shape as
    _empty/_read_cache (minus `cached`, which the caller stamps in).
    """
    from .comeet_client import ComeetClient

    # 1. Fetch the Comeet candidate object to get LinkedIn + resume URL.
    try:
        with ComeetClient() as pub:
            candidate = pub.get_candidate(candidate_uid) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("enrichment: comeet fetch failed for %s: %s", candidate_uid, exc)
        return _empty(candidate_uid, error=f"comeet fetch failed: {exc}")

    linkedin_url = _clean_linkedin(candidate.get("linkedin_url"))
    # Capture Comeet's working web URL while we have the candidate object —
    # it's the only format that navigates inside app.comeet.co, and the
    # whole point of caching it here is to avoid a per-render Comeet round-
    # trip from the calibration queue later.
    profile_url = (candidate.get("URL") or "").strip() or None
    resume_obj = candidate.get("resume") or {}
    resume_url = resume_obj.get("url") if isinstance(resume_obj, dict) else None

    # 2. Download the CV — could be PDF (best) or DOCX (text-only fallback).
    resume_pdf_b64, resume_docx_text, fetch_failed = _maybe_fetch_resume(resume_url)
    if not resume_pdf_b64 and not resume_docx_text:
        # No CV → return what we have (LinkedIn at least), with an
        # informative error so the UI can degrade gracefully.
        return {
            "candidateUid": candidate_uid,
            "linkedinUrl": linkedin_url,
            "profileUrl": profile_url,
            "careerTimeline": [],
            "education": [],
            "error": "no resume available" if fetch_failed else "candidate has no CV on file",
            "cached": False,
        }

    # 3. Extract structured timeline via Claude. Pass PDF as document
    # attachment when available; otherwise pass DOCX text as plain text.
    try:
        parsed = _claude_extract(resume_pdf_b64=resume_pdf_b64, resume_text=resume_docx_text)
    except Exception as exc:  # noqa: BLE001
        log.warning("enrichment: claude extract failed for %s: %s", candidate_uid, exc)
        return {
            "candidateUid": candidate_uid,
            "linkedinUrl": linkedin_url,
            "profileUrl": profile_url,
            "careerTimeline": [],
            "education": [],
            "error": f"timeline extraction failed: {exc}",
            "cached": False,
        }

    # Claude can return a linkedin_url too if it spots one in the CV that
    # Comeet didn't have. Prefer Comeet's; fall back to Claude's.
    if not linkedin_url and parsed.get("linkedin_url"):
        linkedin_url = _clean_linkedin(parsed.get("linkedin_url"))

    return {
        "candidateUid": candidate_uid,
        "linkedinUrl": linkedin_url,
        "profileUrl": profile_url,
        "careerTimeline": _normalize_timeline(parsed.get("career_timeline")),
        "education": _normalize_education(parsed.get("education")),
        "error": None,
        "cached": False,
    }


def _maybe_fetch_resume(url: str | None) -> tuple[str | None, str | None, bool]:
    """Download the CV. Returns (pdf_b64, docx_text, did_fail).

    Mirrors scan._maybe_fetch_resume — kept inline here so this module
    doesn't depend on the scanning pipeline. For DOCX we extract text
    via python-docx so Claude can still process it (the timeline-
    extraction pass below treats text and PDF differently).
    """
    if not url:
        return None, None, False
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return None, None, True
            content = resp.content or b""
            if not content or len(content) > 7 * 1024 * 1024:
                return None, None, True
            mime = (resp.headers.get("content-type") or "").lower()
            url_lower = url.lower()
            if "pdf" in mime or url_lower.endswith(".pdf"):
                return base64.b64encode(content).decode("ascii"), None, False
            if (
                "officedocument.wordprocessingml" in mime
                or "msword" in mime
                or url_lower.endswith((".docx", ".doc"))
                or content.startswith(b"PK\x03\x04")
            ):
                from .scan import _extract_docx_text
                text = _extract_docx_text(content)
                if text:
                    return None, text, False
                return None, None, True
            return None, None, False
    except Exception as exc:  # noqa: BLE001
        log.info("enrichment: resume fetch failed: %s", exc)
        return None, None, True


def _claude_extract(
    *,
    resume_pdf_b64: str | None = None,
    resume_text: str | None = None,
) -> dict[str, Any]:
    """Send the CV to Claude with a focused extraction prompt. Returns a dict
    with `career_timeline`, `education`, and optionally `linkedin_url`.

    Accepts either a PDF (attached as a document) or extracted DOCX text.
    Exactly one should be provided.
    """
    # Lazy import keeps app boot fast.
    from anthropic import Anthropic

    prompt = (
        "You are extracting a structured career timeline from a resume "
        "for a recruiter to glance at. Return STRICT JSON with no markdown, "
        "no prose, no code fences — just the object.\n\n"
        "Schema:\n"
        "{\n"
        '  "career_timeline": [\n'
        "    {\n"
        '      "company": "Company name",\n'
        '      "role": "Job title",\n'
        '      "start": "YYYY-MM or YYYY",\n'
        '      "end": "YYYY-MM or YYYY or \\"Present\\"",\n'
        '      "highlights": ["1-2 short bullet points of the most important achievements or milestones (e.g. promotions, launches, scale numbers). Each ≤ 120 chars. Empty list if nothing notable is stated."]\n'
        "    }\n"
        "  ],\n"
        '  "education": [\n'
        '    {"school": "...", "degree": "...", "year": "YYYY or null"}\n'
        "  ],\n"
        '  "linkedin_url": "https://linkedin.com/in/... or null"\n'
        "}\n\n"
        "Rules:\n"
        "- Order career_timeline most-recent first. Include up to 5 jobs; drop older ones if more than 5.\n"
        "- Use the candidate's exact employer name (don't paraphrase to 'Google' if they wrote 'Google Israel').\n"
        "- If a date is ambiguous (just 'Summer 2020'), pick a year and put just the year.\n"
        "- Skip internships, freelance gigs, and unrelated jobs only if you have to fit in 5; otherwise include them.\n"
        "- Empty arrays are fine. Don't invent details that aren't on the page.\n\n"
        "LinkedIn extraction (look HARDER — this is currently missing 40%+ of the time):\n"
        "- Scan the CV header, contact section, footer, and ANY mention of 'linkedin' (case-insensitive).\n"
        "- Accept any form: 'linkedin.com/in/...', 'www.linkedin.com/in/...', 'https://il.linkedin.com/in/...',\n"
        "  even bare '/in/joey-foo' or 'LinkedIn: joey-foo' — normalise to https://linkedin.com/in/<slug>.\n"
        "- Also accept icons next to a URL (LinkedIn icon often appears in CV headers).\n"
        "- Only return null if you genuinely see no LinkedIn URL or username anywhere in the CV.\n"
    )

    # Build user content based on what we have. PDF → document block;
    # text → inline. Either way the prompt comes second so the schema
    # instructions are read after the source material.
    user_content: list[dict[str, Any]] = []
    if resume_pdf_b64:
        user_content.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": resume_pdf_b64,
            },
        })
    elif resume_text:
        user_content.append({
            "type": "text",
            "text": "CANDIDATE CV (plain text, extracted from .docx):\n\n" + resume_text,
        })
    else:
        raise ValueError("_claude_extract: must provide resume_pdf_b64 OR resume_text")
    user_content.append({"type": "text", "text": prompt})

    client = Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.claude_model,
        max_tokens=2000,
        temperature=0.0,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", "") == "text"
    ).strip()
    # Defensive — strip code fences if Claude ignored instructions.
    if text.startswith("```"):
        # ```json\n{...}\n``` or ```\n{...}\n```
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    return json.loads(text)


def _normalize_timeline(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for it in items[:5]:  # hard cap; the prompt also asks for ≤5
        if not isinstance(it, dict):
            continue
        out.append({
            "company": _clean_str(it.get("company")),
            "role": _clean_str(it.get("role")),
            "start": _clean_str(it.get("start")),
            "end": _clean_str(it.get("end")),
            "highlights": [
                _clean_str(h) for h in (it.get("highlights") or [])
                if isinstance(h, str) and h.strip()
            ][:3],
        })
    return out


def _normalize_education(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        out.append({
            "school": _clean_str(it.get("school")),
            "degree": _clean_str(it.get("degree")),
            "year": _clean_str(it.get("year")),
        })
    return out


def _clean_str(v: Any) -> str:
    return (str(v).strip() if v else "") or ""


def _clean_linkedin(v: Any) -> str | None:
    """Normalise whatever Comeet or Claude returned for a LinkedIn URL.

    Accepted shapes (all coerced to https://linkedin.com/in/<slug>):
      - https://www.linkedin.com/in/joey-foo
      - https://il.linkedin.com/in/joey-foo
      - linkedin.com/in/joey-foo
      - /in/joey-foo (Claude sometimes returns just the path)
    Returns None for anything that doesn't look like a real LinkedIn URL
    — we deliberately do NOT accept bare slugs because random strings would
    be falsely promoted to LinkedIn URLs. The extraction prompt asks Claude
    for the full URL explicitly.
    """
    if not v:
        return None
    s = str(v).strip().rstrip("/")
    if not s:
        return None
    s = s.split("?")[0].split("#")[0]

    import re
    # Match linkedin.com/in/<slug> with optional country subdomain (il., uk., etc.).
    m = re.search(r"linkedin\.com(?:/[a-z]{2})?/in/([A-Za-z0-9_-]+)", s, re.IGNORECASE)
    if m:
        return "https://linkedin.com/in/" + m.group(1)
    # /in/<slug> path-only (no host).
    m = re.search(r"^/in/([A-Za-z0-9_-]+)", s, re.IGNORECASE)
    if m:
        return "https://linkedin.com/in/" + m.group(1)
    return None


__all__ = ["get_or_extract"]
