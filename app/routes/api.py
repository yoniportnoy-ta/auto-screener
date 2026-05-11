"""JSON API for the recruiter UI.

Replaces the `google.script.run.<fn>` calls in the Apps Script Index.html.
Same conceptual endpoints, returning the same shape so the JS port stays minimal.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
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
)
from ..scan import (
    begin_scan_batch,
    finish_scan_batch,
    score_candidate_in_session,
    score_one_candidate_now,
)

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


@router.get("/auto-screen/positions")
def list_auto_screen() -> list[str]:
    """Position UIDs the cron currently scans (debug helper)."""
    return list_auto_screen_positions()


# ─── Chrome extension endpoints ──────────────────────────────────────────────
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
                # Fallback (rare): brute-force across all open positions.
                if not alphanumeric_uid:
                    positions = pub.list_open_positions()
                    for p in positions:
                        if pos_uid and p.get("uid") == pos_uid:
                            continue  # already scanned above
                        candidates = pub.list_candidates_for_position(p["uid"])
                        for c in candidates:
                            c_url = c.get("URL", "")
                            if n_id in c_url:
                                alphanumeric_uid = str(c.get("uid", ""))
                                break
                        if alphanumeric_uid:
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


# ─── Scan flow ───────────────────────────────────────────────────────────────
class BeginScanBody(BaseModel):
    position_uid: str = Field(min_length=1)
    seen_uids: list[str] = Field(default_factory=list)
    class_id: str = ""
    class_level: str = ""


@router.post("/scan/begin")
def scan_begin(body: BeginScanBody) -> dict[str, Any]:
    try:
        result = begin_scan_batch(
            body.position_uid,
            seen_uids=body.seen_uids,
            class_id_override=body.class_id or None,
            class_level_override=body.class_level or None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _result_to_json(result)


class ScoreOneBody(BaseModel):
    session_id: str = Field(min_length=1)
    candidate_uid: str = Field(min_length=1)


@router.post("/scan/score")
def scan_score_one(body: ScoreOneBody) -> dict[str, Any]:
    try:
        summary = score_candidate_in_session(body.session_id, body.candidate_uid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"summary": _summary_to_json(summary)}


class FinishScanBody(BaseModel):
    session_id: str = Field(min_length=1)
    processed_uids: list[str] = Field(default_factory=list)


@router.post("/scan/finish")
def scan_finish(body: FinishScanBody) -> dict[str, Any]:
    return finish_scan_batch(body.session_id, body.processed_uids)


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


# ─── Feedback ────────────────────────────────────────────────────────────────
class FeedbackBody(BaseModel):
    candidate_uid: str = Field(min_length=1)
    candidate_name: str = ""
    position_uid: str = Field(min_length=1)
    position_name: str = ""
    ai_rating: int | None = None
    recruiter_rating: int = Field(ge=1, le=5)
    note: str = ""
    recruiter_email: str = ""


@router.post("/feedback")
def post_feedback(body: FeedbackBody) -> dict[str, Any]:
    cls = get_position_class(body.position_uid)
    class_id = cls["classId"] if cls else "general"
    class_name = cls["className"] if cls else "General"
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
        recruiter_email=body.recruiter_email,
    )
    return {"ok": True, "id": fb_id}


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _result_to_json(result) -> dict[str, Any]:
    out = asdict(result)
    return {_camel(k): v for k, v in out.items()}


def _summary_to_json(summary) -> dict[str, Any]:
    out = asdict(summary)
    # The Apps Script UI expects camelCase keys (linkedinUrl, ratingLabel, …).
    return {_camel(k): v for k, v in out.items()}


def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])
