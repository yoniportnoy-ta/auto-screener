"""Global admin-level controls for the auto-screener.

Two levers live here:

  - `admin_thumbs_up_floor` — integer 1-5. The minimum rating that can
    bucket as 👍 regardless of what any individual recruiter calibrated.
    Stacks with per-recruiter thresholds via `max(...)`. Used by both
    the calibration bucketing UI and the auto-tag gate.

  - `admin_brief` — free text. Appended to every scoring prompt as a
    [GLOBAL ADMIN GUIDANCE] block, so policy-level instructions ("all
    hires must be IC", "no candidates currently at competitors", etc.)
    apply uniformly without rewriting per-position briefs.

Access is gated on the recruiter being in the ADMIN_RECRUITERS env var
(comma-separated list of names). This is internal tooling — we don't
need full RBAC here, just a soft fence.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import db_session
from .models import AdminSetting

log = logging.getLogger(__name__)

KEY_THUMBS_UP_FLOOR = "admin_thumbs_up_floor"
KEY_BRIEF = "admin_brief"


def is_admin(recruiter_name: str) -> bool:
    """Check whether this recruiter has admin permissions.

    Reads from ADMIN_RECRUITERS env var: comma-separated names matched
    case-insensitively against the recruiter_name we already use elsewhere.
    Empty / unset env var → no one is admin (safe default).
    """
    raw = (os.environ.get("ADMIN_RECRUITERS") or "").strip()
    if not raw:
        return False
    allowed = {s.strip().casefold() for s in raw.split(",") if s.strip()}
    return (recruiter_name or "").strip().casefold() in allowed


def _read(key: str) -> str | None:
    with db_session() as ses:
        row = ses.scalar(select(AdminSetting).where(AdminSetting.key == key))
        return (row.value if row else None) or None


def _write(key: str, value: str | None) -> None:
    val = (value or "").strip() or None
    with db_session() as ses:
        stmt = pg_insert(AdminSetting).values(key=key, value=val)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AdminSetting.key],
            set_={"value": stmt.excluded.value},
        )
        ses.execute(stmt)
        ses.commit()


def get_settings() -> dict[str, Any]:
    """Public read. Returns dict with both fields, normalized.

    `thumbsUpFloor` is on the internal 1-10 scale (matches the calibration
    threshold and AI rating). UI dropdowns expose 1-10 values directly.
    """
    floor_raw = _read(KEY_THUMBS_UP_FLOOR)
    floor: int | None = None
    if floor_raw:
        try:
            v = int(floor_raw)
            if 1 <= v <= 10:
                floor = v
        except (ValueError, TypeError):
            pass
    brief = _read(KEY_BRIEF) or ""
    return {
        "thumbsUpFloor": floor,
        "brief": brief,
    }


def set_settings(
    *,
    thumbs_up_floor: int | None = None,
    brief: str | None = None,
) -> dict[str, Any]:
    """Write either or both fields. None = leave unchanged (don't clear).
    Pass `0` / empty string explicitly to clear (treated as None internally
    on the floor side; empty string clears the brief).

    `thumbs_up_floor` is on the 1-10 internal scale.
    """
    if thumbs_up_floor is not None:
        if thumbs_up_floor == 0:
            _write(KEY_THUMBS_UP_FLOOR, None)
        else:
            if not (1 <= thumbs_up_floor <= 10):
                raise ValueError("thumbs_up_floor must be 1-10 (or 0 to clear)")
            _write(KEY_THUMBS_UP_FLOOR, str(thumbs_up_floor))
    if brief is not None:
        _write(KEY_BRIEF, brief.strip())
    return get_settings()


def get_thumbs_up_floor() -> int | None:
    """Convenience reader used by the threshold + tagging hot paths.
    Returns None when no admin floor is set."""
    s = get_settings()
    return s.get("thumbsUpFloor")


def get_admin_brief() -> str:
    """Convenience reader used by the scoring prompt builder."""
    return get_settings().get("brief") or ""


__all__ = [
    "is_admin",
    "get_settings",
    "set_settings",
    "get_thumbs_up_floor",
    "get_admin_brief",
    "KEY_THUMBS_UP_FLOOR",
    "KEY_BRIEF",
]
