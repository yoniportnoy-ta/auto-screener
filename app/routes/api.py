"""JSON API for the recruiter UI.

Replaces the `google.script.run.<fn>` calls in the Apps Script Index.html.
Same conceptual endpoints, returning the same shape so the JS port stays minimal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ..comeet_client import (
    ComeetClient,
    position_country,
    position_lead_recruiter,
)
from ..config import settings
from ..feedback import save_feedback
from ..position_classes import (
    assign_position_class,
    create_custom_class,
    get_industry_preferences,
    get_position_class,
    list_all_classes,
    list_auto_screen_positions,
    set_auto_screen_enabled,
    set_industry_preferences,
    set_recruiter_notes,
)
from ..scan import score_one_candidate_now

log = logging.getLogger(__name__)
router = APIRouter()


def _require_extension_token(
    x_screener_token: str | None = Header(default=None, alias="X-Screener-Token"),
) -> None:
    """Gate the /extension/* endpoints with the shared SCREENER_API_TOKEN.

    The Chrome extension stores the token in `chrome.storage.local` (configured
    via the popup) and sends it on every request as `X-Screener-Token`.
    Token of "changeme" is rejected even if it happens to match — that's the
    Settings default and means the deploy hasn't configured a real secret.
    """
    expected = (settings.screener_api_token or "").strip()
    if not expected or expected == "changeme":
        raise HTTPException(503, "extension auth not configured on server")
    if not x_screener_token or x_screener_token.strip() != expected:
        raise HTTPException(401, "invalid or missing X-Screener-Token")


# ─── Positions ───────────────────────────────────────────────────────────────
@router.get("/positions")
def list_open_positions() -> list[dict[str, Any]]:
    """List open positions, shaped for the UI dropdown."""
    with ComeetClient() as client:
        positions = client.list_open_positions()
    return [
        {
            "uid": str(p["uid"]),
            "name": str(p.get("name") or ""),
            "department": str(p.get("department") or ""),
            "leadRecruiter": position_lead_recruiter(p),
            "location": position_country(p),
        }
        for p in positions
    ]


@router.get("/position/in-step-counts")
def position_in_step_counts(position_uid: str) -> dict[str, Any]:
    """Slow stat: candidates currently sitting in CV-screen step for this
    position (total + how many of those haven't been AI-scored yet).

    Split out from /position/dashboard so the dashboard returns instantly
    on DB-only data; this Comeet-dependent call streams in afterward.
    """
    from sqlalchemy import select
    from ..comeet_client import ComeetClient, candidate_in_allowed_step
    from ..db import db_session
    from ..models import DebugScoring

    pos_uid = (position_uid or "").strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    with db_session() as ses:
        scored_uids = set(ses.scalars(
            select(DebugScoring.candidate_uid).where(DebugScoring.position_uid == pos_uid).distinct()
        ).all()) - {None, ""}

    try:
        with ComeetClient() as client:
            cands = client.list_candidates_for_position(pos_uid)
    except Exception as exc:  # noqa: BLE001
        log.info("in-step-counts: %s", exc)
        return {"inStepTotal": None, "unscoredInStep": None}

    in_step = [c for c in cands if c.get("uid") and candidate_in_allowed_step(c)]
    return {
        "inStepTotal": len(in_step),
        "unscoredInStep": sum(1 for c in in_step if str(c["uid"]) not in scored_uids),
    }


# Module-level cache for /positions/unscreened-counts.
# The Comeet fan-out is slow (~1-2 min cold), so we keep results in memory for
# an hour. A background warmer (see app/main.py) refreshes one position at a
# time every 50 min so the cache effectively never goes cold during a single
# session — meaning the recruiter only ever pays the cold-fetch cost on the
# very first page load after a container restart.
_UNSCREENED_CACHE: dict[str, tuple[int, float]] = {}
_UNSCREENED_CACHE_TTL_SECONDS = 3600


def compute_unscreened_counts(fresh: bool = False) -> dict[str, int]:
    """Shared implementation for /positions/unscreened-counts. Also used by
    the background warmer in app.main lifespan so the cache is populated
    before the first recruiter request.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time
    from ..comeet_client import ComeetClient, candidate_in_allowed_step

    with ComeetClient() as pub:
        positions = pub.list_open_positions()
        pos_uids = [str(p["uid"]) for p in positions if p.get("uid")]

    now = _time.time()
    counts: dict[str, int] = {}
    missing: list[str] = []

    for u in pos_uids:
        if not fresh:
            cached = _UNSCREENED_CACHE.get(u)
            if cached and (now - cached[1]) < _UNSCREENED_CACHE_TTL_SECONDS:
                counts[u] = cached[0]
                continue
        missing.append(u)

    log.info(
        "unscreened-counts: %d cached, %d to fetch (fresh=%s)",
        len(counts), len(missing), fresh,
    )

    if not missing:
        return counts

    def _count_in_step(pos_uid: str) -> tuple[str, int]:
        try:
            with ComeetClient() as client:
                cands = client.list_candidates_for_position(pos_uid)
        except Exception as exc:  # noqa: BLE001
            log.info("unscreened-counts: %s: %s", pos_uid, exc)
            return pos_uid, -1
        cnt = sum(1 for c in cands if c.get("uid") and candidate_in_allowed_step(c))
        return pos_uid, cnt

    # Keep this small (4, not 12). Each worker holds a curl_cffi session +
    # potentially a 2captcha solver, so 12 in parallel was OOM-killing the
    # 512 MB starter instance during prewarm. 4 still finishes ~30 positions
    # in well under a minute.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in as_completed(pool.submit(_count_in_step, u) for u in missing):
            pos_uid, cnt = future.result()
            counts[pos_uid] = cnt
            if cnt >= 0:
                _UNSCREENED_CACHE[pos_uid] = (cnt, now)
    return counts


@router.get("/positions/unscreened-counts")
def positions_unscreened_counts(fresh: bool = False) -> dict[str, int]:
    """For every open position, return how many candidates are currently
    sitting in the CV-screening pipeline step (in-memory cached, 5 min TTL).

    Pass ?fresh=1 to force a re-fetch (bypasses cache).
    """
    return compute_unscreened_counts(fresh=fresh)


# ─── Position dashboard ──────────────────────────────────────────────────────
@router.get("/position/dashboard")
def position_dashboard(position_uid: str, recent_limit: int = 20) -> dict[str, Any]:
    """Aggregate everything a recruiter wants to know about a single position:
    class assignment, scan stats, agreement rate, recent scored candidates.

    Designed for the home-page "position dashboard" view so the recruiter
    sees one screen per position instead of three parallel mode cards.
    """
    from sqlalchemy import select, func, desc
    from ..db import db_session
    from ..models import DebugScoring, Feedback

    pos_uid = (position_uid or "").strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    cls = get_position_class(pos_uid)

    with db_session() as ses:
        # Stats — DB only, fast.
        total_scored = int(ses.scalar(
            select(func.count()).select_from(DebugScoring).where(DebugScoring.position_uid == pos_uid)
        ) or 0)
        last_scan_at = ses.scalar(
            select(func.max(DebugScoring.timestamp)).where(DebugScoring.position_uid == pos_uid)
        )

    # Unscored-in-step requires a Comeet call (slow). Split out into a
    # separate endpoint (/position/in-step-counts) so the dashboard stays
    # fast and the recruiter sees the basic stats immediately.
    unscored_in_step: int | None = None
    in_step_total: int | None = None

    with db_session() as ses:

        # Agreement = count(rows where recruiter_rating == ai_rating) / count(rows where both present)
        feedback_rows = ses.scalars(
            select(Feedback).where(Feedback.position_uid == pos_uid)
        ).all()
        feedback_count = len(feedback_rows)
        with_both = [f for f in feedback_rows if f.ai_rating is not None and f.recruiter_rating is not None]
        agreed = sum(1 for f in with_both if f.ai_rating == f.recruiter_rating)
        agreement = (agreed / len(with_both)) if with_both else None

        # Recent scored candidates — newest first
        recent_rows = ses.scalars(
            select(DebugScoring)
            .where(DebugScoring.position_uid == pos_uid)
            .order_by(desc(DebugScoring.timestamp))
            .limit(max(1, min(100, recent_limit)))
        ).all()
        recent = [
            {
                "candidateUid": r.candidate_uid,
                "candidateName": r.candidate_name or "",
                "rating": r.final_rating,
                "confidence": r.confidence,
                "summary": r.summary or "",
                "scoredAt": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in recent_rows
        ]

    return {
        "positionUid": pos_uid,
        "positionName": cls["className"] if cls else None,  # best-effort; ideally fetch from Comeet
        "class": cls,  # {classId, className, level, autoScreenEnabled} or null
        "stats": {
            "totalScored": total_scored,
            "feedbackCount": feedback_count,
            "agreement": agreement,
            "lastScanAt": last_scan_at.isoformat() if last_scan_at else None,
            "unscoredInStep": unscored_in_step,
            "inStepTotal": in_step_total,
        },
        "recent": recent,
    }


class PositionClearBody(BaseModel):
    position_uid: str = Field(min_length=1)


@router.post("/position/clear-scores")
def position_clear_scores(body: PositionClearBody) -> dict[str, Any]:
    """Wipe the AI's record for this position: debug_scoring rows, applied_tags
    rows, Comeet tag/flag state, and score-done locks.

    Useful when the recruiter wants to start fresh — e.g. after a JD change
    or significant rubric drift. Confirmation is the UI's responsibility.
    """
    from sqlalchemy import delete, select
    from ..db import db_session
    from ..models import AppliedTag, CandidateLock, DebugScoring
    from ..tagging import remove_rating_tags, numeric_candidate_id_from_url
    from ..comeet_app_client import ComeetAppClient, ComeetAppError
    from ..comeet_client import ComeetClient

    pos_uid = body.position_uid.strip()
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    # Collect candidates that have AI tags/flags so we know what to scrub on Comeet's side.
    with db_session() as ses:
        applied = ses.scalars(
            select(AppliedTag).where(AppliedTag.position_uid == pos_uid)
        ).all()
        candidates_with_tags = sorted({a.candidate_uid for a in applied if a.candidate_uid})

    # Remove tags + flag from Comeet — best effort, don't fail the whole call if one errors.
    tag_errors = 0
    flag_errors = 0
    if candidates_with_tags:
        ic = ComeetAppClient()
        for uid in candidates_with_tags:
            try:
                remove_rating_tags(uid, client=ic)
            except Exception:  # noqa: BLE001
                tag_errors += 1
            # Clear is_favorite — need numeric id, fetch via public API once.
            try:
                with ComeetClient() as pub:
                    cand = pub.get_candidate(uid)
                numeric_id = numeric_candidate_id_from_url(cand.get("URL")) if cand else None
                if numeric_id:
                    ic.set_candidate_flag(numeric_id, False)
            except (ComeetAppError, Exception):  # noqa: BLE001
                flag_errors += 1

    # Wipe DB state.
    with db_session() as ses:
        deleted_scoring = ses.execute(
            delete(DebugScoring).where(DebugScoring.position_uid == pos_uid)
        ).rowcount or 0
        deleted_tags = ses.execute(
            delete(AppliedTag).where(AppliedTag.position_uid == pos_uid)
        ).rowcount or 0
        # Drop score-done locks keyed by candidate uid.
        score_done_keys = [f"score_done:{u}" for u in candidates_with_tags]
        deleted_locks = 0
        if score_done_keys:
            deleted_locks = ses.execute(
                delete(CandidateLock).where(CandidateLock.key.in_(score_done_keys))
            ).rowcount or 0

    return {
        "ok": True,
        "positionUid": pos_uid,
        "deletedScoring": int(deleted_scoring),
        "deletedTags": int(deleted_tags),
        "deletedLocks": int(deleted_locks),
        "tagRemovalErrors": tag_errors,
        "flagRemovalErrors": flag_errors,
        "candidatesAffected": len(candidates_with_tags),
    }


class PositionRescoreBody(BaseModel):
    position_uid: str = Field(min_length=1)


@router.post("/position/full-reset")
def position_full_reset(body: PositionRescoreBody) -> dict[str, Any]:
    """Wipe everything for ONE position. Used to start a fresh calibration
    session on a position without affecting any other position.

    Deletes:
      - debug_scoring rows for this position_uid
      - calibration_verdicts rows for this position_uid
      - feedback rows for this position_uid
      - recruiter_thresholds for this position_uid (every recruiter)
      - position-specific learned_rubric (the (class_id, position_uid)
        row in learned_rubrics; the class-level rubric stays intact)
      - score_done locks for every candidate in this position's Comeet pool

    Preserves:
      - position_classes (class assignment + recruiter notes)
      - applied_tags (so re-tagging is idempotent if needed)
      - the class-level learned_rubric (used by other positions in the
        same class as cold-start)

    Mirrors `python -m app.cli reset-position <position_uid>` from cli.py
    so this can be triggered without a working shell.
    """
    from sqlalchemy import delete
    from ..comeet_client import ComeetClient
    from ..db import db_session
    from ..models import (
        CalibrationVerdict, CandidateEnrichment, CandidateLock, DebugScoring,
        Feedback, LearnedRubric, RecruiterThreshold,
    )

    pos_uid = body.position_uid.strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")

    counts: dict[str, int] = {}
    with db_session() as ses:
        counts["debug_scoring"] = ses.execute(
            delete(DebugScoring).where(DebugScoring.position_uid == pos_uid)
        ).rowcount or 0
        counts["calibration_verdicts"] = ses.execute(
            delete(CalibrationVerdict).where(CalibrationVerdict.position_uid == pos_uid)
        ).rowcount or 0
        counts["feedback"] = ses.execute(
            delete(Feedback).where(Feedback.position_uid == pos_uid)
        ).rowcount or 0
        counts["recruiter_thresholds"] = ses.execute(
            delete(RecruiterThreshold).where(RecruiterThreshold.position_uid == pos_uid)
        ).rowcount or 0
        counts["position_rubric"] = ses.execute(
            delete(LearnedRubric).where(LearnedRubric.position_uid == pos_uid)
        ).rowcount or 0

    # Score-done locks AND candidate_enrichment rows are keyed by
    # candidate uid (not position uid), so we need the current Comeet
    # candidate list to know which rows are scoped to this position.
    # Both get wiped — the recruiter expects "fresh calibration" to mean
    # *every* persisted artifact tied to this position is gone.
    try:
        with ComeetClient() as client:
            candidates = client.list_candidates_for_position(pos_uid)
    except Exception:  # noqa: BLE001
        # Comeet hiccup — skip the candidate-keyed wipes rather than
        # failing the whole reset.
        counts["score_done_locks"] = -1
        counts["candidate_enrichment"] = -1
    else:
        uids = {str(c.get("uid")) for c in candidates if c.get("uid")}
        if uids:
            keys = [f"score_done:{u}" for u in uids]
            with db_session() as ses:
                counts["score_done_locks"] = ses.execute(
                    delete(CandidateLock).where(CandidateLock.key.in_(keys))
                ).rowcount or 0
                counts["candidate_enrichment"] = ses.execute(
                    delete(CandidateEnrichment).where(
                        CandidateEnrichment.candidate_uid.in_(uids)
                    )
                ).rowcount or 0
        else:
            counts["score_done_locks"] = 0
            counts["candidate_enrichment"] = 0

    return {"ok": True, "positionUid": pos_uid, **counts}


@router.post("/position/clear-locks")
def position_clear_locks(body: PositionRescoreBody) -> dict[str, Any]:
    """Clear `score_done` locks for every candidate currently in this
    position's Comeet pipeline.

    The locks live in `candidate_locks` and persist across debug_scoring
    wipes — so after a global `reset-debug-scoring` the scan flow still
    treats those candidates as "already scored" and skips them. This
    endpoint fixes that: pulls the current candidate list from Comeet
    and DELETEs the matching `score_done:<uid>` rows.

    Mirrors `python -m app.cli clear-score-locks <position_uid>` from
    cli.py. Exists as an HTTP route so the operator can trigger it
    without a working shell.
    """
    from sqlalchemy import delete
    from ..comeet_client import ComeetClient
    from ..db import db_session
    from ..models import CandidateLock

    pos_uid = body.position_uid.strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")
    try:
        with ComeetClient() as client:
            candidates = client.list_candidates_for_position(pos_uid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Comeet API failed: {exc}")
    uids = {str(c.get("uid")) for c in candidates if c.get("uid")}
    if not uids:
        return {"ok": True, "deletedLocks": 0, "candidateCount": 0}
    keys = [f"score_done:{u}" for u in uids]
    with db_session() as ses:
        deleted = ses.execute(
            delete(CandidateLock).where(CandidateLock.key.in_(keys))
        ).rowcount or 0
    return {
        "ok": True,
        "positionUid": pos_uid,
        "deletedLocks": int(deleted),
        "candidateCount": len(uids),
    }


@router.post("/position/rescore-all")
def position_rescore_all(body: PositionRescoreBody) -> dict[str, Any]:
    """Re-score every previously-scored candidate for this position who is
    still in the configured CV-screening pipeline step.

    Synchronous — for positions with many candidates this can take 5–30
    minutes. UI should warn before invoking. Candidates who have moved past
    CV screening are skipped (we don't want to spend Anthropic credits
    re-scoring someone already in an interview).
    """
    from sqlalchemy import select
    from ..comeet_client import ComeetClient, candidate_in_allowed_step
    from ..db import db_session
    from ..models import DebugScoring
    from ..scan import score_one_candidate_now

    pos_uid = body.position_uid.strip()
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    # Previously-scored uids (one DB query).
    with db_session() as ses:
        scored_uids = set(ses.scalars(
            select(DebugScoring.candidate_uid)
            .where(DebugScoring.position_uid == pos_uid)
            .distinct()
        ).all()) - {None, ""}

    # Restrict to candidates currently in the allowed step (one Comeet call).
    try:
        with ComeetClient() as client:
            current = client.list_candidates_for_position(pos_uid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Comeet API failed: {exc}")
    eligible_uids = [
        str(c.get("uid") or "")
        for c in current
        if c.get("uid") and str(c["uid"]) in scored_uids and candidate_in_allowed_step(c)
    ]
    skipped_not_in_step = len(scored_uids) - len(eligible_uids)

    rescored = 0
    errors: list[str] = []
    for uid in eligible_uids:
        try:
            score_one_candidate_now(pos_uid, candidate_uid=uid)
            rescored += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("rescore-all: failed for %s: %s", uid, exc)
            errors.append(f"{uid}: {exc}")

    return {
        "ok": True,
        "positionUid": pos_uid,
        "totalScored": len(scored_uids),
        "eligibleInStep": len(eligible_uids),
        "skippedNotInStep": skipped_not_in_step,
        "rescored": rescored,
        "errorCount": len(errors),
        "errors": errors[:10],
    }


@router.get("/position/breakdown")
def position_breakdown(position_uid: str) -> dict[str, Any]:
    """5-column breakdown: candidates grouped by the AI's rating, each column
    further split into "with recruiter feedback" (showing the recruiter's
    counter-rating) and "no feedback yet".

    Lets the recruiter see at a glance: how the AI distributed scores across
    candidates, and which AI calls have been validated/corrected.
    """
    from sqlalchemy import select, desc
    from ..db import db_session
    from ..models import DebugScoring, Feedback

    pos_uid = (position_uid or "").strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    with db_session() as ses:
        scoring_rows = ses.scalars(
            select(DebugScoring)
            .where(DebugScoring.position_uid == pos_uid)
            .order_by(desc(DebugScoring.timestamp))
        ).all()
        # Most-recent recruiter rating per candidate.
        feedback_rows = ses.scalars(
            select(Feedback)
            .where(Feedback.position_uid == pos_uid)
            .order_by(desc(Feedback.timestamp))
        ).all()

    recruiter_by_uid: dict[str, dict[str, Any]] = {}
    for f in feedback_rows:
        if f.candidate_uid in recruiter_by_uid:
            continue
        recruiter_by_uid[f.candidate_uid] = {
            "rating": f.recruiter_rating,
            "note": (f.note or "").strip()[:240],
        }

    # One Comeet call to grab profile URLs for every candidate in the position.
    # Lets us render each breakdown row as a clickable link straight to Comeet.
    url_by_uid: dict[str, str] = {}
    try:
        with ComeetClient() as client:
            for c in client.list_candidates_for_position(pos_uid):
                uid = str(c.get("uid") or "")
                u = c.get("URL") or ""
                if uid and isinstance(u, str) and u:
                    url_by_uid[uid] = u
    except Exception as exc:  # noqa: BLE001
        log.info("breakdown: profile URL fetch failed: %s", exc)

    columns: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    for ai in range(1, 6):
        with_feedback: list[dict[str, Any]] = []
        without_feedback: list[dict[str, Any]] = []
        for r in scoring_rows:
            uid = r.candidate_uid or ""
            if uid in seen_uids or int(r.final_rating or 0) != ai:
                continue
            seen_uids.add(uid)
            item = {
                "candidateUid": uid,
                "candidateName": r.candidate_name or "",
                "profileUrl": url_by_uid.get(uid, ""),
                "aiRating": ai,
                "scoredAt": r.timestamp.isoformat() if r.timestamp else None,
                "summary": (r.summary or "")[:160],
            }
            fb = recruiter_by_uid.get(uid)
            if fb:
                item["recruiterRating"] = fb["rating"]
                item["recruiterNote"] = fb["note"]
                with_feedback.append(item)
            else:
                without_feedback.append(item)
        columns.append({
            "aiRating": ai,
            "label": ({1: "Way off", 2: "Not a fit", 3: "OK", 4: "Great", 5: "Superstar"})[ai],
            "withFeedback": with_feedback,
            "withoutFeedback": without_feedback,
        })

    return {"positionUid": pos_uid, "columns": columns}


@router.get("/position/agreement-matrix")
def position_agreement_matrix(position_uid: str) -> dict[str, Any]:
    """Cross-tab of AI rating vs recruiter rating for this position.

    Returns a 5x5 matrix `counts[ai][rec]` (1..5 each) of how many feedback
    rows fall into each AI-rating × recruiter-rating cell. Lets the recruiter
    see at a glance where the AI agrees, where it's too harsh, and where it's
    too lenient — much more useful than a single agreement %.
    """
    from sqlalchemy import select, func
    from ..db import db_session
    from ..models import Feedback

    pos_uid = (position_uid or "").strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    # counts[ai][rec] — both 1..5; index 0 unused for ease of mapping.
    counts = [[0] * 6 for _ in range(6)]
    with db_session() as ses:
        rows = ses.execute(
            select(Feedback.ai_rating, Feedback.recruiter_rating, func.count())
            .where(Feedback.position_uid == pos_uid)
            .where(Feedback.ai_rating.isnot(None))
            .where(Feedback.recruiter_rating.isnot(None))
            .group_by(Feedback.ai_rating, Feedback.recruiter_rating)
        ).all()
    for ai, rec, c in rows:
        if 1 <= int(ai) <= 5 and 1 <= int(rec) <= 5:
            counts[int(ai)][int(rec)] = int(c)

    # Summary stats:
    total = sum(counts[i][j] for i in range(1, 6) for j in range(1, 6))
    agreed = sum(counts[i][i] for i in range(1, 6))
    # Bias = mean(ai_rating - recruiter_rating). Positive = AI rates too high.
    sum_delta = 0
    for i in range(1, 6):
        for j in range(1, 6):
            sum_delta += (i - j) * counts[i][j]
    bias = (sum_delta / total) if total else None

    # Trim to 5x5 matrix indexed 0..4 for cleaner JSON.
    matrix = [[counts[i][j] for j in range(1, 6)] for i in range(1, 6)]
    return {
        "positionUid": pos_uid,
        "matrix": matrix,             # matrix[ai-1][rec-1] = count
        "ratings": [1, 2, 3, 4, 5],
        "totalRated": total,
        "agreed": agreed,
        "agreement": (agreed / total) if total else None,
        "bias": bias,                  # mean(ai - rec); >0 means AI too generous
    }


# ─── Position class management ───────────────────────────────────────────────
@router.get("/position-classes")
def get_classes() -> list[dict[str, Any]]:
    return list_all_classes()


@router.get("/position-class/{position_uid}")
def get_class_for_position(position_uid: str) -> dict[str, Any]:
    cls = get_position_class(position_uid)
    return cls or {}


class AssignClassBody(BaseModel):
    position_uid: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    level: str = ""


@router.post("/position-class")
def assign_class(body: AssignClassBody) -> dict[str, Any]:
    try:
        return assign_position_class(body.position_uid, body.class_id, body.level)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class CreateClassBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    levels: list[str] = Field(default_factory=list)


@router.post("/position-classes")
def create_class(body: CreateClassBody) -> dict[str, Any]:
    try:
        return create_custom_class(body.name, body.levels)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class AutoScreenBody(BaseModel):
    position_uid: str = Field(min_length=1)
    enabled: bool


@router.post("/position-class/auto-screen")
def toggle_auto_screen(body: AutoScreenBody) -> dict[str, Any]:
    try:
        return set_auto_screen_enabled(body.position_uid, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class RecruiterNotesBody(BaseModel):
    position_uid: str = Field(min_length=1)
    notes: str = ""


@router.post("/position-class/notes")
def post_recruiter_notes(body: RecruiterNotesBody) -> dict[str, Any]:
    """Save free-form recruiter notes for a position. Injected into the
    scoring prompt on every future scan for this position."""
    try:
        return set_recruiter_notes(body.position_uid, body.notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/auto-screen/positions")
def list_auto_screen() -> list[str]:
    """Position UIDs the cron currently scans (debug helper)."""
    return list_auto_screen_positions()


# ─── Chrome extension endpoints ──────────────────────────────────────────────
def _suggest_class_for(position_name: str, classes: list[dict[str, Any]]) -> str | None:
    """Crude name-matcher: best class whose name shares tokens with the
    position name. Returns class_id or None if no good match.
    """
    import re
    if not position_name:
        return None
    pname_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", position_name) if len(t) > 1}
    if not pname_tokens:
        return None
    # Hand-curated alias hints — boost certain class matches even when the
    # position name doesn't share a token directly.
    aliases: dict[str, set[str]] = {
        "it": {"it", "helpdesk", "sysadmin", "support"},
        "qa": {"qa", "test", "tester", "quality"},
        "backend": {"backend", "server", "api"},
        "frontend_fullstack": {"frontend", "fullstack", "ui", "client", "react"},
        "devops_security": {"devops", "sre", "security", "infrastructure"},
        "nlp": {"nlp", "ml", "research", "scientist"},
        "talent_acquisition": {"recruiter", "talent", "sourcer", "hr"},
        "product_management": {"product", "pm"},
        "customer_success": {"customer", "success", "csm"},
        "business_development": {"business", "bd", "partnerships"},
        "engineering_leadership": {"engineering", "manager", "director", "head", "lead"},
        "analytical_engineering": {"analytics", "data", "analyst"},
        "controller": {"controller", "finance", "accounting"},
        "account_executive": {"account", "ae", "sales"},
        "knowledge_base_writer": {"knowledge", "writer", "content"},
        "revenue_operations": {"revops", "revenue", "ops"},
    }
    best_id: str | None = None
    best_score = 0
    for c in classes:
        cid, cname = c["id"], c["name"]
        cname_tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", cname) if len(t) > 1}
        # Score: token overlap + alias bonus.
        score = len(pname_tokens & cname_tokens) * 2
        alias_hits = pname_tokens & aliases.get(cid, set())
        score += len(alias_hits) * 3
        if score > best_score:
            best_score = score
            best_id = cid
    return best_id if best_score >= 2 else None


@router.get("/extension/ping", dependencies=[Depends(_require_extension_token)])
def extension_ping() -> dict[str, Any]:
    """Cheap connectivity + token check for the popup's 'Test connection' button.

    Returning early here means the popup doesn't accidentally trigger the
    expensive numeric→alphanumeric search inside /score.
    """
    return {"ok": True}


@router.get("/extension/score", dependencies=[Depends(_require_extension_token)])
def extension_get_score(
    numeric_id: str = "",
    uid: str = "",
    position_uid: str = "",
) -> dict[str, Any]:
    """Used by the in-Comeet Chrome extension.

    Accepts either a numeric candidate ID (what Comeet's URL contains) or the
    alphanumeric public-API uid. Returns the latest scoring summary we have on
    record (rating, confidence, summary, strengths, gaps), or 404 if we've never
    scored this candidate.

    If `position_uid` is supplied (the extension always does — it's in the page
    URL), the numeric→alphanumeric resolution only scans THAT position's
    candidate list, which is ~10–50× faster than scanning every open position.
    """
    from sqlalchemy import select
    from ..comeet_client import ComeetClient
    from ..db import db_session
    from ..models import AppliedTag, DebugScoring

    alphanumeric_uid = (uid or "").strip()
    n_id = (numeric_id or "").strip()
    pos_uid = (position_uid or "").strip()

    # Fast-fail if the caller gave us a numeric_id that obviously isn't a
    # Comeet candidate id (only digits). Keeps the popup's "Test connection"
    # ping from triggering the multi-minute candidate scan below.
    if n_id and not n_id.isdigit():
        raise HTTPException(404, "numeric_id must be all digits")

    # Resolve numeric position id (URL form, e.g. '437204') → alphanumeric uid
    # (e.g. 'DB.A64') so list_candidates_for_position hits the right list.
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    # If the extension only gave us a numeric id, look up the public uid via
    # the public Comeet API (it has a `URL` field with the numeric id embedded).
    if n_id and not alphanumeric_uid:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ComeetClient() as pub:
            try:
                # Fast path: just this candidate's position.
                if pos_uid:
                    cands = pub.list_candidates_for_position(pos_uid)
                    for c in cands:
                        c_url = c.get("URL", "") or ""
                        if n_id in c_url:
                            alphanumeric_uid = str(c.get("uid") or "")
                            break
                # Fallback (rare): brute-force across all open positions, but
                # parallelised so we don't spend 60–90s walking ~30 positions
                # sequentially when a recruiter opens /app/can/<id> without
                # position context.
                if not alphanumeric_uid:
                    positions = pub.list_open_positions()
                    other_uids = [
                        str(p["uid"]) for p in positions
                        if p.get("uid") and p.get("uid") != pos_uid
                    ]

                    def _find_in_pos(p_uid: str) -> str:
                        try:
                            with ComeetClient() as client:
                                cands = client.list_candidates_for_position(p_uid)
                            for c in cands:
                                c_url = c.get("URL", "") or ""
                                if n_id in c_url:
                                    return str(c.get("uid") or "")
                        except Exception:  # noqa: BLE001
                            return ""
                        return ""

                    with ThreadPoolExecutor(max_workers=4) as pool:
                        futures = [pool.submit(_find_in_pos, u) for u in other_uids]
                        for fut in as_completed(futures):
                            found = fut.result()
                            if found:
                                alphanumeric_uid = found
                                # Don't bother cancelling the rest — they'll
                                # finish soon and we've already moved on.
                                break
            except Exception:  # noqa: BLE001
                pass

    if not alphanumeric_uid:
        raise HTTPException(404, "Candidate not found in our index. Has it been scanned yet?")

    with db_session() as ses:
        row = ses.scalar(
            select(DebugScoring)
            .where(DebugScoring.candidate_uid == alphanumeric_uid)
            .order_by(DebugScoring.id.desc())
            .limit(1)
        )
        tag = ses.scalar(
            select(AppliedTag).where(AppliedTag.candidate_uid == alphanumeric_uid)
        )

    if not row:
        raise HTTPException(404, "No scoring record for this candidate. Run a scan first.")

    return {
        "candidateUid": alphanumeric_uid,
        "candidateName": row.candidate_name or "",
        "rating": row.final_rating,
        "confidence": row.confidence,
        "summary": row.summary,
        "strengths": row.strengths_json or [],
        "gaps": row.gaps_json or [],
        "positionUid": row.position_uid,
        "positionName": row.position_name,
        "classId": row.class_id,
        "currentTag": tag.tag_name if tag else None,
        "scoredAt": row.timestamp.isoformat() if row.timestamp else None,
    }


class ExtensionScoreNowBody(BaseModel):
    position_uid: str = Field(min_length=1)
    numeric_id: str = ""
    candidate_uid: str = ""


@router.post("/extension/score-now", dependencies=[Depends(_require_extension_token)])
def extension_score_now(body: ExtensionScoreNowBody) -> dict[str, Any]:
    """Score a single candidate immediately and return the same shape as /score.

    Called by the Chrome extension when the recruiter opens a candidate page
    that hasn't been scored yet. Synchronous — takes ~5-30s for a single
    candidate depending on Comeet response time and Claude latency.
    """
    if not body.numeric_id and not body.candidate_uid:
        raise HTTPException(400, "numeric_id or candidate_uid required")
    if body.numeric_id and not body.numeric_id.isdigit():
        raise HTTPException(400, "numeric_id must be all digits")
    try:
        summary = score_one_candidate_now(
            body.position_uid,
            candidate_uid=body.candidate_uid,
            numeric_id=body.numeric_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if summary.error:
        raise HTTPException(422, summary.error)

    # Look up the position name so the extension panel header shows the role.
    position_name = ""
    try:
        with ComeetClient() as pub:
            pos = pub.get_position(body.position_uid)
            if pos:
                position_name = str(pos.get("name") or "")
    except Exception:  # noqa: BLE001
        pass

    # Match the /score response shape exactly so the extension's render path
    # doesn't need a second code branch.
    return {
        "candidateUid": summary.candidate_uid,
        "candidateName": summary.name or "",
        "rating": summary.rating,
        "confidence": summary.confidence,
        "summary": summary.summary,
        "strengths": summary.strengths or [],
        "gaps": summary.gaps or [],
        "positionUid": body.position_uid,
        "positionName": position_name,
        "classId": None,
        "currentTag": summary.tag_applied,
        "scoredAt": datetime.now(timezone.utc).isoformat(),
    }


class ExtensionFeedbackBody(BaseModel):
    candidate_uid: str = Field(min_length=1)
    candidate_name: str = ""
    position_uid: str = Field(min_length=1)
    position_name: str = ""
    ai_rating: int | None = None
    recruiter_rating: int = Field(ge=1, le=5)
    note: str = ""
    recruiter_email: str = ""


@router.post("/extension/feedback", dependencies=[Depends(_require_extension_token)])
def extension_post_feedback(body: ExtensionFeedbackBody) -> dict[str, Any]:
    """Mirror of /api/feedback for the extension. Kept separate so we can add
    extension-specific auth later (currently public — protect via SCREENER_API_TOKEN
    when we add a middleware)."""
    cls = get_position_class(body.position_uid)
    class_id = cls["classId"] if cls else "general"
    class_name = cls["className"] if cls else "General"
    from ..feedback import save_feedback
    fb_id = save_feedback(
        class_id=class_id,
        class_name=class_name,
        position_uid=body.position_uid,
        position_name=body.position_name,
        candidate_uid=body.candidate_uid,
        candidate_name=body.candidate_name,
        ai_rating=body.ai_rating,
        recruiter_rating=body.recruiter_rating,
        note=body.note,
        recruiter_email=body.recruiter_email or "ext:unknown",
    )
    return {"ok": True, "id": fb_id}


# ─── Extension class-management endpoints ───────────────────────────────────
@router.get("/extension/suggest-class", dependencies=[Depends(_require_extension_token)])
def extension_suggest_class(position_uid: str) -> dict[str, Any]:
    """Inline class picker for the extension. Given a Comeet position uid
    (numeric URL form OR alphanumeric), return:
      - The position's name (so the extension can show it),
      - A best-guess class suggestion from existing classes (or null),
      - The full list of classes so the extension can render a dropdown.
    """
    pos_uid = (position_uid or "").strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    position_name = ""
    try:
        with ComeetClient() as pub:
            pos = pub.get_position(pos_uid)
            if pos:
                position_name = str(pos.get("name") or "")
    except Exception:  # noqa: BLE001
        pass

    classes = list_all_classes()
    suggestion_id = _suggest_class_for(position_name, classes)
    suggestion = next((c for c in classes if c["id"] == suggestion_id), None)
    return {
        "positionUid": pos_uid,
        "positionName": position_name,
        "suggestion": suggestion,
        "classes": classes,
    }


class ExtensionAssignClassBody(BaseModel):
    position_uid: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    level: str = ""


@router.post("/extension/assign-class", dependencies=[Depends(_require_extension_token)])
def extension_assign_class(body: ExtensionAssignClassBody) -> dict[str, Any]:
    pos_uid = body.position_uid.strip()
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved
    try:
        return assign_position_class(pos_uid, body.class_id, body.level)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class ExtensionCreateClassBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    levels: list[str] = Field(default_factory=list)


@router.post("/extension/create-class", dependencies=[Depends(_require_extension_token)])
def extension_create_class(body: ExtensionCreateClassBody) -> dict[str, Any]:
    try:
        return create_custom_class(body.name, body.levels)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ─── Onboarding flow (entrance wizard on the home page) ──────────────────────
class AutoClassBody(BaseModel):
    position_uid: str = Field(min_length=1)


def _pick_class_via_claude(position_name: str, position_jd: str, classes: list[dict[str, Any]]) -> str | None:
    """Ask Claude which existing class best fits this position, or 'none' to
    indicate that we should create a new one. Returns the class_id picked,
    or None if Claude doesn't see a good match.

    Kept deliberately small: short prompt, low temperature, single short
    response. Adds ~2-3s to onboarding so we only call it when the cheap
    heuristic comes up empty.
    """
    if not classes:
        return None
    try:
        from anthropic import Anthropic  # local import keeps cold-start fast
        from ..config import settings
        client = Anthropic(api_key=settings.anthropic_api_key)
        class_lines = "\n".join(f"  - {c['id']}: {c['name']}" for c in classes)
        prompt = (
            "You're a recruiting-ops assistant. Given the position below and a "
            "list of existing screening rubric 'classes', pick the single class "
            "whose rubric is the closest fit, OR answer 'none' if none of them "
            "are a meaningfully good match.\n\n"
            f"POSITION NAME: {position_name}\n"
            f"POSITION DESCRIPTION (first 2000 chars):\n{(position_jd or '')[:2000]}\n\n"
            "EXISTING CLASSES:\n"
            f"{class_lines}\n\n"
            "Reply with just the class id (e.g. 'backend') on its own line, "
            "or the literal word 'none'. No explanation."
        )
        msg = client.messages.create(
            model=settings.claude_model,
            max_tokens=40,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in msg.content if getattr(b, "type", "") == "text"
        ).strip().splitlines()[0].strip().strip("'\"`")
        if not text or text.lower() == "none":
            return None
        # Validate against the actual class list — Claude occasionally invents.
        valid_ids = {c["id"] for c in classes}
        if text in valid_ids:
            return text
        # Case-insensitive fallback (Claude sometimes capitalises).
        lower_map = {c["id"].lower(): c["id"] for c in classes}
        return lower_map.get(text.lower())
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-class: Claude pick failed, falling through: %s", exc)
        return None


@router.post("/onboarding/auto-class")
def onboarding_auto_class(body: AutoClassBody) -> dict[str, Any]:
    """Pick (or create) a class for a position with zero recruiter input.

    Decision tree:
      1. If the position already has a class assigned, return that. Fast path.
      2. Try the heuristic name+alias matcher against existing classes.
      3. If the heuristic returns nothing, ask Claude to pick from the list
         (or say 'none').
      4. If Claude also can't pick, create a new class named after the
         position and assign that.

    Always returns the assigned class plus a `source` field describing how
    we got there, so the UI can be transparent about it.
    """
    from ..comeet_client import ComeetClient, position_jd_text as _jd_text
    from ..position_classes import (
        get_position_class as _get_class,
    )

    pos_uid = body.position_uid.strip()
    if not pos_uid:
        raise HTTPException(400, "position_uid required")
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    # 1. Already assigned? Reuse — recruiters expect persistent decisions.
    existing = _get_class(pos_uid)
    if existing and existing.get("classId"):
        classes = list_all_classes()
        cls = next((c for c in classes if c["id"] == existing["classId"]), None)
        return {
            "positionUid": pos_uid,
            "class": cls or {"id": existing["classId"], "name": existing.get("className", "")},
            "source": "existing",
        }

    # Pull position name + JD once; we may need both for Claude.
    position_name = ""
    position_jd = ""
    try:
        with ComeetClient() as pub:
            pos = pub.get_position(pos_uid)
            if pos:
                position_name = str(pos.get("name") or "")
                # Position JD text — fall back gracefully if helper errors.
                try:
                    position_jd = _jd_text(pos) or ""
                except Exception:  # noqa: BLE001
                    position_jd = ""
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-class: couldn't fetch position %s: %s", pos_uid, exc)

    classes = list_all_classes()

    # 2. Cheap heuristic first.
    chosen_id = _suggest_class_for(position_name, classes)
    source = "heuristic"

    # 3. Claude fallback only if the heuristic punted.
    if not chosen_id:
        chosen_id = _pick_class_via_claude(position_name, position_jd, classes)
        if chosen_id:
            source = "claude"

    # 4. Create a new class as last resort.
    if not chosen_id:
        new_name = (position_name or "Custom").strip()[:120]
        # Avoid clobbering an existing class with the same name.
        existing_names = {c["name"].lower() for c in classes}
        if new_name.lower() in existing_names:
            new_name = f"{new_name} (custom)"
        try:
            created = create_custom_class(new_name, [])
            chosen_id = created["id"]
            source = "created"
        except ValueError as exc:
            raise HTTPException(500, f"could not create class: {exc}")

    # Assign and return.
    try:
        assigned = assign_position_class(pos_uid, chosen_id, "")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    refreshed_classes = list_all_classes()
    cls = next((c for c in refreshed_classes if c["id"] == chosen_id), None)
    return {
        "positionUid": pos_uid,
        "positionName": position_name,
        "class": cls or {"id": chosen_id, "name": assigned.get("className", "")},
        "source": source,
    }


class OnboardingBriefBody(BaseModel):
    position_uid: str = Field(min_length=1)
    brief: str = Field(max_length=10000)
    # Industry preference lists. Both optional — when null, the existing
    # values stay as-is (no-op overwrite). When supplied as empty string,
    # the corresponding column is cleared. This lets the brief screen save
    # only what the recruiter typed without forcing them to re-enter
    # industries they already configured.
    industries_up: str | None = Field(default=None, max_length=4000)
    industries_down: str | None = Field(default=None, max_length=4000)


@router.post("/onboarding/brief")
def onboarding_brief(body: OnboardingBriefBody) -> dict[str, Any]:
    """Save the recruiter's free-text brief + optional industry preferences
    for a position. Persisted to position_classes — used by every scan of
    this position via the scoring prompt composer.
    """
    pos_uid = body.position_uid.strip()
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    try:
        set_recruiter_notes(pos_uid, body.brief)
        # When the body explicitly carries either field, persist both
        # together. Treating None as "not provided" keeps backward
        # compatibility with older clients (Chrome extension, scheduled
        # tasks) that don't know about the new fields yet.
        if body.industries_up is not None or body.industries_down is not None:
            set_industry_preferences(
                pos_uid,
                industries_up=body.industries_up or "",
                industries_down=body.industries_down or "",
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return {"positionUid": pos_uid, "saved": True}


@router.get("/onboarding/brief")
def get_onboarding_brief(position_uid: str) -> dict[str, Any]:
    """Read back the saved brief + industry prefs for a position so the
    wizard can pre-fill the form instead of showing empty textareas when
    the recruiter is re-entering."""
    from ..position_classes import get_recruiter_notes

    pos = _resolve_pos(position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    brief = get_recruiter_notes(pos)
    up, down = get_industry_preferences(pos)
    return {
        "positionUid": pos,
        "brief": brief,
        "hasBrief": bool(brief),
        "industriesUp": up,
        "industriesDown": down,
    }


@router.get("/onboarding/weights")
def get_onboarding_weights(position_uid: str) -> dict[str, Any]:
    """Read the 6-dimension weights for this position. Falls back to
    defaults if the recruiter hasn't customised them yet.
    """
    from .. import dimensions as dims

    pos = _resolve_pos(position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    return {
        "positionUid": pos,
        "weights": dims.get_weights(pos),
        "defaults": dims.DEFAULT_WEIGHTS,
        "labels": dims.DIMENSION_LABELS,
        "descriptions": dims.DIMENSION_DESCRIPTIONS,
        "order": list(dims.DIMENSIONS),
    }


class OnboardingWeightsBody(BaseModel):
    position_uid: str = Field(min_length=1)
    # 5 slider dimensions: profession_domain, company_domain, company_tier,
    # career_progression, university_tier. Each integer 0-100; the five
    # must sum to exactly 100. location_match is server-side (hard gate),
    # not in this dict. Legacy axes `domain_match` and `achievements` are
    # deprecated and ignored if present.
    weights: dict[str, int] = Field(min_length=5, max_length=5)


@router.post("/onboarding/weights")
def post_onboarding_weights(body: OnboardingWeightsBody) -> dict[str, Any]:
    """Persist the recruiter's per-position weight dict. Must include the
    5 slider dimensions (profession_domain, company_domain, company_tier,
    career_progression, university_tier) and sum to exactly 100.
    """
    from .. import dimensions as dims

    pos = _resolve_pos(body.position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    try:
        saved = dims.set_weights(pos, body.weights)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"positionUid": pos, "weights": saved, "saved": True}


# ─── Calibration (thumbs UI) ─────────────────────────────────────────────────
def _resolve_pos(uid: str) -> str:
    """Normalize a position uid: numeric Comeet URL form → alphanumeric."""
    uid = (uid or "").strip()
    if uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(uid)
        if resolved:
            return resolved
    return uid


# Fields that leak the AI's assessment to the recruiter. Stripped from
# the queue payload when `blind=true` to prevent anchoring bias during
# rating. The recruiter retrieves them post-submit via /calibration/reveal.
_AI_REVEAL_FIELDS = (
    "rating",
    "confidence",
    "summary",
    "strengths",
    "gaps",
    "bucket",
    "dimensions",
    "locationGateFailed",
    "domainCapApplied",
)


@router.get("/calibration/queue")
def calibration_queue(
    recruiter: str,
    position_uid: str,
    n: int = 5,
    blind: bool = False,
) -> dict[str, Any]:
    """Next batch of candidates the recruiter should review.

    Filtered to candidates currently bucketed as 👍 by the recruiter's
    threshold, minus anyone they've already verdicted this session.

    `blind=true` strips every AI-derived field from each candidate
    payload (rating, dims, confidence, bucket, gate flags, summary,
    strengths, gaps). Used by the new calibration framework where the
    recruiter rates 1-10 without seeing the AI's score. After they
    submit, the frontend hits /calibration/reveal to fetch the AI side
    for comparison + the broken-axes modal.
    """
    from .. import calibration as cal
    recruiter = (recruiter or "").strip()
    if not recruiter:
        raise HTTPException(400, "recruiter required")
    pos = _resolve_pos(position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    q = cal.get_calibration_queue(recruiter, pos, n=max(1, min(n, 20)))
    candidates = q["candidates"]
    if blind:
        # Strip in-place — the recruiter must not see any AI-derived
        # field until they submit their own rating.
        candidates = [
            {k: v for k, v in c.items() if k not in _AI_REVEAL_FIELDS}
            for c in candidates
        ]
    return {
        "positionUid": pos,
        "blind": bool(blind),
        "candidates": candidates,
        "isCalibrated": q["isCalibrated"],
        "totalScored": q.get("totalScored", 0),
        "totalVerdicted": q.get("totalVerdicted", 0),
        "remainingInPool": q.get("remainingInPool", 0),
        "scoredThisCall": q.get("scoredThisCall", 0),
        "state": cal.get_session_state(recruiter, pos),
    }


@router.get("/calibration/reveal")
def calibration_reveal(
    position_uid: str,
    candidate_uid: str,
) -> dict[str, Any]:
    """Return the AI's assessment for one candidate.

    Called by the frontend AFTER the recruiter submits their 1-10 rating
    in blind mode, so the calibration card can flip from "blind" to
    "reveal" and the broken-axes modal can show per-axis dim sub-scores.

    Reads the most recent DebugScoring row for this (candidate, position)
    pair. Returns 404 when no scoring row exists (shouldn't happen if
    the candidate came from the calibration queue).
    """
    from ..db import db_session
    from ..models import DebugScoring
    from sqlalchemy import select, desc

    pos = _resolve_pos(position_uid)
    cand = (candidate_uid or "").strip()
    if not (pos and cand):
        raise HTTPException(400, "position_uid and candidate_uid required")

    with db_session() as ses:
        row = ses.scalar(
            select(DebugScoring)
            .where(
                (DebugScoring.candidate_uid == cand)
                & (DebugScoring.position_uid == pos)
            )
            .order_by(desc(DebugScoring.timestamp))
            .limit(1)
        )
        if not row:
            raise HTTPException(404, "no scoring row for this candidate")
        dims = {
            "profession_domain": row.dim_profession_domain,
            "company_domain": row.dim_company_domain,
            "company_tier": row.dim_company_tier,
            "career_progression": row.dim_career_progression,
            "location_match": row.dim_location_match,
            "university_tier": row.dim_university_tier,
            "domain_match": row.dim_domain_match,  # legacy
            "achievements": row.dim_achievements,  # deprecated
        }
        return {
            "candidateUid": cand,
            "positionUid": pos,
            "rating": row.final_rating,
            "confidence": row.confidence,
            "summary": row.summary or "",
            "strengths": row.strengths_json or [],
            "gaps": row.gaps_json or [],
            "dimensions": dims,
            "locationGateFailed": (
                row.dim_location_match is not None
                and row.dim_location_match < 4
            ),
            "domainCapApplied": (
                # mirror app.calibration._domain_cap_applied logic
                _domain_cap_applied_signal(row)
            ),
        }


def _domain_cap_applied_signal(row) -> bool:
    """Did the soft cap fire on this candidate? Mirrors calibration._domain_cap_applied."""
    prof = row.dim_profession_domain
    comp = row.dim_company_domain
    present = [x for x in (prof, comp) if x is not None]
    final = row.final_rating or 0
    if present:
        avg = sum(present) / len(present)
        return avg < 5 and final == 5
    legacy = row.dim_domain_match
    return legacy is not None and legacy < 5 and final == 5


# Canonical set of axis ids the broken-axes follow-up can flag. Anything
# outside this set is dropped on the floor at the API layer — we never
# trust client-side strings into the rubric pipeline. Keep this in sync
# with DIM_ORDER_FE in index.html and the keys in dimensions.py weights.
_VALID_BROKEN_AXES = frozenset({
    "profession_domain",
    "company_domain",
    "company_tier",
    "career_progression",
    "university_tier",
})


class CalibrationVerdictBody(BaseModel):
    recruiter: str = Field(min_length=1, max_length=200)
    position_uid: str = Field(min_length=1)
    candidate_uid: str = Field(min_length=1)
    # `verdict` is now optional — if `recruiter_rating` is supplied, the
    # server derives the verdict bucket from it (1-3=down, 4-6=question,
    # 7-10=up). Kept here for legacy thumb-clients.
    verdict: str = Field(default="question", pattern=r"^(up|down|question)$")
    ai_rating: int | None = None
    ai_confidence: float | None = None
    feedback_text: str | None = Field(default=None, max_length=4000)
    # Precise 1-10 ground-truth from the recruiter. Optional for backward
    # compatibility but the calibration UI now always sends it.
    recruiter_rating: int | None = Field(default=None, ge=1, le=10)
    # Per-axis disagreement tag from the follow-up modal. Sent only when
    # |recruiter_rating - ai_rating| >= 1.5 and the recruiter selected
    # one or more axes. Server-side validated against the canonical axis
    # set; unknown ids are silently dropped.
    broken_axes: list[str] | None = Field(default=None, max_length=5)


@router.post("/calibration/verdict")
def calibration_verdict(body: CalibrationVerdictBody) -> dict[str, Any]:
    """Record a 1-10 rating (preferred) or 👍/👎/❓ verdict + update threshold."""
    from .. import calibration as cal
    pos = _resolve_pos(body.position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    # Sanitise broken_axes: keep only canonical ids, dedup, normalise empty
    # list → None so the DB stores NULL instead of [] (cleaner for queries).
    clean_axes: list[str] | None = None
    if body.broken_axes:
        seen: set[str] = set()
        out: list[str] = []
        for raw in body.broken_axes:
            v = (raw or "").strip().lower()
            if v in _VALID_BROKEN_AXES and v not in seen:
                seen.add(v)
                out.append(v)
        clean_axes = out or None
    result = cal.record_verdict(
        recruiter_name=body.recruiter.strip(),
        position_uid=pos,
        candidate_uid=body.candidate_uid.strip(),
        verdict=body.verdict,  # type: ignore[arg-type]
        ai_rating=body.ai_rating,
        ai_confidence=body.ai_confidence,
        feedback_text=body.feedback_text,
        recruiter_rating=body.recruiter_rating,
        broken_axes=clean_axes,
    )
    return result


class BatchCompleteBody(BaseModel):
    recruiter: str = Field(min_length=1, max_length=200)
    position_uid: str = Field(min_length=1)
    round_num: int | None = None


@router.post("/calibration/batch-complete")
def calibration_batch_complete(body: BatchCompleteBody) -> dict[str, Any]:
    """Fires after every batch of 5 verdicts.

    Triggers position-rubric refresh (when eligible) + computes a per-batch
    benchmark snapshot the frontend uses for the mini-chart. The snapshot
    is computed from existing data (CalibrationVerdict rows for this
    position) so no extra writes — the chart can be re-derived from the
    raw verdicts at any time.

    Returns:
      {
        "rubric": {"refreshed": bool, "source": "position"|"class"|None,
                   "feedback_count": int, "rubric_length": int},
        "batches": [
          {"round": 1, "n": 5, "avg_delta": 1.4, "agreement_pct": 0.6,
           "per_axis": {"company_tier": 3, ...}},
          ...
        ],
      }
    """
    from .. import calibration as cal
    from ..position_classes import get_position_class
    from ..rubrics import refresh_position_rubric, POSITION_RUBRIC_MIN_SAMPLES
    from ..db import db_session
    from ..models import CalibrationVerdict
    from ..benchmark_stats import compute_benchmark_stats, is_dirty_feedback
    from sqlalchemy import select

    pos = _resolve_pos(body.position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    recruiter = (body.recruiter or "").strip()
    if not recruiter:
        raise HTTPException(400, "recruiter required")

    # ── 1. Refresh position rubric if eligible ──────────────────────────
    cls = get_position_class(pos)
    rubric_result: dict[str, Any] = {"refreshed": False, "source": None}
    if cls and cls.get("classId"):
        rr = refresh_position_rubric(pos, cls["classId"], cls.get("className") or cls["classId"])
        if rr.get("ok"):
            rubric_result = {
                "refreshed": True,
                "source": "position",
                "feedback_count": rr.get("feedback_count", 0),
                "rubric_length": rr.get("rubric_length", 0),
            }
        else:
            # Position rubric not eligible (< MIN_SAMPLES). The class
            # rubric is still in play as cold-start fallback.
            rubric_result = {
                "refreshed": False,
                "source": "class",
                "reason": rr.get("error", ""),
                "min_samples_needed": POSITION_RUBRIC_MIN_SAMPLES,
                "feedback_count": rr.get("feedback_count", 0),
            }

    # ── 2. Compute per-batch benchmark snapshots ────────────────────────
    # Pull every verdict for this (recruiter, position), bucket by
    # round_num. compute_benchmark_stats handles: dirty-verdict filtering,
    # bias/MAE/RMSE, std-dev + discrimination ratio, false_negatives /
    # false_positives, and per-axis tallies. We layer the running-threshold
    # agreement on top because that's the only stat needing cross-round
    # state (each round's threshold depends on earlier rounds' 👍s).
    with db_session() as ses:
        rows = ses.scalars(
            select(CalibrationVerdict).where(
                (CalibrationVerdict.recruiter_name == recruiter)
                & (CalibrationVerdict.position_uid == pos)
                & (CalibrationVerdict.recruiter_rating.is_not(None))
            ).order_by(CalibrationVerdict.created_at)
        ).all()

    # Running threshold = lowest 👍 the recruiter has clicked so far,
    # OR the legacy 7+ bucket boundary when nothing has been 👍'd yet.
    # We back-compute "same side of threshold" per-batch using the
    # running threshold AT THAT POINT IN TIME — so the agreement curve
    # reflects how alignment improved as the rubric learned.
    by_round: dict[int, list] = {}
    for r in rows:
        rn = r.round_num or 1
        by_round.setdefault(rn, []).append(r)

    running_threshold = None  # lowest 👍 rating to date
    batches = []
    # Aggregate totals so the UI can render a header summary without
    # walking the per-round payload itself.
    total_dirty = 0
    total_fn = 0
    total_fp = 0
    for rn in sorted(by_round.keys()):
        bucket = by_round[rn]
        n_raw = len(bucket)
        if n_raw == 0:
            continue
        # All numeric stats come from compute_benchmark_stats. We pass
        # running_threshold so agreement_pct in the response reflects the
        # threshold KNOWN AT THIS POINT (None until the first 👍).
        stats = compute_benchmark_stats(bucket, threshold=running_threshold)
        # Advance the running threshold AFTER computing this round so the
        # next round uses the bar this round just established.
        for v in bucket:
            if (
                v.recruiter_rating is not None
                and v.verdict == "up"
                and (running_threshold is None or v.recruiter_rating < running_threshold)
            ):
                running_threshold = v.recruiter_rating

        total_dirty += stats["dirty_count"]
        total_fn += stats["false_negatives"]
        total_fp += stats["false_positives"]
        batches.append({
            "round": rn,
            "n": n_raw,                       # raw verdict count (incl. dirty)
            "n_clean": stats["count"],         # numeric stats denominator
            "dirty_count": stats["dirty_count"],
            "avg_delta": stats["bias"],        # legacy alias
            "bias": stats["bias"],
            "mae": stats["mae"],
            "agreement_pct": stats["agreement_pct"],
            "agreement_n": stats["agreement_n"],
            "std_ai": stats["std_ai"],
            "std_recruiter": stats["std_recruiter"],
            "discrimination_ratio": stats["discrimination_ratio"],
            "false_negatives": stats["false_negatives"],
            "false_positives": stats["false_positives"],
            "per_axis": stats["per_axis"],
            "running_threshold": running_threshold,
        })

    return {
        "positionUid": pos,
        "rubric": rubric_result,
        "batches": batches,
        "thresholdAtFinish": running_threshold,
        # Header totals for the round-summary UI banner. dirty_count > 0
        # means stale-cache or API errors polluted this session and the
        # numeric metrics already exclude those verdicts — UI surfaces a
        # "n verdicts excluded" hint.
        "dirtyCount": total_dirty,
        "falseNegatives": total_fn,
        "falsePositives": total_fp,
    }


class FinalizeBody(BaseModel):
    recruiter: str = Field(min_length=1, max_length=200)
    position_uid: str = Field(min_length=1)
    chosen_threshold: int | None = Field(default=None, ge=1, le=10)


@router.post("/calibration/finalize")
def calibration_finalize(body: FinalizeBody) -> dict[str, Any]:
    """Fires after all 25 ratings done. Computes the meta-benchmark.

    If `chosen_threshold` is supplied, also saves it as the
    RecruiterThreshold.thumbs_up_min_rating for this (recruiter, position),
    which is what the auto-tagging cron uses to gate Comeet tags.

    Returns the per-batch table (same as batch-complete) plus aggregate
    stats: total ratings, final bias, agreement under the final threshold,
    per-axis disagreement totals.
    """
    from ..db import db_session
    from ..models import CalibrationVerdict, RecruiterThreshold
    from ..benchmark_stats import compute_benchmark_stats
    from sqlalchemy import select, desc
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    pos = _resolve_pos(body.position_uid)
    recruiter = (body.recruiter or "").strip()
    if not (pos and recruiter):
        raise HTTPException(400, "recruiter and position_uid required")

    # Save the chosen threshold first (if provided), so the agreement
    # metric below uses it.
    if body.chosen_threshold is not None:
        with db_session() as ses:
            stmt = pg_insert(RecruiterThreshold).values(
                recruiter_name=recruiter,
                position_uid=pos,
                thumbs_up_min_rating=body.chosen_threshold,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["recruiter_name", "position_uid"],
                set_={"thumbs_up_min_rating": stmt.excluded.thumbs_up_min_rating},
            )
            ses.execute(stmt)

    # Pull all verdicts, compute aggregate stats + per-batch curve under
    # the FINAL threshold.
    final_threshold = body.chosen_threshold
    with db_session() as ses:
        rows = ses.scalars(
            select(CalibrationVerdict).where(
                (CalibrationVerdict.recruiter_name == recruiter)
                & (CalibrationVerdict.position_uid == pos)
                & (CalibrationVerdict.recruiter_rating.is_not(None))
            ).order_by(CalibrationVerdict.created_at)
        ).all()
        if final_threshold is None:
            # Fall back to whatever's on file.
            t_row = ses.scalar(
                select(RecruiterThreshold).where(
                    (RecruiterThreshold.recruiter_name == recruiter)
                    & (RecruiterThreshold.position_uid == pos)
                )
            )
            final_threshold = t_row.thumbs_up_min_rating if t_row else None

    # Per-batch under the FINAL threshold ("honest replay" — what the
    # agreement curve would look like if we'd had this threshold from the
    # start). Aggregate totals come from a single all-rows call so the
    # header stays consistent with the per-batch numbers.
    overall_stats = compute_benchmark_stats(rows, threshold=final_threshold)

    by_round: dict[int, list] = {}
    for r in rows:
        by_round.setdefault(r.round_num or 1, []).append(r)
    batches = []
    for rn in sorted(by_round.keys()):
        bucket = by_round[rn]
        bs = compute_benchmark_stats(bucket, threshold=final_threshold)
        batches.append({
            "round": rn,
            "n": len(bucket),
            "n_clean": bs["count"],
            "dirty_count": bs["dirty_count"],
            "avg_delta": bs["bias"],
            "bias": bs["bias"],
            "mae": bs["mae"],
            "agreement_pct": bs["agreement_pct"],
            "std_ai": bs["std_ai"],
            "std_recruiter": bs["std_recruiter"],
            "discrimination_ratio": bs["discrimination_ratio"],
            "false_negatives": bs["false_negatives"],
            "false_positives": bs["false_positives"],
            "per_axis": bs["per_axis"],
        })
    return {
        "positionUid": pos,
        "totalRatings": len(rows),
        "totalClean": overall_stats["count"],
        "dirtyCount": overall_stats["dirty_count"],
        "dirtyUids": overall_stats["dirty_uids"],
        "finalThreshold": final_threshold,
        "bias": overall_stats["bias"],
        "mae": overall_stats["mae"],
        "rmse": overall_stats["rmse"],
        "stdAi": overall_stats["std_ai"],
        "stdRecruiter": overall_stats["std_recruiter"],
        "discriminationRatio": overall_stats["discrimination_ratio"],
        "falseNegatives": overall_stats["false_negatives"],
        "falsePositives": overall_stats["false_positives"],
        "agreementOverall": overall_stats["agreement_pct"],
        "perAxisTotals": overall_stats["per_axis"],
        "batches": batches,
    }


@router.get("/calibration/state")
def calibration_state(recruiter: str, position_uid: str) -> dict[str, Any]:
    """Snapshot of where this recruiter is in calibration for this position."""
    from .. import calibration as cal
    pos = _resolve_pos(position_uid)
    return cal.get_session_state(recruiter.strip(), pos)


class CalibrationPrewarmBody(BaseModel):
    position_uid: str = Field(min_length=1)
    n: int = 15


@router.post("/calibration/prewarm")
def calibration_prewarm(body: CalibrationPrewarmBody) -> dict[str, Any]:
    """Fire-and-forget: kick off background scoring of the next N unscored
    candidates for this position. Called from the frontend the moment a
    recruiter picks a position — by the time they're done picking class +
    typing the brief, the calibration queue is likely pre-warmed and the
    first batch loads in <1s instead of 1-2 min.

    Returns immediately (HTTP 200) regardless of whether the prewarm thread
    completes successfully; this is a hint, not a contract.
    """
    from .. import prewarm
    pos = _resolve_pos(body.position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    n = max(1, min(int(body.n or 15), 50))
    return prewarm.prewarm_position(pos, n=n)


# ─── Admin global controls ───────────────────────────────────────────────
@router.get("/admin/settings")
def admin_get_settings(recruiter: str = "") -> dict[str, Any]:
    """Return the current admin levers (👍 floor + global brief), plus a
    flag telling the UI whether the caller is allowed to edit them.

    Anyone can READ — the values affect everyone's scoring anyway, so
    transparency is fine. Only ADMIN_RECRUITERS can WRITE.
    """
    from .. import admin_settings as admin
    s = admin.get_settings()
    s["isAdmin"] = admin.is_admin((recruiter or "").strip())
    return s


class AdminSettingsBody(BaseModel):
    recruiter: str = Field(min_length=1, max_length=200)
    # 1-10 sets the floor (internal scale), 0 clears it, None leaves it alone.
    thumbs_up_floor: int | None = Field(default=None, ge=0, le=10)
    # Empty string clears, None leaves alone.
    brief: str | None = Field(default=None, max_length=10000)


@router.post("/admin/settings")
def admin_set_settings(body: AdminSettingsBody) -> dict[str, Any]:
    """Write admin levers. Gated on recruiter being in ADMIN_RECRUITERS."""
    from .. import admin_settings as admin
    if not admin.is_admin(body.recruiter.strip()):
        raise HTTPException(403, "Admin permission required")
    try:
        s = admin.set_settings(
            thumbs_up_floor=body.thumbs_up_floor,
            brief=body.brief,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    s["isAdmin"] = True
    return s


@router.get("/candidate/enrichment")
def candidate_enrichment(candidate_uid: str) -> dict[str, Any]:
    """Structured profile (career timeline, LinkedIn, education) for one candidate.

    Cache-first: returns instantly if we've extracted this candidate before;
    otherwise fetches their CV from Comeet, runs a focused Claude extraction
    pass (~2-3s, ~$0.01), caches the result. Errors cache too — a candidate
    with no CV on file won't get re-tried on every refresh.
    """
    from .. import enrichment as enr
    cuid = (candidate_uid or "").strip()
    if not cuid:
        raise HTTPException(400, "candidate_uid required")
    return enr.get_or_extract(cuid)


# ─── Scan flow ───────────────────────────────────────────────────────────────
class ScanNowBody(BaseModel):
    position_uid: str = Field(min_length=1)


@router.post("/scan/now")
def scan_now(body: ScanNowBody) -> dict[str, Any]:
    """Run the autoscan pipeline immediately on one position. Synchronous —
    can take a few minutes for positions with many candidates."""
    from ..automation import scan_one_position_now

    try:
        result = scan_one_position_now(body.position_uid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "positionUid": result.position_uid,
        "positionName": result.position_name,
        "classId": result.class_id,
        "scored": result.scored,
        "skipped": result.skipped,
        "tagsApplied": result.tags_applied,
        "errors": result.errors,
        "note": result.note,
    }
