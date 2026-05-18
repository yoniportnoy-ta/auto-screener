"""Five-axis scoring rubric with a location hard gate and a domain soft cap.

The AI emits six 1-10 sub-scores. Five are recruiter-adjustable sliders:

  - company_domain         (do their employers do something similar to us?)
  - profession_domain      (are they the right kind of role for this job?)
  - company_tier           (recent employer quality)
  - career_progression     (title + scope growth)
  - university_tier        (academic signal)

The five sliders sum to exactly 100. The split between company_domain and
profession_domain replaced a single `domain_match` axis after benchmark
feedback showed the AI was conflating "they work at a tech company" with
"they do the same kind of work" — a Senior PM at Goldman has high company
tier but low profession adjacency (banking ≠ creator tools), and a great
PM at a creator-tools startup might have high profession_domain + lower
company_tier. Splitting forces the AI to evaluate both facets independently.

  - location_match         HARD GATE: if < LOCATION_GATE_THRESHOLD the
                           overall is auto-clamped to 1 regardless of how
                           strong the other axes look.

  - achievements           DEPRECATED. The AI no longer scores this axis
                           because candidates rarely put concrete metrics
                           on their CVs and the signal-to-noise was poor.
                           The DB column is kept nullable for historical
                           rows.

The overall rating is computed as:

    if location_match < LOCATION_GATE_THRESHOLD:
        overall = 1
    else:
        overall = round(sum(score[k] * weight[k] / 100 for k in sliders))
        if avg(company_domain, profession_domain) < DOMAIN_GATE_THRESHOLD:
            overall = min(overall, DOMAIN_CAP_RATING)

clamped to 1-10. The combined-average cap fires when the candidate is
weak on BOTH domain facets together — a strong company_domain can offset
a weak profession_domain (and vice versa) before the cap kicks in.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from .db import db_session
from .models import PositionClass

log = logging.getLogger(__name__)


# ─── Fixed parameters (NOT user-adjustable) ───────────────────────────────
# Slider weights are recruiter preferences and sum to this value. There's
# no separate internal budget — the user-facing total IS the math.
SLIDER_WEIGHT_USER_TOTAL = 100

# Back-compat constants for old imports. Neutral values so a stale call
# site doesn't crash.
DOMAIN_WEIGHT_PCT = 0
SLIDER_WEIGHT_BUDGET = 100

# Location is not weighted into the rating — it's a hard gate. If
# location_match < this threshold, overall is forced to 1 regardless of
# the other axes.
LOCATION_GATE_THRESHOLD = 4

# Domain soft cap: if the AVERAGE of (company_domain, profession_domain)
# is below this threshold, the overall is capped at DOMAIN_CAP_RATING
# (even if other sliders are 10/10). Rationale: a candidate with strong
# career signals but no domain fit (banking PM for a creator-tools role)
# shouldn't score 7+ no matter how impressive the rest of the CV is.
# Combined-average means a strong axis can partially offset a weak one
# before the cap fires — chosen over per-axis caps after benchmark review.
DOMAIN_GATE_THRESHOLD = 5
DOMAIN_CAP_RATING = 5


# ─── Slider dimensions (recruiter-adjustable) ─────────────────────────────
# Both domain axes lead because role-fit is the most important signal.
DIMENSIONS: tuple[str, ...] = (
    "profession_domain",
    "company_domain",
    "company_tier",
    "career_progression",
    "university_tier",
)

# All axes the AI scores — for prompt + storage. Includes location_match
# (gate-only, not weighted) on top of the five sliders. `achievements`
# is dropped from new scoring (legacy column stays nullable in DB).
# `domain_match` is kept here too for back-compat with any caller that
# still expects it; new code paths read company_domain / profession_domain.
ALL_SCORED_AXES: tuple[str, ...] = (
    "profession_domain",
    "company_domain",
    "company_tier",
    "career_progression",
    "location_match",
    "university_tier",
)

DIMENSION_LABELS: dict[str, str] = {
    "profession_domain": "Profession domain",
    "company_domain": "Company domain",
    "company_tier": "Company tier",
    "career_progression": "Career progression",
    "location_match": "Location match",
    "university_tier": "University tier",
    # Kept for back-compat with old DB rows that may still surface labels
    # in the legacy UI (calibration breakdown of historical scorings).
    "domain_match": "Domain match (legacy)",
    "achievements": "Concrete achievements (legacy)",
}

DIMENSION_DESCRIPTIONS: dict[str, str] = {
    "profession_domain": (
        "How closely the candidate's role/profession matches THIS job. "
        "Senior PM applying for Senior PM = 10. Designer with PM-adjacent work = 5. "
        "QA Engineer applying for PM = 1."
    ),
    "company_domain": (
        "Have their previous EMPLOYERS done something similar to us "
        "(creator-tools / podcasting / video / SaaS-for-creators / B2C tech)? "
        "Senior PM at Descript or Loom = 10. PM at a bank = 2."
    ),
    "company_tier": (
        "Are recent employers tier-1 product companies (Wix, Shopify, "
        "Allegro, CD Projekt Red, etc.), tier-2 unicorns, or service/agency shops?"
    ),
    "career_progression": (
        "Title + scope growth across the timeline (Junior → Senior → Lead → Manager)."
    ),
    "location_match": (
        "Acts as a hard gate, not a weighted score. If the candidate is in the wrong "
        "country and the CV doesn't say they'll relocate, the overall rating is "
        "auto-clamped to 1."
    ),
    "university_tier": (
        "Tier-1 university (Technion, MIT, Stanford, Shopify-feeder Waterloo, "
        "Warsaw UoT, etc.), respected national, or other. Israeli elite programs "
        "(Talpiot, 8200, Mamram) count as tier-1. NO degree → ≤ 3."
    ),
    "domain_match": (
        "DEPRECATED. Replaced by company_domain + profession_domain. The combined "
        "average of those two acts as the soft cap."
    ),
    "achievements": (
        "DEPRECATED. No longer scored — candidates rarely include concrete metrics "
        "and the signal-to-noise was poor."
    ),
}

# Defaults for the five sliders, summing to 100. Profession-domain leads
# (most disqualifying axis); company-tier follows; company-domain captures
# industry adjacency; progression captures trajectory; university is the
# weakest stand-alone signal.
DEFAULT_WEIGHTS: dict[str, int] = {
    "profession_domain": 23,
    "company_domain": 13,
    "company_tier": 27,
    "career_progression": 20,
    "university_tier": 17,
}
assert sum(DEFAULT_WEIGHTS.values()) == SLIDER_WEIGHT_USER_TOTAL, (
    f"default slider weights must sum to {SLIDER_WEIGHT_USER_TOTAL}"
)


# Legacy keys we silently strip + remap when reading stored weight dicts.
# Older PositionClass rows still have these.
_LEGACY_KEYS_TO_DROP: tuple[str, ...] = ("achievements",)


def get_weights(position_uid: str) -> dict[str, int]:
    """Read a position's slider weights (5 entries summing to 100),
    falling back to defaults. Location is not in this dict — it's the
    hard gate, handled separately by `compute_overall`.

    Handles three legacy stored shapes:
      - {company_tier, career_progression, university_tier, achievements}
        (pre-deprecation) → drop achievements, fill in domain dims, scale.
      - {domain_match, company_tier, career_progression, university_tier}
        (post-deprecation, pre-split) → split domain_match equally into
        company_domain + profession_domain, scale.
      - current 5-key shape → use directly.
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

    # If a legacy row still has `domain_match`, split it equally into the
    # two new domain axes before we apply the normal merge.
    raw = dict(raw)
    if "domain_match" in raw and (
        "company_domain" not in raw and "profession_domain" not in raw
    ):
        dm = 0
        try:
            dm = int(raw["domain_match"])
        except (TypeError, ValueError):
            dm = 0
        # Split with profession getting the slight lean (matches default
        # ratio 23:13).
        prof_share = round(dm * 23 / 36)
        comp_share = dm - prof_share
        raw["profession_domain"] = prof_share
        raw["company_domain"] = comp_share
        del raw["domain_match"]

    # Start from defaults so any newly-introduced key (e.g. company_domain
    # on a row stored before this refactor) is populated.
    merged = dict(DEFAULT_WEIGHTS)
    for k in DIMENSIONS:
        v = raw.get(k)
        try:
            iv = int(v)
            if 0 <= iv <= SLIDER_WEIGHT_USER_TOTAL:
                merged[k] = iv
        except (TypeError, ValueError):
            pass

    # Renormalise so the five current sliders sum to exactly 100.
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
            merged[biggest] = max(
                0, min(SLIDER_WEIGHT_USER_TOTAL, merged[biggest] + diff)
            )
    return merged


def set_weights(position_uid: str, weights: dict[str, Any]) -> dict[str, int]:
    """Persist a fresh weight dict for this position. Must include the 5
    slider dimensions (profession_domain, company_domain, company_tier,
    career_progression, university_tier), each in 0-100, summing exactly
    to 100.
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
    """Combine the AI sub-scores into a final 1-10 rating.

    Decision flow:
      1. If location_match < LOCATION_GATE_THRESHOLD → return 1 (gate).
      2. Otherwise: weighted sum of the five sliders (profession_domain,
         company_domain, company_tier, career_progression, university_tier).
         Weights are stored summing to 100.
      3. If avg(profession_domain, company_domain) < DOMAIN_GATE_THRESHOLD
         → cap result at DOMAIN_CAP_RATING (soft cap).

    Missing axes are skipped and their weight is redistributed across
    the present axes. Returns None only when NO sub-scores are present.

    Back-compat: if `domain_match` is the only domain key present (legacy
    AI output during transition), it's treated as both company_domain
    and profession_domain.
    """
    # 1) Hard gate: location.
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

    # Back-compat: fan domain_match out to both domain axes if needed.
    sub_scores = dict(sub_scores)
    legacy_dm = sub_scores.get("domain_match")
    if (
        legacy_dm is not None
        and sub_scores.get("company_domain") is None
        and sub_scores.get("profession_domain") is None
    ):
        sub_scores["company_domain"] = legacy_dm
        sub_scores["profession_domain"] = legacy_dm

    # 2) Weighted sum across the five sliders.
    pieces: list[tuple[int, float]] = []   # (score, weight)
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
        pieces.append((iv, float(w)))

    if not pieces:
        return None

    used_weight = sum(w for _, w in pieces)
    if used_weight <= 0:
        result = max(1, min(10, round(sum(s for s, _ in pieces) / len(pieces))))
    else:
        weighted = sum(s * w for s, w in pieces)
        result = max(1, min(10, round(weighted / used_weight)))

    # 3) Domain soft cap: average of the two domain axes. If either is
    # missing, use whichever is present. If both are missing, no cap.
    def _to_int(v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    prof = _to_int(sub_scores.get("profession_domain"))
    comp = _to_int(sub_scores.get("company_domain"))
    present = [x for x in (prof, comp) if x is not None]
    if present:
        domain_avg = sum(present) / len(present)
        if domain_avg < DOMAIN_GATE_THRESHOLD and result > DOMAIN_CAP_RATING:
            log.info(
                "compute_overall: domain cap fired "
                "(prof=%s, comp=%s, avg=%.2f < %s) — capping %s to %s",
                prof, comp, domain_avg, DOMAIN_GATE_THRESHOLD, result, DOMAIN_CAP_RATING,
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
