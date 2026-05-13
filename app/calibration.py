"""Per-(recruiter, position) calibration logic.

The recruiter UI shows candidates as 👍 / 👎 / ❓ instead of 1-5 ratings.
Where the dividing lines fall is *per recruiter and per position* — they
learn from each recruiter's thumb clicks.

Key pieces:
  - `bucket_for(rating, threshold)` — maps an AI rating to "up" / "down" / "question"
  - `record_verdict(...)` — saves a thumb click, updates the recruiter's threshold
  - `get_threshold(recruiter, position_uid)` — returns the recruiter's current cutoffs
  - `get_calibration_queue(recruiter, position_uid, n)` — next N currently-👍
    candidates that this recruiter hasn't verdicted yet, ordered most-confident first
  - `get_agreement(recruiter, position_uid)` — % of verdicts so far where the
    recruiter's thumb matched what the AI would have bucketed at that moment

Threshold logic is intentionally simple (percentile of the recruiter's own
past verdicts). It's transparent — when a recruiter asks "why is this a 👍?"
the answer is "you 👍'd a rating-4 once, so all rating-4 and rating-5 are 👍
for you on this role."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import desc, func, select

from .db import db_session
from .models import (
    CalibrationVerdict,
    DebugScoring,
    RecruiterThreshold,
)

log = logging.getLogger(__name__)

Verdict = Literal["up", "down", "question"]

# Defaults used when a recruiter hasn't given any verdicts yet for a
# position. Picked to roughly match the previous AI-rating-to-tag mapping
# (4-5 = top, 1-2 = reject) so the recruiter's first calibration round
# starts from a sensible baseline.
DEFAULT_THUMBS_UP_MIN = 4
DEFAULT_THUMBS_DOWN_MAX = 2


@dataclass
class Threshold:
    """A recruiter's 👍/👎 cutoffs for a single position."""
    thumbs_up_min_rating: int
    thumbs_down_max_rating: int
    has_calibration: bool  # False until the recruiter has verdicted at least once

    def to_dict(self) -> dict[str, Any]:
        return {
            "thumbsUpMinRating": self.thumbs_up_min_rating,
            "thumbsDownMaxRating": self.thumbs_down_max_rating,
            "hasCalibration": self.has_calibration,
        }


def bucket_for(rating: int | None, threshold: Threshold) -> Verdict | None:
    """Map an AI rating to a verdict bucket using the recruiter's cutoffs.

    Returns None when there's no rating to bucket (unscored candidate).
    """
    if rating is None:
        return None
    if rating >= threshold.thumbs_up_min_rating:
        return "up"
    if rating <= threshold.thumbs_down_max_rating:
        return "down"
    return "question"


def get_threshold(recruiter_name: str, position_uid: str) -> Threshold:
    """Read the recruiter's current threshold, or return defaults.

    Defaults apply until the recruiter has given at least one verdict; once
    they've thumb-clicked anything, the stored row takes over.
    """
    with db_session() as ses:
        row = ses.scalar(
            select(RecruiterThreshold).where(
                RecruiterThreshold.recruiter_name == recruiter_name,
                RecruiterThreshold.position_uid == position_uid,
            )
        )
        if not row:
            return Threshold(
                thumbs_up_min_rating=DEFAULT_THUMBS_UP_MIN,
                thumbs_down_max_rating=DEFAULT_THUMBS_DOWN_MAX,
                has_calibration=False,
            )
        # NULL columns fall back to defaults — useful when the recruiter has
        # 👎'd things but never 👍'd (or vice versa) yet.
        return Threshold(
            thumbs_up_min_rating=(row.thumbs_up_min_rating
                                  if row.thumbs_up_min_rating is not None
                                  else DEFAULT_THUMBS_UP_MIN),
            thumbs_down_max_rating=(row.thumbs_down_max_rating
                                    if row.thumbs_down_max_rating is not None
                                    else DEFAULT_THUMBS_DOWN_MAX),
            has_calibration=True,
        )


def _recompute_threshold(
    recruiter_name: str,
    position_uid: str,
) -> Threshold:
    """Look at all the recruiter's past verdicts for this position and
    recompute the cutoffs from scratch. Percentile-based:

      thumbs_up_min  = min(ai_rating where verdict='up')
      thumbs_down_max = max(ai_rating where verdict='down')

    If those ranges collide (e.g. the recruiter 👍'd a 3 and 👎'd a 4),
    we keep them as-is — the in-between rating-3 will hit 'up' branch first
    in `bucket_for` because we check up >= first. That's the recruiter's
    chosen weirdness; we surface it without trying to fix it.

    ❓ verdicts don't affect either cutoff — they're literally "I don't know".
    """
    with db_session() as ses:
        ups = ses.scalars(
            select(CalibrationVerdict.ai_rating).where(
                CalibrationVerdict.recruiter_name == recruiter_name,
                CalibrationVerdict.position_uid == position_uid,
                CalibrationVerdict.verdict == "up",
                CalibrationVerdict.ai_rating.is_not(None),
            )
        ).all()
        downs = ses.scalars(
            select(CalibrationVerdict.ai_rating).where(
                CalibrationVerdict.recruiter_name == recruiter_name,
                CalibrationVerdict.position_uid == position_uid,
                CalibrationVerdict.verdict == "down",
                CalibrationVerdict.ai_rating.is_not(None),
            )
        ).all()

        thumbs_up_min = min(ups) if ups else None
        thumbs_down_max = max(downs) if downs else None

        row = ses.scalar(
            select(RecruiterThreshold).where(
                RecruiterThreshold.recruiter_name == recruiter_name,
                RecruiterThreshold.position_uid == position_uid,
            )
        )
        if row is None:
            row = RecruiterThreshold(
                recruiter_name=recruiter_name,
                position_uid=position_uid,
                thumbs_up_min_rating=thumbs_up_min,
                thumbs_down_max_rating=thumbs_down_max,
            )
            ses.add(row)
        else:
            row.thumbs_up_min_rating = thumbs_up_min
            row.thumbs_down_max_rating = thumbs_down_max
        ses.commit()

    return get_threshold(recruiter_name, position_uid)


def _current_round_num(recruiter_name: str, position_uid: str) -> int:
    """1-indexed round number for the next verdict.

    Round N runs from verdict (N-1)*5 + 1 through N*5 (5 verdicts per round).
    With 0 verdicts in the books, the next click is round 1; with 5, it's
    round 2; with 6, still round 2; etc.
    """
    with db_session() as ses:
        count = ses.scalar(
            select(func.count(CalibrationVerdict.id)).where(
                CalibrationVerdict.recruiter_name == recruiter_name,
                CalibrationVerdict.position_uid == position_uid,
            )
        ) or 0
    return (count // 5) + 1


def record_verdict(
    recruiter_name: str,
    position_uid: str,
    candidate_uid: str,
    verdict: Verdict,
    ai_rating: int | None,
    ai_confidence: float | None,
) -> dict[str, Any]:
    """Persist a thumb click and recompute the recruiter's threshold.

    Returns the updated threshold + the new agreement % + the round number
    this verdict belongs to, so the UI can update headers in one round-trip.
    """
    threshold_before = get_threshold(recruiter_name, position_uid)
    bucket_at_time = bucket_for(ai_rating, threshold_before)
    agreed = bucket_at_time == verdict if bucket_at_time is not None else None
    round_num = _current_round_num(recruiter_name, position_uid)

    with db_session() as ses:
        ses.add(CalibrationVerdict(
            recruiter_name=recruiter_name,
            position_uid=position_uid,
            candidate_uid=candidate_uid,
            verdict=verdict,
            ai_rating=ai_rating,
            ai_confidence=ai_confidence,
            agreed_at_time=agreed,
            round_num=round_num,
        ))
        ses.commit()

    new_threshold = _recompute_threshold(recruiter_name, position_uid)
    agreement = get_agreement(recruiter_name, position_uid)

    return {
        "threshold": new_threshold.to_dict(),
        "agreement": agreement,
        "roundNum": round_num,
        "agreed": agreed,
    }


def get_agreement(recruiter_name: str, position_uid: str) -> dict[str, Any]:
    """Return overall agreement % + per-round breakdown.

    "Agreement" = of all verdicts the recruiter has given on this position,
    what fraction were already in the bucket the AI would have placed them
    in *at the moment the recruiter clicked* (snapshot, not retroactive).

    The per-round breakdown lets the UI show "round 1: 40% → round 2: 80%"
    which is the whole calibration narrative.
    """
    with db_session() as ses:
        rows = ses.execute(
            select(
                CalibrationVerdict.round_num,
                CalibrationVerdict.agreed_at_time,
            ).where(
                CalibrationVerdict.recruiter_name == recruiter_name,
                CalibrationVerdict.position_uid == position_uid,
            )
        ).all()

    if not rows:
        return {"overall": None, "verdictCount": 0, "byRound": []}

    total = 0
    matched = 0
    by_round_raw: dict[int, list[bool]] = {}
    for round_num, agreed in rows:
        if agreed is None:
            continue
        total += 1
        if agreed:
            matched += 1
        if round_num is not None:
            by_round_raw.setdefault(round_num, []).append(bool(agreed))

    by_round = [
        {
            "roundNum": r,
            "agreement": (sum(a) / len(a)) if a else None,
            "n": len(a),
        }
        for r, a in sorted(by_round_raw.items())
    ]
    return {
        "overall": (matched / total) if total else None,
        "verdictCount": len(rows),
        "byRound": by_round,
    }


def get_already_verdicted_uids(recruiter_name: str, position_uid: str) -> set[str]:
    """Candidate uids this recruiter has already given a verdict on — to
    filter out of the next batch."""
    with db_session() as ses:
        rows = ses.scalars(
            select(CalibrationVerdict.candidate_uid).where(
                CalibrationVerdict.recruiter_name == recruiter_name,
                CalibrationVerdict.position_uid == position_uid,
            )
        ).all()
    return set(rows)


def get_calibration_queue(
    recruiter_name: str,
    position_uid: str,
    n: int = 5,
) -> list[dict[str, Any]]:
    """Return the next batch of candidates for this recruiter to verdict.

    Logic:
      1. Pull all DebugScoring rows for this position (latest per candidate).
      2. Bucket each one with the recruiter's current threshold.
      3. Filter to bucket='up' AND not already verdicted by this recruiter.
      4. Sort by AI rating DESC, then confidence DESC.
      5. Return the top N as candidate-profile dicts.

    Returns [] when calibration is exhausted (no more 👍 candidates left
    that this recruiter hasn't already reviewed).
    """
    threshold = get_threshold(recruiter_name, position_uid)
    already = get_already_verdicted_uids(recruiter_name, position_uid)

    with db_session() as ses:
        # Latest scoring row per candidate. The DebugScoring table can have
        # multiple rows per candidate (one per scan/rescore); we want the
        # most recent for the current AI judgment.
        rows = ses.execute(
            select(DebugScoring).where(
                DebugScoring.position_uid == position_uid,
                DebugScoring.candidate_uid.is_not(None),
            ).order_by(desc(DebugScoring.id))
        ).scalars().all()

    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for r in rows:
        uid = r.candidate_uid or ""
        if not uid or uid in seen or uid in already:
            continue
        seen.add(uid)
        bucket = bucket_for(r.final_rating, threshold)
        if bucket != "up":
            continue
        candidates.append({
            "candidateUid": uid,
            "candidateName": r.candidate_name or "",
            "positionName": r.position_name or "",
            "rating": r.final_rating,
            "confidence": r.confidence,
            "summary": r.summary or "",
            "strengths": r.strengths_json or [],
            "gaps": r.gaps_json or [],
            "scoredAt": r.timestamp.isoformat() if r.timestamp else None,
            "bucket": bucket,
        })
        if len(candidates) >= n:
            break

    # Sort already-filtered list by rating DESC, then confidence DESC.
    candidates.sort(
        key=lambda c: (
            -(c["rating"] or 0),
            -(c["confidence"] or 0.0),
        )
    )
    return candidates


def get_session_state(recruiter_name: str, position_uid: str) -> dict[str, Any]:
    """Aggregate snapshot of the recruiter's calibration progress for this
    position — threshold, agreement, total verdicts, current round.

    Used by the frontend on every render of the calibration view so it
    always has fresh numbers in the sidebar.
    """
    threshold = get_threshold(recruiter_name, position_uid)
    agreement = get_agreement(recruiter_name, position_uid)
    round_num = _current_round_num(recruiter_name, position_uid)
    return {
        "threshold": threshold.to_dict(),
        "agreement": agreement,
        "roundNum": round_num,
    }


__all__ = [
    "Threshold",
    "bucket_for",
    "get_threshold",
    "record_verdict",
    "get_agreement",
    "get_calibration_queue",
    "get_session_state",
    "DEFAULT_THUMBS_UP_MIN",
    "DEFAULT_THUMBS_DOWN_MAX",
]
