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


@dataclass
class Threshold:
    """A recruiter's 👍/👎 cutoffs for a single position.

    Both bounds are optional and start as None: the threshold emerges
    entirely from this recruiter's verdicts on this position. There's no
    hardcoded "rating-4 means 👍" — that turned out to be the exact source
    of recruiter confusion ("what's the threshold?"). The bucket is
    explicitly *unknown* until the recruiter has clicked anything.
    """
    thumbs_up_min_rating: int | None
    thumbs_down_max_rating: int | None

    @property
    def has_calibration(self) -> bool:
        return (self.thumbs_up_min_rating is not None
                or self.thumbs_down_max_rating is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thumbsUpMinRating": self.thumbs_up_min_rating,
            "thumbsDownMaxRating": self.thumbs_down_max_rating,
            "hasCalibration": self.has_calibration,
        }


def bucket_for(rating: int | None, threshold: Threshold) -> Verdict | None:
    """Map an AI rating to a verdict bucket using the recruiter's cutoffs.

    Returns None when:
      - the candidate has no rating yet, or
      - the recruiter hasn't given a relevant verdict yet (so we genuinely
        don't know which bucket they'd put this candidate in).

    A partial calibration (only 👍 given, never 👎) still works: anything
    at-or-above the 👍 minimum is "up"; everything else is "question" until
    the recruiter establishes a 👎 ceiling.
    """
    if rating is None:
        return None
    up_min = threshold.thumbs_up_min_rating
    down_max = threshold.thumbs_down_max_rating
    if up_min is None and down_max is None:
        return None  # uncalibrated
    if up_min is not None and rating >= up_min:
        return "up"
    if down_max is not None and rating <= down_max:
        return "down"
    return "question"


def get_threshold(recruiter_name: str, position_uid: str) -> Threshold:
    """Read the recruiter's current threshold.

    Both bounds start as None (no calibration). They become concrete as
    the recruiter gives 👍/👎 verdicts: the 👍 minimum is the lowest rating
    they've ever 👍'd; the 👎 maximum is the highest rating they've ever 👎'd.
    """
    with db_session() as ses:
        row = ses.scalar(
            select(RecruiterThreshold).where(
                RecruiterThreshold.recruiter_name == recruiter_name,
                RecruiterThreshold.position_uid == position_uid,
            )
        )
        if not row:
            return Threshold(thumbs_up_min_rating=None, thumbs_down_max_rating=None)
        return Threshold(
            thumbs_up_min_rating=row.thumbs_up_min_rating,
            thumbs_down_max_rating=row.thumbs_down_max_rating,
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
) -> dict[str, Any]:
    """Return the next batch of candidates for this recruiter to verdict.

    No bucket filtering. We always rank by AI score and hand the recruiter
    the top N they haven't already verdicted. The thumbs they click then
    teach this position's threshold — there's no hardcoded "rating-4 is 👍"
    rule (that was the source of "but what's the threshold?" confusion
    in the first place).

    The per-candidate `bucket` field is included so the UI *can* show how
    the AI would currently classify each one given the recruiter's learned
    threshold, but it's a display hint, not a queue filter. While the
    recruiter is still uncalibrated, that bucket field is null.

    Returns:
        {
          "candidates": [...],   # up to N profiles, top-rated first
          "isCalibrated": bool,  # whether the recruiter has verdicted yet
        }
    """
    threshold = get_threshold(recruiter_name, position_uid)
    already = get_already_verdicted_uids(recruiter_name, position_uid)

    with db_session() as ses:
        # Latest scoring row per candidate. DebugScoring may have several
        # rows per candidate (one per scan/rescore); we want the most recent.
        rows = ses.execute(
            select(DebugScoring).where(
                DebugScoring.position_uid == position_uid,
                DebugScoring.candidate_uid.is_not(None),
            ).order_by(desc(DebugScoring.id))
        ).scalars().all()

    seen: set[str] = set()
    eligible: list[DebugScoring] = []
    for r in rows:
        uid = r.candidate_uid or ""
        if not uid or uid in seen or uid in already:
            continue
        seen.add(uid)
        eligible.append(r)

    eligible.sort(
        key=lambda r: (
            -(r.final_rating or 0),
            -(r.confidence or 0.0),
        )
    )

    pool = eligible[:n]
    candidates = [
        {
            "candidateUid": r.candidate_uid,
            "candidateName": r.candidate_name or "",
            "positionName": r.position_name or "",
            "rating": r.final_rating,
            "confidence": r.confidence,
            "summary": r.summary or "",
            "strengths": r.strengths_json or [],
            "gaps": r.gaps_json or [],
            "scoredAt": r.timestamp.isoformat() if r.timestamp else None,
            "bucket": bucket_for(r.final_rating, threshold),
        }
        for r in pool
    ]
    return {
        "candidates": candidates,
        "isCalibrated": threshold.has_calibration,
    }


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
]
