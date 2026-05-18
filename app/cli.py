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


async def cmd_benchmark(position_uid: str | None = None) -> int:
    """Print a side-by-side AI-vs-recruiter rating table for a position.

    Usage:
        python -m app.cli benchmark 439121        # numeric Comeet ID
        python -m app.cli benchmark DC.E45        # alphanumeric UID
        python -m app.cli benchmark                # all positions

    Joins calibration_verdicts.recruiter_rating (your ground-truth 1-10
    rating) with the latest debug_scoring row per candidate (the AI's
    1-10 rating + per-dim sub-scores). Skips candidates where you haven't
    rated yet (recruiter_rating IS NULL).

    Output:
      - Per-candidate table: name, AI, you, |delta|, gates
      - Summary: mean |delta|, RMSE, % within ±1, % within ±2

    Use this after rating ~10+ candidates manually to see where the AI
    is systematically over- or under-rating — that's the signal for the
    next prompt tweak.
    """
    from sqlalchemy import select, desc, func
    from .db import db_session
    from .models import CalibrationVerdict, DebugScoring
    from .scan import _resolve_numeric_position_uid

    pos = (position_uid or "").strip()
    if pos and pos.isdigit():
        pos = _resolve_numeric_position_uid(pos) or pos

    with db_session() as ses:
        # Latest verdict per (recruiter, candidate, position) — when a
        # candidate has been re-rated, only the most recent count.
        verdicts_q = (
            select(
                CalibrationVerdict.candidate_uid,
                CalibrationVerdict.recruiter_name,
                CalibrationVerdict.recruiter_rating,
                CalibrationVerdict.ai_rating.label("ai_rating_at_verdict"),
                CalibrationVerdict.feedback_text,
                CalibrationVerdict.position_uid,
            )
            .where(CalibrationVerdict.recruiter_rating.is_not(None))
            .order_by(desc(CalibrationVerdict.id))
        )
        if pos:
            verdicts_q = verdicts_q.where(CalibrationVerdict.position_uid == pos)
        raw_verdicts = ses.execute(verdicts_q).all()

        # Dedup: keep most recent verdict per (position, candidate).
        seen: set[tuple[str, str]] = set()
        verdicts: list[dict] = []
        for v in raw_verdicts:
            key = (v.position_uid or "", v.candidate_uid or "")
            if key in seen:
                continue
            seen.add(key)
            verdicts.append({
                "candidate_uid": v.candidate_uid,
                "position_uid": v.position_uid,
                "recruiter": v.recruiter_name,
                "recruiter_rating": v.recruiter_rating,
                "feedback_text": (v.feedback_text or "").strip(),
            })

        if not verdicts:
            log.info(
                "benchmark: no verdicts with recruiter_rating found%s",
                f" for position {pos}" if pos else "",
            )
            return 0

        # Pull the latest DebugScoring per (position, candidate) for the
        # ones we have verdicts on. One query batch + manual dedup.
        candidate_uids = [v["candidate_uid"] for v in verdicts]
        position_uids = list({v["position_uid"] for v in verdicts if v["position_uid"]})
        scoring_rows = ses.execute(
            select(DebugScoring)
            .where(
                DebugScoring.candidate_uid.in_(candidate_uids),
                DebugScoring.position_uid.in_(position_uids),
                DebugScoring.final_rating.is_not(None),
            )
            .order_by(desc(DebugScoring.id))
        ).scalars().all()

        latest_scoring: dict[tuple[str, str], DebugScoring] = {}
        for r in scoring_rows:
            key = (r.position_uid or "", r.candidate_uid or "")
            if key in latest_scoring:
                continue
            latest_scoring[key] = r

    # Build the comparison rows.
    rows: list[dict] = []
    for v in verdicts:
        s = latest_scoring.get((v["position_uid"] or "", v["candidate_uid"] or ""))
        if not s:
            continue
        ai = int(s.final_rating or 0)
        recruiter = int(v["recruiter_rating"] or 0)
        delta = ai - recruiter
        gates: list[str] = []
        if s.dim_location_match is not None and s.dim_location_match < 4:
            gates.append("LOC")
        if s.dim_domain_match is not None and s.dim_domain_match < 5 and ai <= 5:
            gates.append("DOM")
        rows.append({
            "name": s.candidate_name or v["candidate_uid"] or "—",
            "position": s.position_name or v["position_uid"] or "—",
            "ai": ai,
            "recruiter": recruiter,
            "delta": delta,
            "abs_delta": abs(delta),
            "gates": ",".join(gates) or "—",
            "feedback": v["feedback_text"],
        })

    if not rows:
        log.info("benchmark: no matched (verdict + scoring) rows")
        return 0

    # Sort by abs delta DESC — the worst misses surface first.
    rows.sort(key=lambda r: (-r["abs_delta"], r["name"]))

    # Print table.
    print()
    print(f"{'Candidate':<32} {'Position':<28} {'AI':>3} {'You':>4} {'Δ':>4}  {'Gates':<8} Feedback")
    print("-" * 110)
    for r in rows[:80]:  # cap at 80 rows so console isn't overwhelming
        delta_str = f"{r['delta']:+d}"
        print(
            f"{r['name'][:31]:<32} "
            f"{r['position'][:27]:<28} "
            f"{r['ai']:>3} {r['recruiter']:>4} {delta_str:>4}  "
            f"{r['gates']:<8} "
            f"{(r['feedback'][:50] or '')}"
        )
    if len(rows) > 80:
        print(f"... and {len(rows) - 80} more")

    # Summary stats.
    n = len(rows)
    abs_deltas = [r["abs_delta"] for r in rows]
    deltas = [r["delta"] for r in rows]
    mean_abs = sum(abs_deltas) / n
    rmse = (sum(d * d for d in deltas) / n) ** 0.5
    within_1 = sum(1 for d in abs_deltas if d <= 1) / n * 100
    within_2 = sum(1 for d in abs_deltas if d <= 2) / n * 100
    bias = sum(deltas) / n
    overcalls = sum(1 for d in deltas if d > 0)
    undercalls = sum(1 for d in deltas if d < 0)

    print()
    print("─── Summary ──────────────────────────────────────────────")
    print(f"  n            : {n}")
    print(f"  mean |Δ|     : {mean_abs:.2f}")
    print(f"  RMSE         : {rmse:.2f}")
    print(f"  within ±1    : {within_1:.1f}%")
    print(f"  within ±2    : {within_2:.1f}%")
    print(f"  bias (AI − you): {bias:+.2f}  "
          f"({'AI over-rates' if bias > 0 else 'AI under-rates' if bias < 0 else 'no bias'})")
    print(f"  over-calls   : {overcalls} (AI > you)")
    print(f"  under-calls  : {undercalls} (AI < you)")
    print()
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


async def cmd_reset_rubric(position_uid: str | None = None) -> int:
    """Wipe learned_rubrics + recruiter_thresholds, keeping verdicts intact.

    Use this before re-benchmarking the algorithm against the SAME 1-10
    ratings you've already given. The learned rubric encodes corrections
    the AI was making against the OLD prompt; keeping it would double-count
    when you rescore with a new prompt, contaminating the A/B signal.

    Usage:
        python -m app.cli reset-rubric                # all positions
        python -m app.cli reset-rubric 439121         # one position (numeric or alphanumeric)

    WIPED:
      - learned_rubrics      (per-class rubrics synthesised from feedback)
      - recruiter_thresholds (per-(recruiter, position) tagging cutoffs;
                              if a position_uid is given, only that row)

    KEPT:
      - calibration_verdicts (your 1-10 ground-truth ratings)
      - debug_scoring        (existing AI scores — rescore-all overwrites)
      - feedback             (free-text feedback notes on verdicts)

    Typical workflow:
      1. reset-rubric 439121
      2. rescore-all 439121
      3. benchmark 439121
    """
    from sqlalchemy import delete, select
    from .db import db_session
    from .models import LearnedRubric, RecruiterThreshold, PositionClass

    pos = (position_uid or "").strip()
    counts: dict[str, int] = {}

    with db_session() as ses:
        # Rubrics live per CLASS (not per position). If a position was
        # supplied, look up its class and wipe only that class's rubric;
        # otherwise wipe all rubrics across all classes.
        if pos:
            cls_row = ses.scalar(
                select(PositionClass).where(PositionClass.position_uid == pos)
            )
            cls_id = (cls_row.class_id if cls_row else None) or ""
            if cls_id:
                res = ses.execute(
                    delete(LearnedRubric).where(LearnedRubric.class_id == cls_id)
                )
                counts["learned_rubrics"] = res.rowcount or 0
                log.info("scoped rubric wipe to class %s for position %s", cls_id, pos)
            else:
                counts["learned_rubrics"] = 0
                log.warning(
                    "position %s has no class assigned — skipped rubric wipe",
                    pos,
                )

            thr_stmt = delete(RecruiterThreshold).where(
                RecruiterThreshold.position_uid == pos
            )
            res = ses.execute(thr_stmt)
            counts["recruiter_thresholds"] = res.rowcount or 0
        else:
            res = ses.execute(delete(LearnedRubric))
            counts["learned_rubrics"] = res.rowcount or 0
            res = ses.execute(delete(RecruiterThreshold))
            counts["recruiter_thresholds"] = res.rowcount or 0
        ses.commit()

    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    scope = f" for position {pos}" if pos else " (all positions)"
    log.info("reset-rubric done%s: %s", scope, summary)
    log.info(
        "next steps: 'rescore-all%s' then 'benchmark%s'",
        f" {pos}" if pos else "",
        f" {pos}" if pos else "",
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
    "reset-rubric": cmd_reset_rubric,
    "reset-for-launch": cmd_reset_for_launch,
    "rescore-all": cmd_rescore_all,
    "reset-and-rescore": cmd_reset_and_rescore,
    "benchmark": cmd_benchmark,
}

# Commands that accept an optional position_uid positional arg.
COMMANDS_WITH_POSITION = {
    "backfill-tags", "clear-score-locks", "reset-thresholds",
    "reset-rubric", "rescore-all", "benchmark",
}


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
