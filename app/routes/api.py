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
    get_position_class,
    list_all_classes,
    list_auto_screen_positions,
    set_auto_screen_enabled,
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


@router.post("/onboarding/brief")
def onboarding_brief(body: OnboardingBriefBody) -> dict[str, Any]:
    """Save the recruiter's free-text brief for a position. Persisted to
    the position_classes.recruiter_notes column so future scans include it
    in the scoring prompt automatically.
    """
    from ..position_classes import set_recruiter_notes

    pos_uid = body.position_uid.strip()
    if pos_uid.isdigit():
        from ..scan import _resolve_numeric_position_uid
        resolved = _resolve_numeric_position_uid(pos_uid)
        if resolved:
            pos_uid = resolved

    try:
        set_recruiter_notes(pos_uid, body.brief)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return {"positionUid": pos_uid, "saved": True}


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


@router.get("/calibration/queue")
def calibration_queue(
    recruiter: str,
    position_uid: str,
    n: int = 5,
) -> dict[str, Any]:
    """Next batch of candidates the recruiter should review.

    Filtered to candidates currently bucketed as 👍 by the recruiter's
    threshold, minus anyone they've already verdicted this session.
    """
    from .. import calibration as cal
    recruiter = (recruiter or "").strip()
    if not recruiter:
        raise HTTPException(400, "recruiter required")
    pos = _resolve_pos(position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    items = cal.get_calibration_queue(recruiter, pos, n=max(1, min(n, 20)))
    return {
        "positionUid": pos,
        "candidates": items,
        "state": cal.get_session_state(recruiter, pos),
    }


class CalibrationVerdictBody(BaseModel):
    recruiter: str = Field(min_length=1, max_length=200)
    position_uid: str = Field(min_length=1)
    candidate_uid: str = Field(min_length=1)
    verdict: str = Field(pattern=r"^(up|down|question)$")
    ai_rating: int | None = None
    ai_confidence: float | None = None


@router.post("/calibration/verdict")
def calibration_verdict(body: CalibrationVerdictBody) -> dict[str, Any]:
    """Record a 👍 / 👎 / ❓ verdict and update the recruiter's threshold."""
    from .. import calibration as cal
    pos = _resolve_pos(body.position_uid)
    if not pos:
        raise HTTPException(400, "position_uid required")
    result = cal.record_verdict(
        recruiter_name=body.recruiter.strip(),
        position_uid=pos,
        candidate_uid=body.candidate_uid.strip(),
        verdict=body.verdict,  # type: ignore[arg-type]
        ai_rating=body.ai_rating,
        ai_confidence=body.ai_confidence,
    )
    return result


@router.get("/calibration/state")
def calibration_state(recruiter: str, position_uid: str) -> dict[str, Any]:
    """Snapshot of where this recruiter is in calibration for this position."""
    from .. import calibration as cal
    pos = _resolve_pos(position_uid)
    return cal.get_session_state(recruiter.strip(), pos)


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
