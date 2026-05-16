"""Six-axis scoring rubric with a fixed domain weight + location hard gate.

The AI emits six 1-10 sub-scores. Two of them are NOT slider-controlled:

  - domain_match    — always weights 33% of the overall (the role-fit signal
                      is too important to let a recruiter de-prioritise it
                      below other axes; the 33% lock makes that explicit).

  - location_match  — not weighted into the overall at all. Instead it acts
                      as a HARD GATE: if the candidate's location score is
                      below LOCATION_GATE_THRESHOLD (i.e., wrong country +
                      no relocation statement), the overall is auto-clamped
                      to 1 regardless of how strong the other axes look.

The remaining four axes are recruiter-adjustable sliders that must sum to
the SLIDER_WEIGHT_BUDGET (= 67%, the share that's not taken by domain):

  - company_tier
  - career_progression
  - university_tier
  - achievements

The overall rating is computed as:

    if location_match < LOCATION_GATE_THRESHOLD:
        overall = 1
    else:
        overall = round(
            domain_match    * 0.33
          + sum(slider_score * slider_weight/100 for the 4 slider axes)
          * (SLIDER_WEIGHT_BUDGET / 100)
        )

clamped to 1-10.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from .db import db_session
from .models import PositionClass

log = logging.getLogger(__name__)


# ─── Fixed parameters (NOT user-adjustable) ───────────────────────────────
# Domain match always counts for this share of the overall rating.
DOMAIN_WEIGHT_PCT = 33

# Everything else (the four sliders) shares this remaining share. The four
# slider weights stored per position must sum to this value.
SLIDER_WEIGHT_BUDGET = 100 - DOMAIN_WEIGHT_PCT  # = 67

# Location is not weighted into the rating at all — instead it's a hard
# gate. If the AI's location_match sub-score is below this threshold, the
# overall rating is forced to 1 regardless of everything else.
LOCATION_GATE_THRESHOLD = 4


# ─── Slider dimensions (recruiter-adjustable) ─────────────────────────────
DIMENSIONS: tuple[str, ...] = (
    "company_tier",
    "career_progression",
    "university_tier",
    "achievements",
)

# All six axes the AI scores — for prompt + storage. Domain and location
# live here too even though they're not slider-controlled.
ALL_SCORED_AXES: tuple[str, ...] = (
    "domain_match",
    "company_tier",
    "career_progression",
    "location_match",
    "university_tier",
    "achievements",
)

DIMENSION_LABELS: dict[str, str] = {
    "domain_match": "Domain match",
    "company_tier": "Company tier",
    "career_progression": "Career progression",
    "location_match": "Location match",
    "university_tier": "University tier",
    "achievements": "Concrete achievements",
}

DIMENSION_DESCRIPTIONS: dict[str, str] = {
    "domain_match": (
        "How closely the candidate's skills, stack, and recent work match this exact role. "
        "Always weights 33% of the overall (fixed) — too important to de-prioritise."
    ),
    "company_tier": "Are recent employers tier-1 product companies, smaller product cos, or service/agency shops?",
    "career_progression": "Title + scope growth across the timeline (Junior → Senior → Lead → Manager).",
    "location_match": (
        "Acts as a hard gate, not a weighted score. If the candidate is in the wrong country "
        "and the CV doesn't say they'll relocate, the overall rating is auto-clamped to 1."
    ),
    "university_tier": "Tier-1 university (Technion, MIT, Stanford...), respected national, or other.",
    "achievements": "Concrete scale/scope numbers (DAUs, revenue, team size, launches) vs vague claims.",
}

# Defaults for the four sliders. Must sum to SLIDER_WEIGHT_BUDGET.
DEFAULT_WEIGHTS: dict[str, int] = {
    "company_tier": 27,
    "career_progression": 17,
    "university_tier": 8,
    "achievements": 15,
}
assert sum(DEFAULT_WEIGHTS.values()) == SLIDER_WEIGHT_BUDGET, (
    f"default slider weights must sum to {SLIDER_WEIGHT_BUDGET}"
)


def get_weights(position_uid: str) -> dict[str, int]:
    """Read a position's slider weights (4 entries summing to 67), falling
    back to defaults. Domain and location are NOT in this dict — they're
    handled separately by `compute_overall`.
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

    merged = dict(DEFAULT_WEIGHTS)
    for k in DIMENSIONS:
        v = raw.get(k)
        try:
            iv = int(v)
            if 0 <= iv <= SLIDER_WEIGHT_BUDGET:
                merged[k] = iv
        except (TypeError, ValueError):
            pass

    # Renormalise if the stored values don't sum exactly to the budget.
    total = sum(merged.values())
    if total == 0:
        return dict(DEFAULT_WEIGHTS)
    if total != SLIDER_WEIGHT_BUDGET:
        scale = SLIDER_WEIGHT_BUDGET / total
        merged = {
            k: max(0, min(SLIDER_WEIGHT_BUDGET, round(v * scale)))
            for k, v in merged.items()
        }
        diff = SLIDER_WEIGHT_BUDGET - sum(merged.values())
        if diff != 0:
            biggest = max(merged, key=lambda k: merged[k])
            merged[biggest] = max(0, min(SLIDER_WEIGHT_BUDGET, merged[biggest] + diff))
    return merged


def set_weights(position_uid: str, weights: dict[str, Any]) -> dict[str, int]:
    """Persist a fresh weight dict for this position. Must include the 4
    slider dimensions, each in 0-SLIDER_WEIGHT_BUDGET, summing exactly to
    SLIDER_WEIGHT_BUDGET (67).
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
        if not (0 <= iv <= SLIDER_WEIGHT_BUDGET):
            raise ValueError(
                f"weight for {k!r} must be 0-{SLIDER_WEIGHT_BUDGET} (got {iv})"
            )
        cleaned[k] = iv

    if sum(cleaned.values()) != SLIDER_WEIGHT_BUDGET:
        raise ValueError(
            f"slider weights must sum to exactly {SLIDER_WEIGHT_BUDGET} "
            f"(got {sum(cleaned.values())})"
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


def compute_overall(
    sub_scores: dict[str, int | None],
    slider_weights: dict[str, int],
) -> int | None:
    """Combine the six AI sub-scores into a final 1-10 rating.

    Decision flow:
      1. If location_match < LOCATION_GATE_THRESHOLD → return 1 (gate).
      2. Otherwise weight domain_match at DOMAIN_WEIGHT_PCT + the four
         slider dimensions at `slider_weights[k] / 100`. The slider
         weights are expected to sum to SLIDER_WEIGHT_BUDGET so the
         total weight integrates to 100.

    Missing axes are skipped and their weight is redistributed across the
    present axes (so a sparse CV doesn't break overall computation).

    Returns None only when NO sub-scores at all are available.
    """
    # Hard gate: location.
    loc = sub_scores.get("location_match")
    try:
        loc_int = int(loc) if loc is not None else None
    except (TypeError, ValueError):
        loc_int = None
    if loc_int is not None and loc_int < LOCATION_GATE_THRESHOLD:
        log.info(
            "compute_overall: location gate fired (location_match=%s < %s) — auto-1",
            loc_int, LOCATION_GATE_THRESHOLD,
        )
        return 1

    # Domain + sliders.
    pieces: list[tuple[int, int]] = []   # (score, weight_pct)

    domain = sub_scores.get("domain_match")
    if domain is not None:
        try:
            d = max(1, min(10, int(domain)))
            pieces.append((d, DOMAIN_WEIGHT_PCT))
        except (TypeError, ValueError):
            pass

    for k in DIMENSIONS:
        v = sub_scores.get(k)
        if v is None:
            continue
        try:
            iv = max(1, min(10, int(v)))
        except (TypeError, ValueError):
            continue
        w = slider_weights.get(k, 0)
        if w <= 0:
            continue
        pieces.append((iv, w))

    if not pieces:
        return None

    used_weight = sum(w for _, w in pieces)
    if used_weight <= 0:
        # All weights happen to be 0 — fall back to simple mean.
        return max(1, min(10, round(sum(s for s, _ in pieces) / len(pieces))))

    weighted = sum(s * w for s, w in pieces)
    return max(1, min(10, round(weighted / used_weight)))


__all__ = [
    "DIMENSIONS",
    "ALL_SCORED_AXES",
    "DIMENSION_LABELS",
    "DIMENSION_DESCRIPTIONS",
    "DEFAULT_WEIGHTS",
    "DOMAIN_WEIGHT_PCT",
    "SLIDER_WEIGHT_BUDGET",
    "LOCATION_GATE_THRESHOLD",
    "get_weights",
    "set_weights",
    "compute_overall",
]
