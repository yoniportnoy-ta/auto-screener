"""Position-class registry — same catalogue as the Apps Script version, plus
custom classes added by recruiters.

Replaces SCREENER_POS_CLASS_<uid> + SCREENER_CUSTOM_CLASSES script properties.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import db_session
from .models import CustomPositionClass, PositionClass


@dataclass(frozen=True)
class ClassDefinition:
    id: str
    name: str
    levels: tuple[str, ...] = ()


# Same defaults that shipped in ScoringV2.gs.
DEFAULT_CLASSES: tuple[ClassDefinition, ...] = (
    ClassDefinition("product_management", "Product Management"),
    ClassDefinition("business_development", "Business Development"),
    ClassDefinition("customer_success", "Customer Success", ("Junior", "Mid Level", "Enterprise")),
    ClassDefinition("data_analyst", "Data Analyst"),
    ClassDefinition("analytical_engineering", "Analytical Engineering"),
    ClassDefinition("product_design", "Product Design"),
    ClassDefinition("devops_security", "DevOps and Security"),
    ClassDefinition("backend", "Backend"),
    ClassDefinition("frontend_fullstack", "Frontend and Fullstack"),
    ClassDefinition("engineering_leadership", "Engineering Leadership"),
    ClassDefinition("core_engineering", "Core Engineering"),
    ClassDefinition("qa", "QA"),
    ClassDefinition("controller", "Controller"),
    ClassDefinition("knowledge_base_writer", "Knowledge Base Writer"),
    ClassDefinition("account_executive", "Account Executive", ("SMB", "Mid-Market", "Enterprise")),
    ClassDefinition("revenue_operations", "Revenue Operations"),
)


def list_all_classes() -> list[dict]:
    """Default catalogue + custom classes, returned as plain dicts for JSON."""
    out = [{"id": c.id, "name": c.name, "levels": list(c.levels)} for c in DEFAULT_CLASSES]
    with db_session() as session:
        custom = session.scalars(select(CustomPositionClass)).all()
        for row in custom:
            out.append({"id": row.class_id, "name": row.class_name, "levels": list(row.levels_json or [])})
    return out


def create_custom_class(name: str, levels: Iterable[str] = ()) -> dict:
    """Add a recruiter-created class. Idempotent on (case-insensitive) name."""
    n = (name or "").strip()
    if not n:
        raise ValueError("class name required")
    cid = "".join(c.lower() if c.isalnum() else "_" for c in n).strip("_")
    if not cid:
        raise ValueError("class id could not be derived from name")

    existing = list_all_classes()
    for item in existing:
        if item["id"] == cid or item["name"].lower() == n.lower():
            return {"id": item["id"], "name": item["name"], "levels": item.get("levels", []), "alreadyExisted": True}

    levels_list = [s for s in (levels or []) if s]
    with db_session() as session:
        session.add(CustomPositionClass(class_id=cid, class_name=n, levels_json=levels_list))
    return {"id": cid, "name": n, "levels": levels_list, "alreadyExisted": False}


def get_position_class(position_uid: str) -> dict | None:
    """Returns the saved class assignment for a position, or None."""
    if not position_uid:
        return None
    with db_session() as session:
        row = session.scalar(select(PositionClass).where(PositionClass.position_uid == position_uid))
        if not row:
            return None
        return {
            "classId": row.class_id,
            "className": row.class_name,
            "level": row.level,
            "autoScreenEnabled": bool(row.auto_screen_enabled),
            "recruiterNotes": row.recruiter_notes or "",
        }


def set_auto_screen_enabled(position_uid: str, enabled: bool) -> dict:
    """Toggle the auto-screen flag for a position. The position must already
    have a class assigned; we don't auto-create one here."""
    uid = (position_uid or "").strip()
    if not uid:
        raise ValueError("position_uid required")
    with db_session() as session:
        row = session.scalar(select(PositionClass).where(PositionClass.position_uid == uid))
        if not row:
            raise ValueError(
                "Position has no class assigned yet — pick a class before enabling auto-screen."
            )
        row.auto_screen_enabled = bool(enabled)
    return {
        "positionUid": uid,
        "autoScreenEnabled": bool(enabled),
    }


def set_recruiter_notes(position_uid: str, notes: str) -> dict:
    """Persist free-form recruiter notes for a position. Empty string clears."""
    uid = (position_uid or "").strip()
    if not uid:
        raise ValueError("position_uid required")
    n = (notes or "").strip()
    with db_session() as session:
        row = session.scalar(select(PositionClass).where(PositionClass.position_uid == uid))
        if not row:
            raise ValueError(
                "Position has no class assigned yet — pick a class before saving notes."
            )
        row.recruiter_notes = n or None
    return {"positionUid": uid, "recruiterNotes": n}


def list_auto_screen_positions() -> list[str]:
    """Position UIDs the cron should walk. Order is stable (alpha by class then uid)."""
    with db_session() as session:
        rows = session.scalars(
            select(PositionClass).where(PositionClass.auto_screen_enabled.is_(True))
        ).all()
        return [row.position_uid for row in rows]


def assign_position_class(position_uid: str, class_id: str, level: str = "") -> dict:
    """Persist a position → class mapping. Idempotent (UPSERT)."""
    uid = (position_uid or "").strip()
    cid = (class_id or "").strip()
    if not uid or not cid:
        raise ValueError("position_uid and class_id required")

    classes = list_all_classes()
    cls = next((c for c in classes if c["id"] == cid), None)
    if not cls:
        raise ValueError(f"unknown class id {cid!r}")

    lvl = (level or "").strip() or None
    with db_session() as session:
        stmt = pg_insert(PositionClass).values(
            position_uid=uid,
            class_id=cid,
            class_name=cls["name"],
            level=lvl,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[PositionClass.position_uid],
            set_={
                "class_id": stmt.excluded.class_id,
                "class_name": stmt.excluded.class_name,
                "level": stmt.excluded.level,
            },
        )
        session.execute(stmt)
    return {"classId": cid, "className": cls["name"], "level": lvl}


__all__ = [
    "ClassDefinition",
    "DEFAULT_CLASSES",
    "list_all_classes",
    "create_custom_class",
    "get_position_class",
    "assign_position_class",
    "set_auto_screen_enabled",
    "list_auto_screen_positions",
]
