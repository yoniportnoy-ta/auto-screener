"""Per-call debug capture for scoring.

Port of DebugLog.gs. Each scoring call writes one row to `debug_scoring`
showing what anchors fired, which rubric was used, what raw rating Claude
returned, and what we ended up applying. Lets us diagnose any rating later.
"""
from __future__ import annotations

import logging
import os

from .db import db_session
from .models import DebugScoring

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 4000
MAX_ANCHORS_CHARS = 2000
MAX_RUBRIC_CHARS = 1500
MAX_SUMMARY_CHARS = 400


def is_debug_logging_enabled() -> bool:
    """Toggled via SCREENER_DEBUG_LOGGING env var. Default off."""
    val = (os.environ.get("SCREENER_DEBUG_LOGGING") or "").strip().lower()
    return val in {"1", "true", "yes"}


def append_debug_log(
    *,
    candidate_uid: str,
    candidate_name: str,
    position_uid: str,
    position_name: str,
    class_id: str,
    anchors_count: int,
    anchors_critical: int,
    anchors_block: str,
    rubric_used: bool,
    rubric_snippet: str,
    raw_rating: int | None,
    final_rating: int | None,
    calibration_delta: float | None,
    arithmetic_applied: bool,
    confidence: float | None,
    summary: str,
    strengths: list[str] | None,
    gaps: list[str] | None,
) -> None:
    """Best-effort insert. Never raises — debug logging is non-critical."""
    if not is_debug_logging_enabled():
        return
    try:
        with db_session() as session:
            row = DebugScoring(
                candidate_uid=candidate_uid,
                candidate_name=candidate_name,
                position_uid=position_uid,
                position_name=position_name,
                class_id=class_id,
                anchors_used=anchors_count,
                anchors_critical=anchors_critical,
                anchors_block=_truncate(anchors_block, MAX_ANCHORS_CHARS),
                rubric_used=rubric_used,
                rubric_snippet=_truncate(rubric_snippet, MAX_RUBRIC_CHARS),
                raw_rating=raw_rating,
                final_rating=final_rating,
                calibration_delta=calibration_delta,
                arithmetic_applied=arithmetic_applied,
                confidence=confidence,
                summary=_truncate(summary, MAX_SUMMARY_CHARS),
                strengths_json=list(strengths or []),
                gaps_json=list(gaps or []),
            )
            session.add(row)
    except Exception as exc:  # noqa: BLE001
        log.warning("debug_log insert failed: %s", exc)


def _truncate(s: str | None, limit: int) -> str | None:
    if not s:
        return s
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


__all__ = ["append_debug_log", "is_debug_logging_enabled"]
