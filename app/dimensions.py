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
# Domain match always counts for this share of the overall rating. Hidden
# from the recruiter — they don't see this in the UI.
DOMAIN_WEIGHT_PCT = 33

# The four slider dimensions share the remaining 67% of the overall.
# The recruiter sees those 4 sliders summing to 100% (relative preference
# among the four), and we scale them into this internal budget when
# computing the weighted overall.
SLIDER_WEIGHT_BUDGET = 100 - DOMAIN_WEIGHT_PCT  # = 67

# What the recruiter sees on the UI: sliders summing to this value. Stays
# at 100 forever — it's just a mental-model number.
SLIDER_WEIGHT_USER_TOTAL = 100

# Location is not weighted into the rating at all — instead it's a hard
# gate. If the AI's location_match sub-score is below this threshold, the
# overall rating is forced to 1 regardless of everything else.
LOCATION_GATE_THRESHOLD = 4

# Domain match is the most important fit signal. If the candidate's
# domain_match sub-score is weak (below this threshold), the overall
# is capped at DOMAIN_CAP_RATING — even if all other axes are 10/10.
# Rationale: a pharma recruiter applying to a tech-recruiter role might
# look stellar on every other axis (career arc, employers, achievements)
# but the domain mismatch alone should prevent the AI from rating them
# 7+. Strong sliders pull them up TO the cap, never past it.
DOMAIN_GATE_THRESHOLD = 5
DOMAIN_CAP_RATING = 5


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

# Defaults for the four sliders, expressed as percentages summing to 100
# (the recruiter-facing total). Internally these are scaled into the 67%
# slider budget when computing the overall.
DEFAULT_WEIGHTS: dict[str, int] = {
    "company_tier": 40,
    "career_progression": 25,
    "university_tier": 12,
    "achievements": 23,
}
assert sum(DEFAULT_WEIGHTS.values()) == SLIDER_WEIGHT_USER_TOTAL, (
    f"default slider weights must sum to {SLIDER_WEIGHT_USER_TOTAL}"
)


def get_weights(position_uid: str) -> dict[str, int]:
    """Read a position's slider weights (4 entries summing to 100 — the
    recruiter-facing total), falling back to defaults. Domain and location
    are NOT in this dict — they're handled separately by `compute_overall`.
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
            if 0 <= iv <= SLIDER_WEIGHT_USER_TOTAL:
                merged[k] = iv
        except (TypeError, ValueError):
            pass

    # Renormalise if the stored values don't sum exactly to the user total.
    # Handles two legacy cases:
    #   - sum=67 (old internal budget) → scaled up to 100
    #   - sum=anything else            → proportionally normalized to 100
    total = sum(merged.values())
    if total == 0:
        return dict(DEFAULT_WEIGHTS)
    if total != SLIDER_WEIGHT_USER_TOTAL:
        scale = SLIDER_WEIGHT_USER_TOTAL / total
        merged = {
            k: max(0, min(SLIDER_WEIGHT_USER_TOTAL, round(v * scale)))
            for k, v in merged.items()
        }
        diff = SLIDER_WEIGHT_USER_TOTAL - sum(merged.values())
        if diff != 0:
            biggest = max(merged, key=lambda k: merged[k])
            merged[biggest] = max(0, min(SLIDER_WEIGHT_USER_TOTAL, merged[biggest] + diff))
    return merged


def set_weights(position_uid: str, weights: dict[str, Any]) -> dict[str, int]:
    """Persist a fresh weight dict for this position. Must include the 4
    slider dimensions, each in 0-100, summing exactly to 100 (the user-
    facing total). Internally we'll scale into the 67% budget at scoring
    time — the recruiter never sees that.
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
        if not (0 <= iv <= SLIDER_WEIGHT_USER_TOTAL):
            raise ValueError(
                f"weight for {k!r} must be 0-{SLIDER_WEIGHT_USER_TOTAL} (got {iv})"
            )
        cleaned[k] = iv

    if sum(cleaned.values()) != SLIDER_WEIGHT_USER_TOTAL:
        raise ValueError(
            f"slider weights must sum to exactly {SLIDER_WEIGHT_USER_TOTAL} "
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

    # Domain + sliders. Domain weight is fixed at DOMAIN_WEIGHT_PCT (33).
    # Slider weights are stored as 100-sum recruiter preferences; we
    # scale each into the 67% slider budget at compute time so the total
    # weight (domain + scaled sliders) sums to 100.
    pieces: list[tuple[int, float]] = []   # (score, weight_share)

    domain = sub_scores.get("domain_match")
    if domain is not None:
        try:
            d = max(1, min(10, int(domain)))
            pieces.append((d, float(DOMAIN_WEIGHT_PCT)))
        except (TypeError, ValueError):
            pass

    scale = SLIDER_WEIGHT_BUDGET / SLIDER_WEIGHT_USER_TOTAL  # = 0.67
    for k in DIMENSIONS:
        v = sub_scores.get(k)
        if v is None:
            continue
        try:
            iv = max(1, min(10, int(v)))
        except (TypeError, ValueError):
            continue
        w_stored = slider_weights.get(k, 0)
        if w_stored <= 0:
            continue
        pieces.append((iv, w_stored * scale))

    if not pieces:
        return None

    used_weight = sum(w for _, w in pieces)
    if used_weight <= 0:
        # All weights happen to be 0 — fall back to simple mean.
        result = max(1, min(10, round(sum(s for s, _ in pieces) / len(pieces))))
    else:
        weighted = sum(s * w for s, w in pieces)
        result = max(1, min(10, round(weighted / used_weight)))

    # Domain soft cap: if domain_match is weak (< threshold), don't let
    # strong sliders push the overall above DOMAIN_CAP_RATING. Common
    # scenario: tier-1-everything candidate from the wrong professional
    # field — pharma recruiter for a tech-recruiter role, full-stack dev
    # for a data-engineer role, etc.
    domain_raw = sub_scores.get("domain_match")
    try:
        domain_int = int(domain_raw) if domain_raw is not None else None
    except (TypeError, ValueError):
        domain_int = None
    if domain_int is not None and domain_int < DOMAIN_GATE_THRESHOLD and result > DOMAIN_CAP_RATING:
        log.info(
            "compute_overall: domain cap fired (domain=%s < %s) — capping %s to %s",
            domain_int, DOMAIN_GATE_THRESHOLD, result, DOMAIN_CAP_RATING,
        )
        result = DOMAIN_CAP_RATING

    return result


__all__ = [
    "DIMENSIONS",
    "ALL_SCORED_AXES",
    "DIMENSION_LABELS",
    "DIMENSION_DESCRIPTIONS",
    "DEFAULT_WEIGHTS",
    "DOMAIN_WEIGHT_PCT",
    "SLIDER_WEIGHT_BUDGET",
    "SLIDER_WEIGHT_USER_TOTAL",
    "LOCATION_GATE_THRESHOLD",
    "DOMAIN_GATE_THRESHOLD",
    "DOMAIN_CAP_RATING",
    "get_weights",
    "set_weights",
    "compute_overall",
]
