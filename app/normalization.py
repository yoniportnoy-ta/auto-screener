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


def _compute_normalization_changes(rows: list[DebugScoring]) -> list[dict[str, Any]]:
    """Core algorithm. Returns a list of {debug_id, candidate_uid, old, new}.

    Rows come in already sorted highest-first. We walk them from the top,
    handing out target ratings starting at 10 and stepping down. For each
    candidate the proposed new rating is clamped to ±MAX_DELTA_INTERNAL
    of their current rating.
    """
    if not rows:
        return []

    pool_size = len(rows)
    target_counts = _compute_target_counts(pool_size)

    changes: list[dict[str, Any]] = []
    cursor = 0  # position in the sorted `rows` list
    for target_rating in range(INTERNAL_MAX, INTERNAL_MIN - 1, -1):
        slots = target_counts.get(target_rating, 0)
        for _ in range(slots):
            if cursor >= pool_size:
                break
            row = rows[cursor]
            cursor += 1
            old = int(row.final_rating or 0)
            if old == target_rating:
                continue  # already where it should be
            # Clamp to ±MAX_DELTA_INTERNAL.
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
