"""Post-scan rating distribution normalization.

Problem: even with a tight prompt and reflection step, Claude tends to
bunch ratings — too many 7s, not enough 1s or 10s. Calibration UX
suffers (everyone "looks the same") and the calibration queue draws
from a flat distribution.

Solution (gentle): after a scan/rescore that produced 10+ new scores
for a position, re-bucket all the latest DebugScoring rows for that
position to match a target distribution, **with a hard cap of ±2 on the
internal 1-10 scale (= ±1 on the recruiter-facing 5-scale)**.

We sort candidates by (current rating DESC, confidence DESC), so the
highest-confidence candidates anchor the top of their bucket; the
low-confidence candidates in an over-full bucket are the ones who get
nudged into adjacent buckets. That preserves the AI's strongest
opinions and only blurs its weakest ones.

  Example (10 candidates, all at 4-7 currently):
    Before: 4(2), 5(3), 6(3), 7(2)
    After:  3(1), 4(2), 5(2), 6(2), 7(2), 8(1)
  Each moved candidate shifted by ±1 (5-scale) at most.

Only triggers when *this batch* produced > batch_size_threshold scores
for the position — otherwise the cost of re-bucketing isn't worth it
and the pool isn't statistically meaningful anyway.

raw_rating is preserved untouched, so we can always audit "what did the
AI actually say" vs "what did normalization end up storing". The change
is logged at info level.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, select, update

from .db import db_session
from .models import DebugScoring
from .rating_scale import INTERNAL_MAX, INTERNAL_MIN

log = logging.getLogger(__name__)


# Target distribution on the internal 1-10 scale. Sums to ~1.0.
# Matches the rating-scale prompt — keep them in sync.
TARGET_DISTRIBUTION: dict[int, float] = {
    10: 0.03,
    9: 0.05,
    8: 0.07,
    7: 0.10,
    6: 0.15,
    5: 0.15,
    4: 0.15,
    3: 0.12,
    2: 0.10,
    1: 0.08,
}

# Hard cap on how far a single candidate's rating can shift during
# normalization, on the 1-10 internal scale. 2 = 1 step on the recruiter's
# 5-scale view.
MAX_DELTA_INTERNAL = 2

# Don't bother normalising for small batches — distribution math is noisy
# with <10 samples.
DEFAULT_MIN_BATCH = 10


def normalize_position_if_needed(
    position_uid: str,
    *,
    batch_scored: int,
    min_batch_size: int = DEFAULT_MIN_BATCH,
) -> dict[str, Any]:
    """Public entry. Caller passes `batch_scored` = how many candidates
    this scan/rescore just produced for this position. If that crosses
    the threshold, normalize the whole position's latest scoring pool.

    Returns:
        {
          "ran": bool,                # whether normalization actually ran
          "reason": str,              # short status
          "pool_size": int,           # candidates considered
          "changes": list[dict],      # what got moved (for logs/audit)
          "before": dict[int, int],   # distribution counts before
          "after": dict[int, int],    # distribution counts after
        }
    """
    if batch_scored < min_batch_size:
        return {
            "ran": False,
            "reason": f"batch_scored={batch_scored} < threshold={min_batch_size}",
            "pool_size": 0,
            "changes": [],
            "before": {},
            "after": {},
        }

    rows = _read_latest_scoring_pool(position_uid)
    if len(rows) < min_batch_size:
        return {
            "ran": False,
            "reason": f"pool_size={len(rows)} < threshold={min_batch_size}",
            "pool_size": len(rows),
            "changes": [],
            "before": {},
            "after": {},
        }

    before = _count_distribution(rows)
    changes = _compute_normalization_changes(rows)
    if not changes:
        return {
            "ran": True,
            "reason": "distribution already balanced",
            "pool_size": len(rows),
            "changes": [],
            "before": before,
            "after": before,
        }

    _apply_changes(changes)

    # Cheap "after" calc — patch the in-memory rows so we don't refetch.
    new_ratings_by_id = {ch["debug_id"]: ch["new"] for ch in changes}
    after_rows = [
        _RowView(
            id=r.id,
            candidate_uid=r.candidate_uid,
            final_rating=new_ratings_by_id.get(r.id, r.final_rating),
            confidence=r.confidence,
        )
        for r in rows
    ]
    after = _count_distribution(after_rows)

    log.info(
        "normalization: position=%s pool=%d changed=%d before=%s after=%s",
        position_uid, len(rows), len(changes), before, after,
    )

    return {
        "ran": True,
        "reason": f"adjusted {len(changes)} of {len(rows)} candidates",
        "pool_size": len(rows),
        "changes": changes,
        "before": before,
        "after": after,
    }


# ─── Internals ────────────────────────────────────────────────────────────


class _RowView:
    """Lightweight stand-in for DebugScoring so we can reuse the
    distribution-counting helper on in-memory mutated copies."""
    __slots__ = ("id", "candidate_uid", "final_rating", "confidence")

    def __init__(self, id: int, candidate_uid: str | None, final_rating: int | None, confidence: float | None):
        self.id = id
        self.candidate_uid = candidate_uid
        self.final_rating = final_rating
        self.confidence = confidence


def _read_latest_scoring_pool(position_uid: str) -> list[DebugScoring]:
    """One row per distinct candidate (the most recent score), with a
    non-null final_rating. Stable order: highest rating first, then
    highest confidence (so high-confidence anchors stay put when we
    re-bucket).
    """
    with db_session() as ses:
        all_rows = ses.execute(
            select(DebugScoring)
            .where(
                DebugScoring.position_uid == position_uid,
                DebugScoring.candidate_uid.is_not(None),
                DebugScoring.final_rating.is_not(None),
            )
            .order_by(desc(DebugScoring.id))
        ).scalars().all()

    seen: set[str] = set()
    latest: list[DebugScoring] = []
    for r in all_rows:
        uid = r.candidate_uid or ""
        if not uid or uid in seen:
            continue
        seen.add(uid)
        latest.append(r)

    # Sort: rating DESC, confidence DESC. High-confidence candidates anchor
    # the top of their bucket; low-confidence ones at the bottom of the
    # bucket are the first to be nudged out when re-bucketing.
    latest.sort(
        key=lambda r: (-(r.final_rating or 0), -(r.confidence or 0.0))
    )
    return latest


def _count_distribution(rows: list[Any]) -> dict[int, int]:
    out: dict[int, int] = {i: 0 for i in range(INTERNAL_MIN, INTERNAL_MAX + 1)}
    for r in rows:
        rating = r.final_rating
        if rating is None:
            continue
        rating = max(INTERNAL_MIN, min(INTERNAL_MAX, int(rating)))
        out[rating] = out.get(rating, 0) + 1
    return out


def _compute_target_counts(pool_size: int) -> dict[int, int]:
    """Apply TARGET_DISTRIBUTION percentages to pool_size, with rounding
    that keeps the total equal to pool_size.

    We use largest-remainder rounding: floor each percentage * pool_size,
    then distribute the leftover units to the buckets with the biggest
    fractional parts. Guarantees sum(target_counts.values()) == pool_size.
    """
    if pool_size <= 0:
        return {i: 0 for i in range(INTERNAL_MIN, INTERNAL_MAX + 1)}

    raw: dict[int, float] = {
        r: pool_size * pct for r, pct in TARGET_DISTRIBUTION.items()
    }
    floored: dict[int, int] = {r: int(v) for r, v in raw.items()}
    remainder = pool_size - sum(floored.values())

    if remainder > 0:
        # Sort buckets by fractional part descending; give +1 to top-`remainder`.
        order = sorted(raw.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)
        for rating, _ in order[:remainder]:
            floored[rating] += 1

    # Fill any missing keys with 0
    for r in range(INTERNAL_MIN, INTERNAL_MAX + 1):
        floored.setdefault(r, 0)
    return floored


# Thresholds for the gates that normalization must respect. Kept in sync
# with app.dimensions — duplicating the numbers here (rather than importing
# at module load) keeps the normalization module independent of dimensions
# in import order.
_LOCATION_GATE_THRESHOLD = 4
_DOMAIN_GATE_THRESHOLD = 5
_DOMAIN_CAP_RATING = 5


def _is_gate_locked(row: DebugScoring) -> tuple[bool, int | None]:
    """Decide whether `row` is locked to a specific rating by a hard/soft
    gate. Returns (locked, forced_rating).

    - location_match < 4   → locked at 1 (hard gate)
    - domain_match  < 5 and current rating ≤ 5 → locked at current rating
      (the domain soft cap; if compute_overall already capped them at 5,
      normalization must not push them above)
    """
    loc = getattr(row, "dim_location_match", None)
    if loc is not None and int(loc) < _LOCATION_GATE_THRESHOLD:
        return True, 1
    domain = getattr(row, "dim_domain_match", None)
    current = int(row.final_rating or 0)
    if (
        domain is not None
        and int(domain) < _DOMAIN_GATE_THRESHOLD
        and current <= _DOMAIN_CAP_RATING
    ):
        return True, current
    return False, None


def _compute_normalization_changes(rows: list[DebugScoring]) -> list[dict[str, Any]]:
    """Core algorithm. Returns a list of {debug_id, candidate_uid, old, new}.

    Gated candidates (location < 4 or domain < 5 with current rating ≤ 5)
    are EXCLUDED from the redistribution and kept at their gated rating.
    Their slot count is subtracted from the target distribution so the
    remaining (unfrozen) candidates fill the right share of buckets.

    Rows come in already sorted highest-first. We walk the unfrozen
    candidates from the top, handing out target ratings starting at 10
    and stepping down. For each, the proposed new rating is clamped to
    ±MAX_DELTA_INTERNAL of their current rating.
    """
    if not rows:
        return []

    # Partition: frozen (gated) vs free (eligible for normalization).
    frozen: list[tuple[DebugScoring, int]] = []   # (row, forced_rating)
    free: list[DebugScoring] = []
    for r in rows:
        locked, forced = _is_gate_locked(r)
        if locked and forced is not None:
            frozen.append((r, forced))
        else:
            free.append(r)

    # Build the target count map for the WHOLE pool, then subtract the
    # frozen candidates' slots so the redistribution only redistributes
    # the free ones.
    target_counts = _compute_target_counts(len(rows))
    for _, forced in frozen:
        if target_counts.get(forced, 0) > 0:
            target_counts[forced] -= 1
    # If freezing left a target with negative count (e.g., more frozen
    # at rating 1 than the target allows), clamp to 0. The free pool just
    # gets fewer rating-1 slots — that's correct.
    for r in target_counts:
        if target_counts[r] < 0:
            target_counts[r] = 0

    changes: list[dict[str, Any]] = []

    # 1) Force-fix any frozen row whose stored rating doesn't match the
    # gate (e.g., a future scoring change that briefly let a location-gated
    # row through). Belt-and-suspenders.
    for row, forced in frozen:
        old = int(row.final_rating or 0)
        if old != forced:
            changes.append({
                "debug_id": row.id,
                "candidate_uid": row.candidate_uid,
                "old": old,
                "new": forced,
            })

    # 2) Normalize the free pool.
    free_size = len(free)
    if free_size == 0:
        return changes
    # Re-cap target counts to the free pool size.
    target_sum = sum(target_counts.values())
    if target_sum > free_size:
        # Trim from the bottom-rated bucket downward until we match.
        overflow = target_sum - free_size
        for r in range(INTERNAL_MIN, INTERNAL_MAX + 1):
            if overflow <= 0:
                break
            take = min(target_counts[r], overflow)
            target_counts[r] -= take
            overflow -= take
    elif target_sum < free_size:
        # Distribute the deficit to the largest bucket.
        deficit = free_size - target_sum
        biggest = max(target_counts, key=lambda k: target_counts[k])
        target_counts[biggest] += deficit

    cursor = 0
    for target_rating in range(INTERNAL_MAX, INTERNAL_MIN - 1, -1):
        slots = target_counts.get(target_rating, 0)
        for _ in range(slots):
            if cursor >= free_size:
                break
            row = free[cursor]
            cursor += 1
            old = int(row.final_rating or 0)
            if old == target_rating:
                continue
            delta = target_rating - old
            if abs(delta) > MAX_DELTA_INTERNAL:
                delta = MAX_DELTA_INTERNAL if delta > 0 else -MAX_DELTA_INTERNAL
            new = max(INTERNAL_MIN, min(INTERNAL_MAX, old + delta))
            if new != old:
                changes.append({
                    "debug_id": row.id,
                    "candidate_uid": row.candidate_uid,
                    "old": old,
                    "new": new,
                })

    return changes


def _apply_changes(changes: list[dict[str, Any]]) -> None:
    if not changes:
        return
    with db_session() as ses:
        for ch in changes:
            ses.execute(
                update(DebugScoring)
                .where(DebugScoring.id == ch["debug_id"])
                .values(final_rating=ch["new"])
            )
        ses.commit()


__all__ = [
    "TARGET_DISTRIBUTION",
    "MAX_DELTA_INTERNAL",
    "DEFAULT_MIN_BATCH",
    "normalize_position_if_needed",
]
