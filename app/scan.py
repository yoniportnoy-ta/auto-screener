"""High-level scan orchestration — eligibility, queue building, score-and-tag.

Replaces computeScanBatchQueue_ + scoreScanSessionCandidate side of Code.gs.
The flow is:

    begin_scan_batch(position_uid, ...)
      → return ScanSession(session_id, uids)

    score_candidate_in_session(session_id, candidate_uid)
      → return summary (rating, strengths, gaps, …)

    finish_scan_batch(session_id, processed_uids, ...)
      → commit (advance review cursor, drop session)
"""
from __future__ import annotations

import base64
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .comeet_app_client import ComeetAppClient
from .comeet_client import (
    ComeetClient,
    candidate_active_for_screening,
    candidate_full_name,
    candidate_in_allowed_step,
    candidate_max_activity_iso,
    position_jd_text,
)
from .config import settings
from .db import db_session
from .feedback import saturated_candidate_uids_for_position
from .models import CandidateLock
from .position_classes import get_position_class as get_class_for_position
from .scan_session import (
    ScanSession,
    delete_session,
    load_session,
    new_session_id,
    save_session,
)
from .scoring import ScoreInputs, ScoreResult, score_candidate
from .tagging import apply_rating_tag

log = logging.getLogger(__name__)

LOCK_KEY_LAST_REVIEW = "last_review:"
LOCK_KEY_SCORE_DONE = "score_done:"


# ─── Public API result types ─────────────────────────────────────────────────
@dataclass
class CandidateSummary:
    """What the UI table shows per candidate after scoring."""
    candidate_uid: str
    name: str = ""
    time_created: str = ""
    status: str = ""
    activity_iso: str | None = None
    rating: int | None = None
    rating_label: str | None = None
    confidence: float | None = None
    summary: str | None = None
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    profile_url: str | None = None
    cv_url: str | None = None
    cv_file_name: str | None = None
    linkedin_url: str | None = None
    error: str | None = None
    tag_applied: str | None = None
    tag_error: str | None = None


@dataclass
class BeginScanResult:
    empty: bool
    session_id: str | None = None
    position_name: str = ""
    class_id: str | None = None
    class_name: str | None = None
    class_level: str | None = None
    uids: list[str] = field(default_factory=list)
    pending_new_count: int = 0
    capped: bool = False
    remaining_new_count: int = 0
    batch_size: int = 0
    last_review_before: str | None = None
    message: str = ""
    previous_pages_available: int = 0


# ─── Cursor helpers ──────────────────────────────────────────────────────────
def _last_review_key(position_uid: str) -> str:
    return f"{LOCK_KEY_LAST_REVIEW}{position_uid}"


def _score_done_key(candidate_uid: str) -> str:
    return f"{LOCK_KEY_SCORE_DONE}{candidate_uid}"


def get_last_review_iso(position_uid: str) -> str:
    with db_session() as ses:
        from sqlalchemy import select
        row = ses.scalar(select(CandidateLock).where(CandidateLock.key == _last_review_key(position_uid)))
        return (row.value if row else "") or "1970-01-01T00:00:00Z"


def set_last_review_iso(position_uid: str, value: str) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    with db_session() as ses:
        stmt = pg_insert(CandidateLock).values(key=_last_review_key(position_uid), value=value)
        stmt = stmt.on_conflict_do_update(
            index_elements=[CandidateLock.key],
            set_={"value": stmt.excluded.value, "updated_at": datetime.now(timezone.utc)},
        )
        ses.execute(stmt)


def is_score_done(candidate_uid: str) -> bool:
    with db_session() as ses:
        from sqlalchemy import select
        row = ses.scalar(select(CandidateLock).where(CandidateLock.key == _score_done_key(candidate_uid)))
        return row is not None and (row.value or "") not in ("", "0", "false")


def mark_score_done(candidate_uid: str) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    with db_session() as ses:
        stmt = pg_insert(CandidateLock).values(key=_score_done_key(candidate_uid), value="1")
        stmt = stmt.on_conflict_do_update(
            index_elements=[CandidateLock.key],
            set_={"value": "1", "updated_at": datetime.now(timezone.utc)},
        )
        ses.execute(stmt)


# ─── Eligibility / queue build ───────────────────────────────────────────────
def begin_scan_batch(
    position_uid: str,
    *,
    seen_uids: list[str] | None = None,
    class_id_override: str | None = None,
    class_level_override: str | None = None,
) -> BeginScanResult:
    """Produce a ScanSession with the next batch of candidates to score."""
    if not position_uid:
        raise ValueError("position_uid required")

    # Class assignment must exist (or be supplied via override).
    cls = get_class_for_position(position_uid)
    if cls is None and class_id_override:
        from .position_classes import assign_position_class
        cls = assign_position_class(position_uid, class_id_override, class_level_override or "")
    if cls is None:
        raise ValueError(
            "No position class selected for this position. Pick one from the UI dropdown first."
        )

    seen_set: set[str] = {str(u) for u in (seen_uids or []) if u}

    with ComeetClient() as client:
        position = client.get_position(position_uid)
        if not position:
            raise ValueError(f"Position not found: {position_uid}")
        position_name = position.get("name") or position_uid
        jd_text = position_jd_text(position)

        candidates = client.list_candidates_for_position(position_uid)

    # Filter for eligibility (active + in allowed step + not yet scored).
    saturated = saturated_candidate_uids_for_position(position_uid, threshold=3)
    candidates = [c for c in candidates if c.get("uid") and c["uid"] not in saturated]
    eligible = [
        c for c in candidates
        if candidate_active_for_screening(c)
        and candidate_in_allowed_step(c)
        and not is_score_done(str(c["uid"]))
    ]

    # Sort by activity, newest first; deduplicate by email/name; drop seen UIDs.
    eligible.sort(key=_activity_sort_key, reverse=True)
    eligible = _dedupe_by_identity(eligible)
    eligible = [c for c in eligible if str(c["uid"]) not in seen_set]

    max_run = max(1, min(50, settings.screener_max_per_run))
    to_process = eligible[:max_run]

    pending_new_count = len(eligible)
    remaining = max(0, len(eligible) - len(to_process))

    if not to_process:
        return BeginScanResult(
            empty=True,
            position_name=position_name,
            class_id=cls["classId"],
            class_name=cls["className"],
            class_level=cls.get("level"),
            pending_new_count=pending_new_count,
            capped=False,
            remaining_new_count=remaining,
            batch_size=max_run,
            message=f'No new candidates to review for "{position_name}".',
        )

    sess = ScanSession(
        session_id=new_session_id(),
        position_uid=position_uid,
        position_name=position_name,
        class_id=cls["classId"],
        class_name=cls["className"],
        uids=[str(c["uid"]) for c in to_process],
        last_review_before=get_last_review_iso(position_uid),
        pending_new_count=pending_new_count,
        capped=remaining > 0,
        remaining_new_count=remaining,
        batch_size=max_run,
        jd_text=jd_text,
    )
    save_session(sess)

    return BeginScanResult(
        empty=False,
        session_id=sess.session_id,
        position_name=position_name,
        class_id=cls["classId"],
        class_name=cls["className"],
        class_level=cls.get("level"),
        uids=sess.uids,
        pending_new_count=pending_new_count,
        capped=remaining > 0,
        remaining_new_count=remaining,
        batch_size=max_run,
        last_review_before=sess.last_review_before,
    )


def score_candidate_in_session(session_id: str, candidate_uid: str) -> CandidateSummary:
    sess = load_session(session_id)
    if sess is None:
        raise ValueError("Scan session expired or finished. Start a new scan.")
    if candidate_uid not in sess.uids:
        raise ValueError("Candidate is not part of this scan batch.")

    with ComeetClient() as client:
        candidate = client.get_candidate(candidate_uid)

    if not candidate:
        return CandidateSummary(candidate_uid=candidate_uid, error="Could not load candidate.")

    summary = CandidateSummary(
        candidate_uid=str(candidate.get("uid") or candidate_uid),
        name=candidate_full_name(candidate),
        time_created=str(candidate.get("time_created") or ""),
        status=str(candidate.get("status") or ""),
        profile_url=_normalize_url(candidate.get("URL")),
        linkedin_url=_normalize_url(candidate.get("linkedin_url")),
    )
    resume = candidate.get("resume") or {}
    summary.cv_url = _normalize_url(resume.get("url"))
    summary.cv_file_name = (resume.get("name") or "").strip() or None

    activity = candidate_max_activity_iso(candidate)
    if activity:
        summary.activity_iso = activity.isoformat().replace("+00:00", "Z")

    if not candidate_active_for_screening(candidate):
        summary.error = (
            f"Skipped: recruiting status not active for screening ({candidate.get('status') or 'unknown'})."
        )
        return summary
    if not candidate_in_allowed_step(candidate):
        summary.error = "Skipped: not in a configured pipeline step."
        return summary

    # Build process context (name/email/source/links) — enough for prompt anchoring.
    process_ctx = _build_process_context(candidate)
    resume_pdf_b64, resume_failed = _maybe_fetch_resume(resume.get("url"))

    inputs = ScoreInputs(
        candidate=candidate,
        position_uid=sess.position_uid,
        position_name=sess.position_name,
        position_jd=sess.jd_text,
        class_id=sess.class_id,
        class_name=sess.class_name,
        process_context=process_ctx,
        resume_pdf_b64=resume_pdf_b64,
        resume_url_existed_but_failed=resume_failed,
    )

    try:
        result = score_candidate(inputs)
    except Exception as exc:  # noqa: BLE001
        log.exception("score_candidate failed for %s", candidate_uid)
        summary.error = f"scoring failed: {exc}"
        return summary

    summary.rating = result.rating
    summary.rating_label = _rating_label(result.rating)
    summary.confidence = result.confidence
    summary.summary = result.summary
    summary.strengths = result.strengths
    summary.gaps = result.gaps
    if not summary.linkedin_url and result.linkedin_url and "linkedin.com/in/" in result.linkedin_url:
        summary.linkedin_url = result.linkedin_url.split("?")[0].rstrip("/")

    mark_score_done(summary.candidate_uid)

    # Best-effort tag — apply_rating_tag is a no-op when AUTO_TAG_ENABLED=0.
    try:
        applied = apply_rating_tag(
            summary.candidate_uid, summary.rating,
            position_uid=sess.position_uid, position_name=sess.position_name,
        )
        if applied:
            summary.tag_applied = applied
    except Exception as exc:  # noqa: BLE001
        log.warning("tag application failed for %s: %s", summary.candidate_uid, exc)
        summary.tag_error = str(exc)

    return summary


def finish_scan_batch(session_id: str, processed_uids: list[str]) -> dict:
    sess = load_session(session_id)
    if sess is None:
        return {"ok": False, "error": "Session already finished or expired."}
    delete_session(session_id)
    return {
        "ok": True,
        "committedUids": len(processed_uids),
        "lastReviewAfter": sess.last_review_before,
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _activity_sort_key(c: dict[str, Any]) -> float:
    ts = candidate_max_activity_iso(c)
    return ts.timestamp() if ts else -1.0


def _dedupe_by_identity(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in candidates:
        email = (c.get("email") or "").strip().lower()
        first = (c.get("first_name") or "").strip().lower()
        last = (c.get("last_name") or "").strip().lower()
        key = email or f"{first}|{last}"
        if not key or key == "|":
            out.append(c)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _normalize_url(u: Any) -> str | None:
    if not u or not isinstance(u, str):
        return None
    s = u.strip()
    if not s:
        return None
    if s.startswith("//"):
        return "https:" + s
    return s


def _build_process_context(candidate: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"Status: {candidate.get('status') or ''}")
    lines.append(f"Created: {candidate.get('time_created') or ''}")
    src = candidate.get("source") or {}
    if src.get("name") or src.get("type"):
        lines.append("Source: " + " / ".join(p for p in (src.get("type"), src.get("name")) if p))
    if candidate.get("linkedin_url"):
        lines.append(f"LinkedIn: {candidate['linkedin_url']}")
    completed = candidate.get("completed_steps") or []
    if completed:
        lines.append("Completed steps:")
        for step in completed[:20]:
            lines.append(
                f" - {step.get('name', '')} ({step.get('type', '')})"
                + (f" done {step.get('time_completed')}" if step.get("time_completed") else "")
            )
    current = candidate.get("current_steps") or []
    if current:
        lines.append("Current steps:")
        for step in current:
            lines.append(f" - {step.get('name', '')} ({step.get('type', '')})")
    if (candidate.get("disposition_reason") or {}).get("reason"):
        lines.append(f"Disposition: {candidate['disposition_reason']['reason']}")
    return "\n".join(lines)


def _maybe_fetch_resume(url: str | None) -> tuple[str | None, bool]:
    """Download resume PDF, base64-encode it. Returns (b64_or_None, url_failed_flag)."""
    if not url:
        return None, False
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                log.info("resume URL returned %s for %s", resp.status_code, url[:80])
                return None, True
            content = resp.content or b""
            if not content:
                return None, True
            # 7 MB cap mirrors the Apps Script behaviour.
            if len(content) > 7 * 1024 * 1024:
                log.info("resume too large (%d bytes) — skipping", len(content))
                return None, True
            mime = (resp.headers.get("content-type") or "").lower()
            if "pdf" not in mime:
                # Not a PDF — Comeet sometimes serves DOCX. Skip gracefully.
                return None, False
            return base64.b64encode(content).decode("ascii"), False
    except Exception as exc:  # noqa: BLE001
        log.info("resume fetch failed for %s: %s", url[:80], exc)
        return None, True


_RATING_LABELS = {5: "Superstar", 4: "Great", 3: "OK", 2: "Not a fit", 1: "Way off"}


def _rating_label(rating: int | None) -> str | None:
    if rating is None:
        return None
    return _RATING_LABELS.get(int(rating))


__all__ = [
    "BeginScanResult",
    "CandidateSummary",
    "begin_scan_batch",
    "score_candidate_in_session",
    "finish_scan_batch",
    "is_score_done",
    "mark_score_done",
    "get_last_review_iso",
    "set_last_review_iso",
]
