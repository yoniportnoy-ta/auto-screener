"""/api/screening/*: CV screening for the Riverside Screening Claude plugin.

Only the sourcing gateway calls these. It authenticates each recruiter with their
personal access code and passes their name here; this service trusts it because the
call carries SCREENER_GATEWAY_TOKEN. Comeet writes run as the recruiter-app bot
(see app/screening_actions.py) and every decision comment names the recruiter.
"""
from __future__ import annotations

import hmac
import logging
import threading
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .. import screening_actions as sa
from ..comeet_client import (
    ComeetClient,
    candidate_active_for_screening,
    candidate_full_name,
    candidate_in_allowed_step,
)
from ..config import settings

log = logging.getLogger(__name__)
router = APIRouter()

QUEUE_TTL_SECONDS = 300


def _require_gateway(x_gateway_token: str | None = Header(default=None, alias="X-Gateway-Token")) -> None:
    expected = (settings.screener_gateway_token or "").strip()
    if not expected:
        raise HTTPException(503, "screening isn't configured on the server (set SCREENER_GATEWAY_TOKEN)")
    if not x_gateway_token or not hmac.compare_digest(x_gateway_token.strip(), expected):
        raise HTTPException(401, "invalid or missing X-Gateway-Token")


# ─── Position candidates (cached briefly: the Comeet listing is slow) ────────
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def _position_candidates(position_uid: str, *, fresh: bool = False) -> list[dict[str, Any]]:
    with _cache_lock:
        hit = _cache.get(position_uid)
    if hit and not fresh and time.time() - hit[0] < QUEUE_TTL_SECONDS:
        return hit[1]
    with ComeetClient() as client:
        cands = [c for c in client.list_candidates_for_position(position_uid) if isinstance(c, dict) and c.get("uid")]
    with _cache_lock:
        _cache[position_uid] = (time.time(), cands)
    return cands


def _forget(position_uid: str) -> None:
    with _cache_lock:
        _cache.pop(position_uid, None)


def _source_name(c: dict[str, Any]) -> str:
    src = c.get("source")
    if isinstance(src, dict):
        return str(src.get("name") or src.get("type") or "")
    return str(src or "")


def _summary(c: dict[str, Any]) -> dict[str, Any]:
    url = str(c.get("URL") or "")
    resume = c.get("resume") or {}
    return {
        "uid": str(c["uid"]),
        "name": candidate_full_name(c),
        "comeet_url": url,
        "numeric_id": sa.numeric_id_from_url(url),
        "linkedin_url": c.get("linkedin_url") or "",
        "source": _source_name(c),
        "applied": c.get("time_created") or "",
        "has_cv": bool(isinstance(resume, dict) and resume.get("url")),
        "current_step": ", ".join(str(s.get("name") or "") for s in (c.get("current_steps") or [])),
    }


def _in_queue(c: dict[str, Any]) -> bool:
    return candidate_in_allowed_step(c) and candidate_active_for_screening(c)


def _find(position_uid: str, candidate_uid: str) -> dict[str, Any]:
    for fresh in (False, True):
        for c in _position_candidates(position_uid, fresh=fresh):
            if str(c.get("uid")) == candidate_uid:
                return c
    raise HTTPException(404, f"candidate {candidate_uid} isn't on position {position_uid}")


# ─── Routes ──────────────────────────────────────────────────────────────────
@router.get("/queue", dependencies=[Depends(_require_gateway)])
def queue(position_uid: str, limit: int = 50) -> dict[str, Any]:
    """Candidates waiting at CV screen on a position, oldest application first."""
    pos = position_uid.strip()
    if not pos:
        raise HTTPException(400, "position_uid is required")
    waiting = sorted((c for c in _position_candidates(pos) if _in_queue(c)), key=lambda c: str(c.get("time_created") or ""))
    limit = max(1, min(limit, 200))
    return {"position_uid": pos, "waiting": len(waiting), "candidates": [_summary(c) for c in waiting[:limit]]}


@router.get("/candidate", dependencies=[Depends(_require_gateway)])
def candidate(position_uid: str, candidate_uid: str) -> dict[str, Any]:
    """One candidate with their CV as text."""
    c = _find(position_uid.strip(), candidate_uid.strip())
    resume = c.get("resume") or {}
    text, status = sa.cv_text(resume.get("url") if isinstance(resume, dict) else None)
    return {**_summary(c), "status": c.get("status") or "", "cv_status": status, "cv_text": text}


@router.get("/reasons", dependencies=[Depends(_require_gateway)])
def reasons() -> dict[str, Any]:
    try:
        return {"reasons": sa.reject_reasons()}
    except sa.ComeetAppError as exc:
        raise HTTPException(502, f"Comeet error: {exc}") from exc


class Recruiter(BaseModel):
    name: str = Field(min_length=1)
    email: str = ""


class DecisionBody(BaseModel):
    position_uid: str = Field(min_length=1)
    candidate_uid: str = Field(min_length=1)
    decision: Literal["pass", "reject"]
    reason_id: int | None = None
    comment: str = ""
    send_rejection_email: bool = False   # rejects only; the recruiter chose it on this candidate
    recruiter: Recruiter


@router.post("/decision", dependencies=[Depends(_require_gateway)])
def decision(body: DecisionBody) -> dict[str, Any]:
    """Pass or reject a candidate at CV screen, then add an internal comment.

    Refused unless the candidate is on this position and their current step is the CV
    screen, so a decision is never written twice or on the wrong step.
    """
    pos, uid = body.position_uid.strip(), body.candidate_uid.strip()
    c = _find(pos, uid)
    try:
        num = sa.numeric_id_from_url(str(c.get("URL") or "")) or sa.resolve_numeric_id(uid)
        step = sa.current_cv_screen_step(num)
        reason = None
        if body.decision == "reject":
            reason = next((r["reason"] for r in sa.reject_reasons() if r["id"] == body.reason_id), None)
        sa.submit_decision(num, int(step["id"]), body.decision, body.reason_id)
    except sa.ScreeningError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except sa.ComeetAppError as exc:
        raise HTTPException(502, f"Comeet error: {exc}") from exc
    finally:
        _forget(pos)

    log.info("screening: %s %s on %s by %s", body.decision, uid, pos, body.recruiter.email or body.recruiter.name)
    comment_posted, comment_error = False, ""
    try:
        sa.post_comment(num, sa.comment_text(body.recruiter.name, body.decision, reason, body.comment))
        comment_posted = True
    except Exception as exc:  # noqa: BLE001  the decision is already saved; report, don't fail
        comment_error = str(exc)[:200]
        log.warning("screening: comment on %s failed: %s", uid, comment_error)
    email: dict[str, Any] | None = None
    email_error = ""
    if body.decision == "reject" and body.send_rejection_email:
        try:
            email = sa.send_rejection_email(num)
            log.info("screening: rejection email for %s sent by %s", uid, body.recruiter.email or body.recruiter.name)
        except Exception as exc:  # noqa: BLE001  the reject is saved; report, don't fail
            email_error = str(exc)[:200]
            log.warning("screening: rejection email for %s failed: %s", uid, email_error)
    return {
        "ok": True, "decision": body.decision, "reason": reason, "candidate_uid": uid, "numeric_id": num,
        "comeet_url": str(c.get("URL") or ""), "comment_posted": comment_posted, "comment_error": comment_error,
        "rejection_email": email, "rejection_email_error": email_error,
    }


class RejectionEmailBody(BaseModel):
    position_uid: str = Field(min_length=1)
    candidate_uid: str = Field(min_length=1)
    recruiter: Recruiter


@router.post("/rejection-email", dependencies=[Depends(_require_gateway)])
def rejection_email(body: RejectionEmailBody) -> dict[str, Any]:
    """Send the waiting rejection email for a candidate rejected earlier."""
    pos, uid = body.position_uid.strip(), body.candidate_uid.strip()
    c = _find(pos, uid)
    try:
        num = sa.numeric_id_from_url(str(c.get("URL") or "")) or sa.resolve_numeric_id(uid)
        email = sa.send_rejection_email(num)
    except sa.ScreeningError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except sa.ComeetAppError as exc:
        raise HTTPException(502, f"Comeet error: {exc}") from exc
    finally:
        _forget(pos)
    log.info("screening: rejection email for %s sent by %s", uid, body.recruiter.email or body.recruiter.name)
    return {"ok": True, "candidate_uid": uid, "comeet_url": str(c.get("URL") or ""), "rejection_email": email}
