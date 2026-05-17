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

# Hard floor on the recruiter's 👍 minimum. Internal scale is 1-10, so a
# 👍 floor below 6 means the recruiter would tag rating-5-or-less candidates
# as "good fit" — which is nonsense on the recalibrated scale (5 = lean
# no, 6 = lean yes). 6 is the smallest defensible 👍 floor; the algorithm
# only clamps after-the-fact when the computed min is below 6.
MIN_THUMBS_UP_FLOOR = 6


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

    The global admin 👍 floor (when set) is applied on top so a 👍 bucket
    can never drop below the admin's policy bar even if a recruiter
    accidentally clicked something low.
    """
    with db_session() as ses:
        row = ses.scalar(
            select(RecruiterThreshold).where(
                RecruiterThreshold.recruiter_name == recruiter_name,
                RecruiterThreshold.position_uid == position_uid,
            )
        )
    up_min = row.thumbs_up_min_rating if row else None
    down_max = row.thumbs_down_max_rating if row else None

    # Stack the admin floor (if configured). The threshold algorithm
    # already clamps to MIN_THUMBS_UP_FLOOR in _recompute_threshold; the
    # admin floor is an additional, runtime-tunable lever for the team
    # lead to tighten things further without a code change.
    try:
        from .admin_settings import get_thumbs_up_floor
        admin_floor = get_thumbs_up_floor()
    except Exception as exc:  # noqa: BLE001
        log.info("admin floor lookup failed: %s", exc)
        admin_floor = None
    if admin_floor is not None:
        if up_min is None:
            up_min = admin_floor
        else:
            up_min = max(up_min, admin_floor)

    return Threshold(thumbs_up_min_rating=up_min, thumbs_down_max_rating=down_max)


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

        # Apply the 👍 floor: if the recruiter accidentally 👍'd a low-rated
        # candidate, don't let that pull the bar down into nonsense territory.
        # A 2/5 should never be auto-tagged as "good fit" on a fresh candidate.
        if thumbs_up_min is not None and thumbs_up_min < MIN_THUMBS_UP_FLOOR:
            thumbs_up_min = MIN_THUMBS_UP_FLOOR

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
    feedback_text: str | None = None,
) -> dict[str, Any]:
    """Persist a thumb click and recompute the recruiter's threshold.

    `feedback_text` is the optional free-form reason the recruiter typed —
    e.g. "Too senior" / "Great match on the data stack". Stored on the
    verdict row for later review. We deliberately don't try to map it to a
    1-5 Feedback row here: the existing Feedback flow expects a numeric
    rating, and the thumb-trichotomy doesn't map cleanly enough that the
    AI's scoring rubric should learn from it without supervision.

    Returns the updated threshold + the new agreement % + the round number
    this verdict belongs to, so the UI can update headers in one round-trip.
    """
    threshold_before = get_threshold(recruiter_name, position_uid)
    bucket_at_time = bucket_for(ai_rating, threshold_before)
    # ❓ ("not sure") is deliberately excluded from the agreement metric —
    # it isn't really "the AI was wrong", it's "the recruiter didn't pick
    # a side". Counting it as disagreement when the AI said up/down would
    # drag the headline number down for reasons unrelated to AI quality.
    if verdict == "question":
        agreed = None
    elif bucket_at_time is None:
        agreed = None
    else:
        agreed = (bucket_at_time == verdict)
    round_num = _current_round_num(recruiter_name, position_uid)

    # Trim conservatively. The column is TEXT so there's no DB cap, but
    # nothing useful tends to live past ~2 KB and we don't want a recruiter
    # paste-bombing the prompt later.
    cleaned_text = (feedback_text or "").strip()[:2000] or None

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
            feedback_text=cleaned_text,
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


def _read_scored_pool(
    position_uid: str,
    *,
    exclude_uids: set[str],
) -> tuple[list[DebugScoring], set[str]]:
    """Read DebugScoring rows for this position and split them into
    (eligible_for_calibration, all_scored_uids).

    `eligible` is dedup'd to the most-recent row per candidate, with
    `exclude_uids` (typically: already-verdicted uids) stripped out.
    `all_scored_uids` is the unfiltered set of every candidate who has any
    scoring row at all — used for empty-state messaging.
    """
    with db_session() as ses:
        rows = ses.execute(
            select(DebugScoring).where(
                DebugScoring.position_uid == position_uid,
                DebugScoring.candidate_uid.is_not(None),
            ).order_by(desc(DebugScoring.id))
        ).scalars().all()

    all_scored: set[str] = set()
    eligible: list[DebugScoring] = []
    seen: set[str] = set()
    for r in rows:
        uid = r.candidate_uid or ""
        if not uid:
            continue
        all_scored.add(uid)
        if uid in seen or uid in exclude_uids:
            continue
        seen.add(uid)
        eligible.append(r)
    return eligible, all_scored


def _lazy_score_to_fill(
    position_uid: str,
    *,
    needed: int,
    exclude_uids: set[str],
) -> int:
    """Score up to `needed` unscored candidates for this position right now,
    so the calibration queue always returns a full batch.

    Synchronous — the recruiter sees a longer spinner on the first call, but
    every subsequent queue request hits the cache. Best-effort: any error on
    an individual candidate is logged and skipped; we never fail the whole
    queue load because of one bad CV.

    Returns the number actually scored (may be 0 if Comeet has nothing left
    to offer in the active CV-screening step).
    """
    if needed <= 0:
        return 0

    # Lazy import: scan.py is heavyweight and pulls in Comeet + Claude. The
    # only callers of this helper are queue requests, so paying that import
    # cost here keeps app boot fast.
    try:
        from .scan import (
            begin_scan_batch,
            finish_scan_batch,
            score_candidate_in_session,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("calibration: scan module import failed: %s", exc)
        return 0

    try:
        batch = begin_scan_batch(position_uid)
    except Exception as exc:  # noqa: BLE001
        log.info("calibration: begin_scan_batch failed for %s: %s", position_uid, exc)
        return 0

    if batch.empty or not batch.uids:
        return 0

    # `begin_scan_batch` may return up to SCREENER_MAX_PER_RUN UIDs; we only
    # need `needed`. Trim to keep the spinner time bounded.
    uids_to_score = [u for u in batch.uids if u not in exclude_uids][:needed]
    if not uids_to_score:
        # Nothing useful — close out the empty session so we don't leak a
        # row in Postgres.
        try:
            finish_scan_batch(batch.session_id, [])
        except Exception:  # noqa: BLE001
            pass
        return 0

    scored = 0
    for uid in uids_to_score:
        try:
            summary = score_candidate_in_session(batch.session_id, uid)
        except Exception as exc:  # noqa: BLE001
            log.warning("calibration: lazy score failed for %s: %s", uid, exc)
            continue
        if not getattr(summary, "error", None):
            scored += 1

    try:
        finish_scan_batch(batch.session_id, uids_to_score)
    except Exception as exc:  # noqa: BLE001
        log.info("calibration: finish_scan_batch failed for %s: %s", position_uid, exc)

    return scored


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

    If we don't have enough scored candidates to fill the batch, we lazily
    score fresh ones from Comeet *right now* so the recruiter never sees a
    half-empty batch. First call on a thin position can take a minute or
    two; subsequent calls hit the cache.

    The per-candidate `bucket` field is included so the UI *can* show how
    the AI would currently classify each one given the recruiter's learned
    threshold, but it's a display hint, not a queue filter. While the
    recruiter is still uncalibrated, that bucket field is null.

    Returns:
        {
          "candidates": [...],   # up to N profiles, top-rated first
          "isCalibrated": bool,  # whether the recruiter has verdicted yet
          "totalScored": int,    # distinct candidates with any scoring row
          "totalVerdicted": int, # this recruiter's verdicts so far
          "remainingInPool": int,# extras beyond the returned batch
          "scoredThisCall": int, # how many fresh candidates we scored just now
        }
    """
    threshold = get_threshold(recruiter_name, position_uid)
    already = get_already_verdicted_uids(recruiter_name, position_uid)

    eligible, all_scored = _read_scored_pool(position_uid, exclude_uids=already)

    # Top up the pool with fresh scoring if we're below `n`. The recruiter's
    # expectation is "always 5 per round" — we honor that by paying the
    # scoring tokens just-in-time instead of waiting on a cron.
    scored_this_call = 0
    if len(eligible) < n:
        needed = n - len(eligible)
        exclude = already | {r.candidate_uid for r in eligible if r.candidate_uid}
        scored_this_call = _lazy_score_to_fill(
            position_uid, needed=needed, exclude_uids=exclude,
        )
        if scored_this_call > 0:
            # Re-read to pull the fresh DebugScoring rows into the pool.
            eligible, all_scored = _read_scored_pool(
                position_uid, exclude_uids=already
            )

    eligible.sort(
        key=lambda r: (
            -(r.final_rating or 0),
            -(r.confidence or 0.0),
        )
    )

    pool = eligible[:n]
    # Look up cached enrichment profile_urls for any rows that don't have a
    # profile_url on their scoring row (older rows from before the column
    # existed). One bulk query keeps this O(1) per queue request.
    enrichment_urls: dict[str, str] = {}
    missing = [r.candidate_uid for r in pool if r.candidate_uid and not getattr(r, "profile_url", None)]
    if missing:
        with db_session() as ses:
            from .models import CandidateEnrichment as _CE  # local to avoid cycle on first import
            enr_rows = ses.execute(
                select(_CE.candidate_uid, _CE.profile_url).where(
                    _CE.candidate_uid.in_(missing),
                    _CE.profile_url.is_not(None),
                )
            ).all()
        enrichment_urls = {uid: url for uid, url in enr_rows if uid and url}

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
            "profileUrl": (
                getattr(r, "profile_url", None)
                or enrichment_urls.get(r.candidate_uid or "")
                or None
            ),
            # Per-dimension sub-scores. Null for legacy rows scored before
            # the dimension migration — frontend hides the breakdown when
            # all six are null. location_match is included for display +
            # gate-state lookup; it isn't weighted into the overall.
            "dimensions": {
                "domain_match": getattr(r, "dim_domain_match", None),
                "company_tier": getattr(r, "dim_company_tier", None),
                "career_progression": getattr(r, "dim_career_progression", None),
                "location_match": getattr(r, "dim_location_match", None),
                "university_tier": getattr(r, "dim_university_tier", None),
                "achievements": getattr(r, "dim_achievements", None),
            },
            # Convenience flags for the UI:
            # - locationGateFailed: location gate (auto-rated 1).
            # - domainCapApplied: domain mismatch cap (overall held at 5).
            "locationGateFailed": (
                getattr(r, "dim_location_match", None) is not None
                and getattr(r, "dim_location_match") < 4  # LOCATION_GATE_THRESHOLD
            ),
            "domainCapApplied": (
                getattr(r, "dim_domain_match", None) is not None
                and getattr(r, "dim_domain_match") < 5  # DOMAIN_GATE_THRESHOLD
                and (r.final_rating or 0) == 5           # DOMAIN_CAP_RATING
            ),
        }
        for r in pool
    ]
    # Context for the empty-queue UX: distinguish between "truly calibrated"
    # and "ran out of scored candidates after only a couple of verdicts" so
    # the UI can show the right message + offer a "scan more" CTA.
    return {
        "candidates": candidates,
        "isCalibrated": threshold.has_calibration,
        "totalScored": len(all_scored),
        "totalVerdicted": len(already),
        "remainingInPool": max(0, len(eligible) - len(pool)),
        "scoredThisCall": scored_this_call,
    }


def get_threshold_for_tagging(position_uid: str) -> int | None:
    """Position-level threshold used to gate auto-tagging in Comeet.

    Returns None if no recruiter has calibrated for this position yet —
    callers should treat None as "don't auto-tag, we don't know the bar".

    When multiple recruiters have calibrated the same position, we pick the
    *strictest* thumbs_up_min (max) so the auto-tag only fires on
    candidates strong enough that the most demanding recruiter on the
    position would 👍. Conservative-by-default avoids polluting Comeet
    with tags the picky recruiter disagrees with.
    """
    if not position_uid:
        return None
    with db_session() as ses:
        rows = ses.scalars(
            select(RecruiterThreshold.thumbs_up_min_rating).where(
                RecruiterThreshold.position_uid == position_uid,
                RecruiterThreshold.thumbs_up_min_rating.is_not(None),
            )
        ).all()
    if not rows:
        return None
    strictest = max(rows)
    # Stack the admin floor on top so the tag bar can be tightened
    # across all positions without per-position recalibration.
    try:
        from .admin_settings import get_thumbs_up_floor
        admin_floor = get_thumbs_up_floor()
    except Exception as exc:  # noqa: BLE001
        log.info("admin floor lookup failed (tagging): %s", exc)
        admin_floor = None
    if admin_floor is not None:
        strictest = max(strictest, admin_floor)
    return strictest


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
    "get_threshold_for_tagging",
    "record_verdict",
    "get_agreement",
    "get_calibration_queue",
    "get_session_state",
]
