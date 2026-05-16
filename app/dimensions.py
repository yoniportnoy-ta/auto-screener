"""Six-dimension scoring rubric + per-position weights.

The internal rating Claude emits is no longer a single number — it's
six 1-10 sub-scores plus a confidence. The recruiter-facing overall
rating is the weighted sum, using whatever weights the recruiter set
for this specific position (or sensible defaults).

  domain_match        — does the candidate's skills/stack match the role
  company_tier        — quality of recent employers (tier-1, agency, etc.)
  career_progression  — title + scope growth across timeline
  location_match      — country/relocation alignment with the position
  university_tier     — highest-degree institution
  achievements        — concrete scale/scope/impact numbers in the CV

Weights are percentages (0-100) that sum to 100. Per-position storage
lives in PositionClass.dimension_weights_json — null means defaults.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from .db import db_session
from .models import PositionClass

log = logging.getLogger(__name__)


# Canonical key order — also the order shown in UI sliders.
DIMENSIONS: tuple[str, ...] = (
    "domain_match",
    "company_tier",
    "career_progression",
    "location_match",
    "university_tier",
    "achievements",
)

# Human-readable labels for the UI. Keep in sync with the prompt copy.
DIMENSION_LABELS: dict[str, str] = {
    "domain_match": "Domain match",
    "company_tier": "Company tier",
    "career_progression": "Career progression",
    "location_match": "Location match",
    "university_tier": "University tier",
    "achievements": "Concrete achievements",
}

DIMENSION_DESCRIPTIONS: dict[str, str] = {
    "domain_match": "How closely the candidate's skills, stack, and recent work match this exact role.",
    "company_tier": "Are recent employers tier-1 product companies, smaller product cos, or service/agency shops?",
    "career_progression": "Title + scope growth across the timeline (Junior → Senior → Lead → Manager).",
    "location_match": "Does the candidate live where the role is, or have they stated they'll relocate?",
    "university_tier": "Tier-1 university (Technion, MIT, Stanford...), respected national, or other.",
    "achievements": "Concrete scale/scope numbers (DAUs, revenue, team size, launches) vs vague claims.",
}

# Defaults if a position has no override. Tuned for engineering-ish roles
# at Riverside; recruiters can rebalance per position via the slider UI.
DEFAULT_WEIGHTS: dict[str, int] = {
    "domain_match": 25,
    "company_tier": 20,
    "career_progression": 15,
    "location_match": 15,
    "university_tier": 10,
    "achievements": 15,
}
assert sum(DEFAULT_WEIGHTS.values()) == 100, "default weights must sum to 100"


def get_weights(position_uid: str) -> dict[str, int]:
    """Read a position's weights, falling back to defaults.

    Always returns a dict with all six dimensions, summing to 100.
    """
    if not position_uid:
        return dict(DEFAULT_WEIGHTS)
    with db_session() as ses:
        row = ses.scalar(
            select(PositionClass).where(PositionClass.position_uid == position_uid)
        )
        raw = row.dimension_weights_json if (row and row.dimension_weights_json) else None
    if not isinstance(raw, dict):
        return dict(DEFAULT_WEIGHTS)
    # Coerce + fill missing dimensions from defaults.
    merged = dict(DEFAULT_WEIGHTS)
    for k in DIMENSIONS:
        v = raw.get(k)
        try:
            iv = int(v)
            if 0 <= iv <= 100:
                merged[k] = iv
        except (TypeError, ValueError):
            pass
    # Sanity: if the stored values don't sum to 100, renormalise. Don't
    # error — bad data shouldn't break scoring.
    total = sum(merged.values())
    if total == 0:
        return dict(DEFAULT_WEIGHTS)
    if total != 100:
        scale = 100.0 / total
        merged = {k: max(0, min(100, round(v * scale))) for k, v in merged.items()}
        # Largest-remainder adjustment to land exactly at 100.
        diff = 100 - sum(merged.values())
        if diff != 0:
            # Apply the diff to the largest weight (least proportional impact).
            biggest = max(merged, key=lambda k: merged[k])
            merged[biggest] = max(0, min(100, merged[biggest] + diff))
    return merged


def set_weights(position_uid: str, weights: dict[str, Any]) -> dict[str, int]:
    """Persist a fresh weight dict for this position. Validates that the
    six known dimensions are present and they sum to exactly 100.

    Raises ValueError on bad input.
    """
    uid = (position_uid or "").strip()
    if not uid:
        raise ValueError("position_uid required")

    cleaned: dict[str, int] = {}
    for k in DIMENSIONS:
        v = weights.get(k)
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"weight for {k!r} must be an integer")
        if not (0 <= iv <= 100):
            raise ValueError(f"weight for {k!r} must be 0-100 (got {iv})")
        cleaned[k] = iv

    if sum(cleaned.values()) != 100:
        raise ValueError(
            f"weights must sum to exactly 100 (got {sum(cleaned.values())})"
        )

    with db_session() as ses:
        row = ses.scalar(select(PositionClass).where(PositionClass.position_uid == uid))
        if not row:
            raise ValueError(
                "position has no class assigned yet — pick a class before setting weights"
            )
        row.dimension_weights_json = cleaned
        ses.commit()
    return cleaned


def compute_overall(sub_scores: dict[str, int | None], weights: dict[str, int]) -> int | None:
    """Weighted sum of sub-scores → overall 1-10 rating.

    Missing dimensions are skipped and their weight redistributed
    proportionally across the present ones. Returns None when ALL
    sub-scores are missing.
    """
    present: dict[str, int] = {}
    for k in DIMENSIONS:
        v = sub_scores.get(k)
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        present[k] = max(1, min(10, iv))

    if not present:
        return None

    # Sum of weights for dimensions we have scores for.
    used_weight = sum(weights.get(k, 0) for k in present)
    if used_weight <= 0:
        # All present-dim weights are 0 — fall back to simple mean.
        return max(1, min(10, round(sum(present.values()) / len(present))))

    weighted_sum = sum(present[k] * weights.get(k, 0) for k in present)
    overall_raw = weighted_sum / used_weight
    return max(1, min(10, round(overall_raw)))


__all__ = [
    "DIMENSIONS",
    "DIMENSION_LABELS",
    "DIMENSION_DESCRIPTIONS",
    "DEFAULT_WEIGHTS",
    "get_weights",
    "set_weights",
    "compute_overall",
]
