"""JSON API for the recruiter UI.

Replaces the `google.script.run.<fn>` calls in the Apps Script Index.html.
Same conceptual endpoints, returning the same shape so the JS port stays minimal.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from ..comeet_client import (
    ComeetClient,
    position_country,
    position_lead_recruiter,
)
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
)

log = logging.getLogger(__name__)
router = APIRouter()


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
