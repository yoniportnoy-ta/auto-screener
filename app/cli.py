"""CLI entrypoints used by the Render cron job (and for one-off ops).

Usage:
    python -m app.cli scan-all              # iterate all open positions, score new candidates
    python -m app.cli refresh-rubrics       # regenerate learned rubrics for all classes
    python -m app.cli refresh-comeet-session  # force-relogin to app.comeet.co
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .logging_config import configure_logging

log = logging.getLogger(__name__)


async def cmd_scan_all() -> int:
    """Hourly cron entrypoint. Walks open positions, scores new candidates, applies rating tags."""
    from .automation import run_autoscan

    result = run_autoscan()
    if result.error:
        log.error("scan-all: %s", result.error)
        return 1
    log.info(
        "scan-all done: positions=%d candidates_scored=%d tags_applied=%d duration=%.1fs",
        result.positions_scanned, result.candidates_scored, result.tags_applied, result.duration_s,
    )
    return 0


async def cmd_reset_and_rescore() -> int:
    """One-shot: wipe all feedback/thresholds/verdicts AND rescore every
    candidate with the current prompt.

    Equivalent to running:
        python -m app.cli reset-for-launch
        python -m app.cli rescore-all

    Used during prompt iteration: change the prompt, push, then run this
    to clear stale data and refresh every score so you can see the new
    distribution immediately instead of waiting for the next prewarm.

    Long-running: same cost as rescore-all (~10-30 min, $1-$5 in tokens
    depending on pool size).
    """
    log.info("reset-and-rescore: step 1/2 — clearing feedback/thresholds/verdicts")
    rc = await cmd_reset_for_launch()
    if rc != 0:
        log.error("reset-and-rescore: reset step failed (rc=%d), aborting rescore", rc)
        return rc
    log.info("reset-and-rescore: step 2/2 — rescoring all open positions")
    return await cmd_rescore_all(None)


async def cmd_rescore_all(position_uid: str | None = None) -> int:
    """Re-score previously-scored candidates with the *current* prompt.

    Usage:
        python -m app.cli rescore-all                 # every open position
        python -m app.cli rescore-all DC.E45          # one position only

    Why: when the scoring prompt changes (new pre-rating checklist,
    company tier reference, location signal, etc.) every existing
    DebugScoring row is stale. This walks every open position, finds
    candidates we've scored before who are still in the CV-screening
    step, and re-runs the pipeline so their rating reflects the new
    prompt. Skipped candidates: anyone who moved past CV screening
    (in interviews / hired / rejected) — no point spending tokens
    re-rating someone already decided.

    Long-running: ~5-30 s per candidate × N candidates × M positions.
    Print progress every 10 candidates so the recruiter watching the
    Render shell knows we're alive.
    """
    from sqlalchemy import select as _select
    from .comeet_client import ComeetClient, candidate_in_allowed_step
    from .db import db_session
    from .models import DebugScoring
    from .scan import _resolve_numeric_position_uid, score_one_candidate_now

    # Build the list of positions to walk.
    target_uid = (position_uid or "").strip()
    if target_uid and target_uid.isdigit():
        target_uid = _resolve_numeric_position_uid(target_uid) or target_uid

    if target_uid:
        positions_to_walk = [{"uid": target_uid, "name": target_uid}]
    else:
        try:
            with ComeetClient() as client:
                positions_to_walk = client.list_open_positions()
        except Exception as exc:  # noqa: BLE001
            log.error("rescore-all: list_open_positions failed: %s", exc)
            return 1

    total_rescored = 0
    total_skipped = 0
    total_errors = 0

    for pos in positions_to_walk:
        pos_uid = str(pos.get("uid") or "")
        pos_name = str(pos.get("name") or pos_uid)
        if not pos_uid:
            continue

        # Find previously-scored candidates for this position.
        with db_session() as ses:
            scored_uids = set(ses.scalars(
                _select(DebugScoring.candidate_uid)
                .where(DebugScoring.position_uid == pos_uid)
                .distinct()
            ).all()) - {None, ""}

        if not scored_uids:
            log.info("rescore-all: %s — no scored candidates, skipping", pos_name)
            continue

        # Filter to ones still in the allowed CV-screening step.
        try:
            with ComeetClient() as client:
                current = client.list_candidates_for_position(pos_uid)
        except Exception as exc:  # noqa: BLE001
            log.warning("rescore-all: %s — Comeet fetch failed: %s", pos_name, exc)
            total_errors += 1
            continue

        eligible = [
            str(c["uid"]) for c in current
            if c.get("uid")
            and str(c["uid"]) in scored_uids
            and candidate_in_allowed_step(c)
        ]
        skipped = len(scored_uids) - len(eligible)
        log.info(
            "rescore-all: %s — %d eligible, %d skipped (moved past CV step)",
            pos_name, len(eligible), skipped,
        )
        total_skipped += skipped

        scored_this_position = 0
        for i, uid in enumerate(eligible, start=1):
            try:
                score_one_candidate_now(pos_uid, candidate_uid=uid)
                total_rescored += 1
                scored_this_position += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("rescore-all: %s/%s failed: %s", pos_name, uid, exc)
                total_errors += 1
            if i % 10 == 0:
                log.info("  ... %d/%d on %s", i, len(eligible), pos_name)

        # Per-position normalization: spread the distribution out when we
        # rescored a sizeable batch. No-op for small batches.
        if scored_this_position > 0:
            try:
                from .normalization import normalize_position_if_needed
                norm = normalize_position_if_needed(
                    pos_uid, batch_scored=scored_this_position,
                )
                if norm.get("ran"):
                    log.info(
                        "rescore-all: normalized %s — %s; before=%s after=%s",
                        pos_name, norm.get("reason"),
                        norm.get("before"), norm.get("after"),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("rescore-all: normalization failed for %s: %s", pos_name, exc)

    log.info(
        "rescore-all done: rescored=%d skipped=%d errors=%d (positions=%d)",
        total_rescored, total_skipped, total_errors, len(positions_to_walk),
    )
    return 0


async def cmd_reset_for_launch() -> int:
    """One-shot pre-launch cleanup. Clears the four tables that contain
    pre-launch noise and leaves the expensive/historical ones alone:

      WIPED:
        - feedback                (old 1-5 ratings + notes — pre-calibration era)
        - recruiter_thresholds    (so the new MIN_THUMBS_UP_FLOOR is enforced)
        - calibration_verdicts    (pre-launch test thumbs from internal QA)
        - learned_rubrics         (synthesised FROM feedback above — leaving them
                                   in place means the scoring prompt still treats
                                   the stale "rate 4-5 when X" patterns as
                                   authoritative, overruling the new strict
                                   pre-rating checklist)

      KEPT:
        - debug_scoring           (the scoring pool — Claude tokens already paid)
        - applied_tags            (bookkeeping of tags pushed to Comeet)

    Idempotent — safe to re-run if something goes wrong mid-launch.
    """
    from sqlalchemy import delete
    from .db import db_session
    from .models import (
        CalibrationVerdict,
        Feedback,
        LearnedRubric,
        RecruiterThreshold,
    )

    counts: dict[str, int] = {}
    with db_session() as ses:
        for label, model in [
            ("feedback", Feedback),
            ("recruiter_thresholds", RecruiterThreshold),
            ("calibration_verdicts", CalibrationVerdict),
            ("learned_rubrics", LearnedRubric),
        ]:
            res = ses.execute(delete(model))
            counts[label] = res.rowcount or 0
        ses.commit()

    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    log.info("reset-for-launch done: %s", summary)
    return 0


async def cmd_reset_thresholds(position_uid: str | None = None) -> int:
    """Wipe RecruiterThreshold rows so calibration restarts from scratch.

    Usage:
        python -m app.cli reset-thresholds                # all positions
        python -m app.cli reset-thresholds DC.E45         # one position only

    Why this is sometimes needed: the threshold algorithm is monotonic —
    once a recruiter has 👍'd a low-rated candidate, `thumbs_up_min` stays
    at that rating forever, producing absurd buckets ("AI says 👍 because
    2/5 ≥ your 👍 floor of 2"). Resetting lets the recruiter start over
    with the new floor logic in place. Verdicts in calibration_verdicts
    are kept — they're history and feed the scoring prompt.
    """
    from sqlalchemy import delete
    from .db import db_session
    from .models import RecruiterThreshold

    pos = (position_uid or "").strip()
    with db_session() as ses:
        stmt = delete(RecruiterThreshold)
        if pos:
            stmt = stmt.where(RecruiterThreshold.position_uid == pos)
        result = ses.execute(stmt)
        deleted = result.rowcount or 0
        ses.commit()
    log.info(
        "reset-thresholds done: deleted %d row(s)%s",
        deleted,
        f" for position {pos}" if pos else " (all positions)",
    )
    return 0


async def cmd_prewarm_all() -> int:
    """Pre-score the next N candidates across every open position.

    Wired to the hourly `auto-screener-prewarm` cron. Unlike scan-all (which
    only walks opted-in positions), this runs against *every* open position
    so calibration sessions opened on any position have a good chance of
    landing on already-scored candidates.

    Doesn't apply tags — tagging is gated separately on whether the lead
    recruiter has calibrated for the position.
    """
    from .prewarm import prewarm_all_open_positions

    res = prewarm_all_open_positions(n_per_position=15, time_budget_s=900.0)
    if res.get("error"):
        log.error("prewarm-all: %s", res["error"])
        return 1
    log.info(
        "prewarm-all done: scanned=%d elapsed=%ss",
        res.get("scanned", 0), res.get("elapsed_s", 0),
    )
    return 0


async def cmd_refresh_rubrics() -> int:
    """Force-regenerate learned rubrics for every class with enough feedback."""
    from .position_classes import list_all_classes
    from .rubrics import refresh_learned_rubric

    refreshed = 0
    for cls in list_all_classes():
        result = refresh_learned_rubric(cls["id"], cls["name"])
        if result.get("ok"):
            log.info("refreshed rubric for %s (samples=%s)", cls["id"], result.get("feedback_count"))
            refreshed += 1
        else:
            log.debug("rubric refresh skipped for %s: %s", cls["id"], result.get("error"))
    log.info("refresh-rubrics done: %d classes refreshed", refreshed)
    return 0


async def cmd_refresh_comeet_session() -> int:
    """Force a fresh app.comeet.co login (drops the cached session first)."""
    from .comeet_app_client import ComeetAppClient, clear_session

    clear_session()
    client = ComeetAppClient()
    client.login()
    summary = client.session_summary()
    log.info("refresh-comeet-session done: %s", summary)
    return 0


async def cmd_poll_feedback() -> int:
    """Sweep applied_tags rows; record auto-feedback when recruiter swapped our tag."""
    from .feedback_polling import poll_tag_changes

    result = poll_tag_changes()
    log.info(
        "poll-feedback done: checked=%d feedback=%d dropped=%d errors=%d",
        result.candidates_checked, result.feedback_recorded,
        result.tags_dropped, len(result.errors),
    )
    return 0


async def cmd_clear_score_locks(position_uid: str | None = None) -> int:
    """Delete score-done locks so the next scan re-queues those candidates.

    Usage:
        python -m app.cli clear-score-locks                # all positions (nuclear)
        python -m app.cli clear-score-locks <position_uid> # only candidates on this position
    """
    from sqlalchemy import select
    from .comeet_client import ComeetClient
    from .db import db_session
    from .models import CandidateLock

    cleared = 0
    if position_uid:
        # Fetch candidate UIDs on the position so we only clear those.
        with ComeetClient() as client:
            candidates = client.list_candidates_for_position(position_uid)
        uids = {str(c.get("uid")) for c in candidates if c.get("uid")}
        log.info("clear-score-locks: %d candidates on position %s", len(uids), position_uid)
        if not uids:
            return 0
        keys = [f"score_done:{u}" for u in uids]
        with db_session() as ses:
            cleared = ses.query(CandidateLock).filter(CandidateLock.key.in_(keys)).delete(
                synchronize_session=False,
            )
    else:
        log.warning("clear-score-locks: clearing ALL score-done locks (no position uid given)")
        with db_session() as ses:
            cleared = ses.query(CandidateLock).filter(
                CandidateLock.key.like("score_done:%")
            ).delete(synchronize_session=False)

    log.info("clear-score-locks done: deleted=%d", cleared)
    return 0


async def cmd_backfill_tags(position_uid: str | None = None) -> int:
    """Apply rating tags to every candidate in debug_scoring that has a final_rating
    but no applied_tags row yet. Useful for back-filling tags after a scan was run
    while AUTO_TAG_ENABLED was off.

    Usage:
        python -m app.cli backfill-tags                # all positions
        python -m app.cli backfill-tags <position_uid> # one position
    """
    from sqlalchemy import select
    from .comeet_app_client import ComeetAppClient
    from .db import db_session
    from .models import AppliedTag, DebugScoring
    from .tagging import RATING_TAG_NAMES, apply_rating_tag

    with db_session() as ses:
        stmt = select(DebugScoring).where(DebugScoring.final_rating.isnot(None))
        if position_uid:
            stmt = stmt.where(DebugScoring.position_uid == position_uid)
        rows = ses.scalars(stmt).all()
    log.info("backfill-tags: %d debug-scoring rows to consider", len(rows))

    # Drop rows that already have ANY applied_tags entry (already tagged).
    with db_session() as ses:
        existing_uids = {
            row[0] for row in ses.execute(
                select(AppliedTag.candidate_uid).distinct()
            ).all()
        }

    rating_lookup = RATING_TAG_NAMES
    client = ComeetAppClient()

    tagged = 0
    skipped = 0
    errors = 0
    for r in rows:
        if not r.candidate_uid:
            skipped += 1
            continue
        if r.candidate_uid in existing_uids:
            skipped += 1
            continue
        if r.final_rating not in rating_lookup:
            skipped += 1
            continue
        try:
            applied = apply_rating_tag(
                r.candidate_uid, r.final_rating,
                client=client,
                position_uid=r.position_uid,
                position_name=r.position_name,
                force=True,  # bypass the AUTO_TAG_ENABLED check (we know we want this)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill-tags: %s failed: %s", r.candidate_uid, exc)
            errors += 1
            continue
        if applied:
            tagged += 1
        else:
            skipped += 1
    log.info("backfill-tags done: tagged=%d skipped=%d errors=%d", tagged, skipped, errors)
    return 0


COMMANDS = {
    "scan-all": cmd_scan_all,
    "prewarm-all": cmd_prewarm_all,
    "refresh-rubrics": cmd_refresh_rubrics,
    "refresh-comeet-session": cmd_refresh_comeet_session,
    "poll-feedback": cmd_poll_feedback,
    "backfill-tags": cmd_backfill_tags,
    "clear-score-locks": cmd_clear_score_locks,
    "reset-thresholds": cmd_reset_thresholds,
    "reset-for-launch": cmd_reset_for_launch,
    "rescore-all": cmd_rescore_all,
    "reset-and-rescore": cmd_reset_and_rescore,
}

# Commands that accept an optional position_uid positional arg.
COMMANDS_WITH_POSITION = {"backfill-tags", "clear-score-locks", "reset-thresholds", "rescore-all"}


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("command", choices=COMMANDS.keys())
    parser.add_argument("position_uid", nargs="?", default=None)
    args = parser.parse_args()
    cmd = COMMANDS[args.command]
    if args.command in COMMANDS_WITH_POSITION:
        return asyncio.run(cmd(args.position_uid))
    return asyncio.run(cmd())


if __name__ == "__main__":
    sys.exit(main())
