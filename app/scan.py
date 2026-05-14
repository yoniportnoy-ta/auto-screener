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
    position_recruiter_notes,
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
        position_notes = position_recruiter_notes(position)

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
        position_notes=position_notes,
        recruiter_notes=cls.get("recruiterNotes") or "",
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
    # If we couldn't get a PDF, fall back to Comeet's internal API for a
    # rich profile dump (work history, education, recruiter comments).
    # This gives Claude something to score against instead of returning
    # "no resume content available".
    if not resume_pdf_b64:
        try:
            enrich = _fetch_internal_profile_text(candidate)
            if enrich:
                process_ctx = (process_ctx + "\n\n" + enrich).strip()
        except Exception as exc:  # noqa: BLE001
            log.info("internal-profile enrichment failed: %s", exc)
    else:
        try:
            comments = _fetch_internal_comments_text(candidate)
            if comments:
                process_ctx = (process_ctx + "\n\n" + comments).strip()
        except Exception as exc:  # noqa: BLE001
            log.info("internal-comments fetch failed: %s", exc)

    # Per-position recruiter notes from Comeet's details[] blocks named
    # like "Notes", "Internal", etc.
    if getattr(sess, "position_notes", ""):
        process_ctx = (process_ctx + "\n\n" + sess.position_notes).strip()
    # Persistent recruiter-typed notes from our own DB (textarea on the
    # home page). Applied to every candidate in this batch.
    if getattr(sess, "recruiter_notes", ""):
        process_ctx = (
            process_ctx
            + "\n\n[RECRUITER NOTES on this position — persistent guidance for all candidates]\n"
            + sess.recruiter_notes
        ).strip()

    try:
        fb_ctx = _feedback_context(
            candidate_uid, sess.class_id, sess.class_name,
            position_uid=sess.position_uid,
        )
        if fb_ctx:
            process_ctx = (process_ctx + "\n\n" + fb_ctx).strip()
    except Exception as exc:  # noqa: BLE001
        log.info("feedback-context injection failed: %s", exc)

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
            candidate_url=summary.profile_url,  # numeric ID extracted from this for internal API
            position_uid=sess.position_uid, position_name=sess.position_name,
        )
        if applied:
            summary.tag_applied = applied
    except Exception as exc:  # noqa: BLE001
        log.warning("tag application failed for %s: %s", summary.candidate_uid, exc)
        summary.tag_error = str(exc)

    return summary


def score_one_candidate_now(
    position_uid: str,
    *,
    candidate_uid: str = "",
    numeric_id: str = "",
) -> CandidateSummary:
    """Score a single candidate immediately, bypassing the batched UI flow.

    Called by the Chrome extension when a recruiter opens a candidate page that
    hasn't been scanned yet. Either `candidate_uid` (alphanumeric public-API id)
    or `numeric_id` (the integer in the Comeet app URL) must be provided.

    Side-effects mirror the normal scan: writes to debug_scoring, marks
    score_done, applies the rating tag (if AUTO_TAG_ENABLED), and flags the
    candidate (if AUTO_FLAG_ENABLED + rating >= flag_rating_threshold).
    """
    position_uid = (position_uid or "").strip()
    candidate_uid = (candidate_uid or "").strip()
    numeric_id = (numeric_id or "").strip()
    if not position_uid:
        raise ValueError("position_uid required")
    if not candidate_uid and not numeric_id:
        raise ValueError("Either candidate_uid or numeric_id is required")

    # The Comeet app URL exposes the *numeric* position id (e.g. 437204), but
    # our DB and the public API use the *alphanumeric* uid (e.g. DB.A64).
    # If the caller gave us a numeric id, resolve it before looking up the
    # class assignment.
    if position_uid.isdigit():
        resolved = _resolve_numeric_position_uid(position_uid)
        if resolved:
            log.info("score_one_candidate_now: resolved numeric position %s → %s",
                     position_uid, resolved)
            position_uid = resolved

    cls = get_class_for_position(position_uid)
    if cls is None:
        raise ValueError(
            "No position class selected for this position. Pick one from the UI dropdown first."
        )

    with ComeetClient() as client:
        position = client.get_position(position_uid)
        if not position:
            raise ValueError(f"Position not found: {position_uid}")
        position_name = position.get("name") or position_uid
        jd_text = position_jd_text(position)
        position_notes = position_recruiter_notes(position)

        # Resolve numeric_id → alphanumeric uid by scanning only THIS position's
        # candidates (cheap — one paginated call). Falls back to direct fetch
        # if candidate_uid was already given.
        if not candidate_uid and numeric_id:
            cands = client.list_candidates_for_position(position_uid)
            for c in cands:
                c_url = c.get("URL", "") or ""
                if numeric_id in c_url:
                    candidate_uid = str(c.get("uid") or "")
                    if candidate_uid:
                        break
            if not candidate_uid:
                raise ValueError(
                    f"Candidate {numeric_id} not found in position {position_uid}."
                )

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

    # We don't enforce step / status here — the recruiter is *actively looking*
    # at the candidate, so they obviously want the score regardless of whether
    # the candidate is in the configured screening step.

    process_ctx = _build_process_context(candidate)
    resume_pdf_b64, resume_failed = _maybe_fetch_resume(resume.get("url"))
    # If we couldn't get a PDF, fall back to Comeet's internal API for a
    # rich profile dump (work history, education, recruiter comments).
    # This gives Claude something to score against instead of returning
    # "no resume content available".
    if not resume_pdf_b64:
        try:
            enrich = _fetch_internal_profile_text(candidate)
            if enrich:
                process_ctx = (process_ctx + "\n\n" + enrich).strip()
        except Exception as exc:  # noqa: BLE001
            log.info("internal-profile enrichment failed: %s", exc)
    else:
        try:
            comments = _fetch_internal_comments_text(candidate)
            if comments:
                process_ctx = (process_ctx + "\n\n" + comments).strip()
        except Exception as exc:  # noqa: BLE001
            log.info("internal-comments fetch failed: %s", exc)

    # Per-position recruiter notes from Comeet's details[] blocks named
    # like "Notes", "Internal", etc.
    if position_notes:
        process_ctx = (process_ctx + "\n\n" + position_notes).strip()
    # Persistent recruiter-typed notes from our own DB (the textarea on the
    # home page). Applied to every scan of this position.
    if cls.get("recruiterNotes"):
        process_ctx = (
            process_ctx
            + "\n\n[RECRUITER NOTES on this position — persistent guidance for all candidates]\n"
            + cls["recruiterNotes"]
        ).strip()

    try:
        fb_ctx = _feedback_context(
            candidate_uid, cls["classId"], cls["className"],
            position_uid=position_uid,
        )
        if fb_ctx:
            process_ctx = (process_ctx + "\n\n" + fb_ctx).strip()
    except Exception as exc:  # noqa: BLE001
        log.info("feedback-context injection failed: %s", exc)

    inputs = ScoreInputs(
        candidate=candidate,
        position_uid=position_uid,
        position_name=position_name,
        position_jd=jd_text,
        class_id=cls["classId"],
        class_name=cls["className"],
        process_context=process_ctx,
        resume_pdf_b64=resume_pdf_b64,
        resume_url_existed_but_failed=resume_failed,
    )

    try:
        result = score_candidate(inputs)
    except Exception as exc:  # noqa: BLE001
        log.exception("score_one_candidate_now: scoring failed for %s", candidate_uid)
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

    try:
        applied = apply_rating_tag(
            summary.candidate_uid, summary.rating,
            candidate_url=summary.profile_url,
            position_uid=position_uid, position_name=position_name,
        )
        if applied:
            summary.tag_applied = applied
    except Exception as exc:  # noqa: BLE001
        log.warning("score_one_candidate_now: tag application failed for %s: %s",
                    summary.candidate_uid, exc)
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


def _feedback_context(
    candidate_uid: str,
    class_id: str,
    class_name: str,
    *,
    position_uid: str = "",
) -> str:
    """Build a recruiter-feedback summary to inject into the scoring prompt.

    Three sections, ordered by priority (highest first — Claude weights
    earlier sections more heavily in long prompts):
      0. CALIBRATION VERDICTS for this position. The recruiter's explicit
         👍 / 👎 thumb clicks plus their typed reasons. This is the team's
         direct hire/no-hire signal for the role and the AI is told to
         weight it above everything else.
      1. Past feedback on THIS candidate (newest first). Critical when the
         recruiter has already rated this person — the AI shouldn't ignore
         the prior verdict on Re-grade.
      2. Recent ratings on OTHER candidates in the same position class
         (newest 8 rows with notes). Gives the AI a feel for how the
         recruiter has been calibrating recently, beyond what the learned
         rubric captures.

    Returns "" if there's nothing useful to add.
    """
    from .feedback import list_feedback_for_candidate, list_feedback_for_class
    parts: list[str] = []

    # ─── Section -1: admin-level global brief (applies to ALL positions) ─
    # Comes FIRST so Claude treats it as overarching policy. Per-position
    # briefs in process_ctx remain too — admin brief stacks on top, it
    # doesn't replace anything.
    try:
        from .admin_settings import get_admin_brief
        admin_brief = get_admin_brief()
    except Exception as exc:  # noqa: BLE001
        log.info("feedback_context: admin brief lookup failed: %s", exc)
        admin_brief = ""
    if admin_brief:
        parts.append(
            "[GLOBAL ADMIN GUIDANCE — applies to every position. "
            "Treat as binding hiring policy from the team lead.]"
        )
        parts.append(admin_brief.strip())

    # ─── Section 0: calibration verdicts (max-weight block) ─────────────
    if position_uid:
        try:
            verdicts = _calibration_verdicts_for_position(position_uid, limit=30)
        except Exception as exc:  # noqa: BLE001
            log.info("feedback_context: calibration lookup failed for %s: %s", position_uid, exc)
            verdicts = []
        # Highlight verdicts on THIS specific candidate first; they outrank
        # everything because they're literally about this person.
        own_verdicts = [v for v in verdicts if v["candidate_uid"] == candidate_uid]
        other_verdicts = [v for v in verdicts if v["candidate_uid"] != candidate_uid]

        if own_verdicts:
            parts.append(
                "[CRITICAL — DIRECT THUMB VERDICTS ON THIS EXACT CANDIDATE. "
                "These are the recruiter's binding decisions. If they said 👎, "
                "this person is NOT a fit regardless of other signals. "
                "Mirror their judgment in your rating + reasoning.]"
            )
            for v in own_verdicts[:5]:
                parts.append(_format_calibration_line(v))

        if other_verdicts:
            parts.append(
                "\n[HIGH-WEIGHT — Recent thumb verdicts on OTHER candidates for "
                "this position. These show exactly which profiles the team has "
                "decided to advance or reject. Use them as the strongest "
                "calibration signal — stronger than the learned rubric, "
                "stronger than your general intuition. Look for the patterns: "
                "what got 👍, what got 👎, what got ❓, and why.]"
            )
            for v in other_verdicts[:15]:
                parts.append(_format_calibration_line(v))

    # ─── Section 1: prior 1-5 feedback on THIS candidate ────────────────
    try:
        own = list_feedback_for_candidate(candidate_uid)
    except Exception as exc:  # noqa: BLE001
        log.info("feedback_context: own-feedback lookup failed for %s: %s", candidate_uid, exc)
        own = []
    if own:
        parts.append("\n[Prior feedback on THIS candidate — recruiter has weighed in before]")
        # Newest first; cap at 5.
        for fb in own[:5]:
            ts = fb.timestamp.strftime("%Y-%m-%d") if fb.timestamp else ""
            note = (fb.note or "").strip().replace("\n", " ")
            line = f" - {ts}: recruiter rated {fb.recruiter_rating}/5"
            if fb.ai_rating is not None:
                line += f" (AI had said {fb.ai_rating}/5)"
            if note:
                line += f" — {note[:300]}"
            parts.append(line)

    # ─── Section 2: recent class-wide 1-5 feedback ──────────────────────
    try:
        cls_recent = list_feedback_for_class(class_id, limit=20) if class_id else []
    except Exception as exc:  # noqa: BLE001
        log.info("feedback_context: class-feedback lookup failed: %s", exc)
        cls_recent = []
    # Drop rows for the current candidate (already in section 1) and rows with no note.
    cls_recent = [
        fb for fb in cls_recent
        if fb.candidate_uid != candidate_uid and (fb.note or "").strip()
    ][:8]
    if cls_recent:
        parts.append(f"\n[Recent recruiter feedback for class '{class_name or class_id}' — last {len(cls_recent)} rated candidates]")
        for fb in cls_recent:
            note = (fb.note or "").strip().replace("\n", " ")
            line = f" - rated {fb.recruiter_rating}/5"
            if fb.ai_rating is not None:
                line += f" (AI: {fb.ai_rating}/5)"
            if fb.candidate_name:
                line += f" — {fb.candidate_name}"
            line += f": {note[:240]}"
            parts.append(line)

    return "\n".join(parts) if parts else ""


def _calibration_verdicts_for_position(position_uid: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Pull recent calibration verdicts for this position. Returns plain
    dicts (not ORM rows) so the calling code doesn't accidentally lazy-load
    outside the session.
    """
    from sqlalchemy import desc, select as _select
    from .models import CalibrationVerdict
    out: list[dict[str, Any]] = []
    with db_session() as ses:
        rows = ses.execute(
            _select(CalibrationVerdict)
            .where(CalibrationVerdict.position_uid == position_uid)
            .order_by(desc(CalibrationVerdict.id))
            .limit(limit)
        ).scalars().all()
        for r in rows:
            out.append({
                "recruiter": r.recruiter_name or "",
                "candidate_uid": r.candidate_uid or "",
                "verdict": r.verdict or "",
                "ai_rating": r.ai_rating,
                "feedback_text": (r.feedback_text or "").strip(),
                "created_at": r.created_at.strftime("%Y-%m-%d") if r.created_at else "",
            })
    return out


def _format_calibration_line(v: dict[str, Any]) -> str:
    """Format one calibration verdict as a single prompt line."""
    icon = {"up": "👍 GOOD FIT", "down": "👎 NOT A FIT", "question": "❓ unsure"}.get(
        v.get("verdict") or "", v.get("verdict") or ""
    )
    parts = [f" - {v.get('recruiter') or 'recruiter'}: {icon}"]
    if v.get("ai_rating") is not None:
        parts.append(f"(AI rated {v['ai_rating']}/5)")
    if v.get("feedback_text"):
        # Cap each note so a few verbose notes can't dominate the prompt
        # budget — but keep them long enough to convey real reasoning.
        parts.append(f"— \"{v['feedback_text'][:280]}\"")
    return " ".join(parts)


def _resolve_numeric_position_uid(numeric_id: str) -> str | None:
    """Map Comeet's numeric position id (e.g. '437204' from the app URL) to the
    alphanumeric position uid (e.g. 'DB.A64') used by the public API + our DB.

    Iterates open positions once and matches against any field that looks like
    it might contain the numeric id (id, requisition_id, requisition_number,
    or any URL field). Returns None if no match.
    """
    numeric_id = str(numeric_id).strip()
    if not numeric_id or not numeric_id.isdigit():
        return None
    try:
        with ComeetClient() as client:
            positions = client.list_open_positions()
    except Exception as exc:  # noqa: BLE001
        log.info("_resolve_numeric_position_uid: list failed: %s", exc)
        return None

    for p in positions:
        # Direct numeric-field matches.
        for key in ("id", "requisition_id", "requisition_number", "external_id"):
            v = p.get(key)
            if v is not None and str(v).strip() == numeric_id:
                uid = str(p.get("uid") or "").strip()
                if uid:
                    return uid
        # URL-embedded numeric id (Comeet often returns share URLs that contain
        # the numeric id as a path segment, eg 'jobs/437204/...').
        for key in ("url", "URL", "web_url", "share_url"):
            v = p.get(key)
            if isinstance(v, str) and numeric_id in v:
                uid = str(p.get("uid") or "").strip()
                if uid:
                    return uid
    return None


def _fetch_internal_comments_text(candidate: dict[str, Any]) -> str:
    """Fetch ONLY the recruiter comments from Comeet's internal API. Cheap,
    always-call companion to `_fetch_internal_profile_text` (which does a
    full profile dump only when the CV is missing).

    Returns "" when no comments are present or the call fails.
    """
    from .tagging import numeric_candidate_id_from_url
    numeric_id = numeric_candidate_id_from_url(candidate.get("URL"))
    candidate_uid = str(candidate.get("uid") or "")
    if not numeric_id and not candidate_uid:
        return ""
    try:
        ic = ComeetAppClient()
        data: dict[str, Any] = {}
        for fetcher_name in ("get_candidate_v2", "get_candidate"):
            fetcher = getattr(ic, fetcher_name, None)
            if not fetcher:
                continue
            try:
                data = fetcher(numeric_id or candidate_uid) or {}
                if data:
                    break
            except Exception:  # noqa: BLE001
                continue
        if not data:
            return ""
    except Exception as exc:  # noqa: BLE001
        log.info("comments fetch failed: %s", exc)
        return ""

    comments = data.get("comments") or data.get("notes") or []
    if not isinstance(comments, list) or not comments:
        return ""
    lines: list[str] = ["[Recruiter notes on the Comeet profile]"]
    for c in comments[:10]:
        if not isinstance(c, dict):
            continue
        txt = c.get("text") or c.get("body") or c.get("content") or ""
        who = c.get("author") or c.get("created_by") or ""
        when = c.get("created_at") or c.get("time") or ""
        if isinstance(txt, str) and txt.strip():
            lines.append(f" - {who} {when}: {txt.strip()[:600]}".strip())
    return "\n".join(lines) if len(lines) > 1 else ""


def _fetch_internal_profile_text(candidate: dict[str, Any]) -> str:
    """Fall back to Comeet's internal API for a richer profile when the
    resume PDF isn't fetchable. Returns a plain-text dump suitable for the
    scoring prompt, or "" if we can't get anything useful.

    Called only when `_maybe_fetch_resume` returned no PDF. The internal API
    requires the recruiter cookie + CSRF dance we already do for tagging.
    """
    from .tagging import numeric_candidate_id_from_url
    numeric_id = numeric_candidate_id_from_url(candidate.get("URL"))
    candidate_uid = str(candidate.get("uid") or "")
    if not numeric_id and not candidate_uid:
        return ""

    try:
        ic = ComeetAppClient()
        data: dict[str, Any] = {}
        # Try v2 first — richer payload.
        for fetcher_name in ("get_candidate_v2", "get_candidate"):
            fetcher = getattr(ic, fetcher_name, None)
            if not fetcher:
                continue
            try:
                data = fetcher(numeric_id or candidate_uid) or {}
                if data:
                    break
            except Exception as exc:  # noqa: BLE001
                log.info("internal-API %s failed: %s", fetcher_name, exc)
                continue
        if not data:
            return ""
    except Exception as exc:  # noqa: BLE001
        log.info("internal API auth failed, skipping enrichment: %s", exc)
        return ""

    parts: list[str] = ["", "[INTERNAL PROFILE — fallback because no fetchable CV]"]

    # Headline / summary / about
    for key in ("headline", "summary", "about", "title"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(f"{key.title()}: {val.strip()}")

    # Person fields (sometimes nested)
    person = data.get("person") if isinstance(data.get("person"), dict) else {}
    for key in ("first_name", "last_name", "email", "phone", "location", "city", "country"):
        val = (data.get(key) if data.get(key) else person.get(key)) if person else data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(f"{key.replace('_', ' ').title()}: {val.strip()}")

    # Work experience
    work = data.get("work_experience") or data.get("workExperience") or data.get("experience") or []
    if isinstance(work, list) and work:
        parts.append("\nWork experience:")
        for w in work[:15]:
            if not isinstance(w, dict):
                continue
            company = w.get("company") or w.get("company_name") or ""
            title = w.get("title") or w.get("role") or w.get("position") or ""
            start = w.get("start_date") or w.get("from") or ""
            end = w.get("end_date") or w.get("to") or ("present" if w.get("current") else "")
            desc = w.get("description") or w.get("summary") or ""
            line = f" - {title} at {company} ({start} → {end})".rstrip()
            parts.append(line)
            if desc and isinstance(desc, str):
                parts.append(f"   {desc.strip()[:600]}")

    # Education
    edu = data.get("education") or data.get("education_history") or []
    if isinstance(edu, list) and edu:
        parts.append("\nEducation:")
        for e in edu[:10]:
            if not isinstance(e, dict):
                continue
            school = e.get("school") or e.get("institution") or ""
            degree = e.get("degree") or ""
            field = e.get("field_of_study") or e.get("field") or ""
            start = e.get("start_date") or e.get("from") or ""
            end = e.get("end_date") or e.get("to") or ""
            parts.append(f" - {degree} {field} at {school} ({start} → {end})".strip())

    # Skills
    skills = data.get("skills") or []
    if isinstance(skills, list) and skills:
        flat = [s.get("name") if isinstance(s, dict) else str(s) for s in skills]
        flat = [x for x in flat if x]
        if flat:
            parts.append("\nSkills: " + ", ".join(flat[:40]))

    # Links / socials
    links = data.get("links") or data.get("social_links") or []
    if isinstance(links, list) and links:
        for ln in links[:10]:
            if isinstance(ln, dict):
                url = ln.get("url") or ln.get("href")
                kind = ln.get("type") or ln.get("name")
                if url:
                    parts.append(f"Link: {kind or ''} {url}".strip())
            elif isinstance(ln, str):
                parts.append(f"Link: {ln}")

    # Recruiter comments / notes
    comments = data.get("comments") or data.get("notes") or []
    if isinstance(comments, list) and comments:
        parts.append("\nRecruiter notes:")
        for c in comments[:10]:
            if not isinstance(c, dict):
                continue
            txt = c.get("text") or c.get("body") or c.get("content") or ""
            who = c.get("author") or c.get("created_by") or ""
            when = c.get("created_at") or c.get("time") or ""
            if isinstance(txt, str) and txt.strip():
                parts.append(f" - {who} {when}: {txt.strip()[:600]}".strip())

    text = "\n".join(p for p in parts if p is not None)
    return text if len(text) > 50 else ""  # only return when we actually got something


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
