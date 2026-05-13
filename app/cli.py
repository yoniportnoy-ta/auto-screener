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
}

# Commands that accept an optional position_uid positional arg.
COMMANDS_WITH_POSITION = {"backfill-tags", "clear-score-locks"}


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
