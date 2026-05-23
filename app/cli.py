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
    from .benchmark_stats import (
        compute_benchmark_stats,
        is_dirty_feedback,
        FN_AI_MAX,
        FN_REC_MIN,
        FP_AI_MIN,
        FP_REC_MAX,
    )

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

    # Build the comparison rows. Track dirty (cache-error) verdicts
    # separately so they're displayed but excluded from the summary stats.
    rows: list[dict] = []
    dirty_rows: list[dict] = []
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
        is_dirty = is_dirty_feedback(v["feedback_text"])
        row = {
            "name": s.candidate_name or v["candidate_uid"] or "—",
            "position": s.position_name or v["position_uid"] or "—",
            "ai": ai,
            "recruiter": recruiter,
            "delta": delta,
            "abs_delta": abs(delta),
            "gates": ",".join(gates) or "—",
            "feedback": v["feedback_text"],
            "dirty": is_dirty,
        }
        if is_dirty:
            dirty_rows.append(row)
        else:
            rows.append(row)

    if not rows and not dirty_rows:
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

    if dirty_rows:
        # These are usually one-off — stale enrichment cache or transient
        # Anthropic 5xxs captured into feedback_text. Show them at the
        # bottom so the operator knows to look, and exclude them from
        # the summary stats below.
        print()
        print(f"─── ⚠ {len(dirty_rows)} dirty verdict(s) (excluded from stats) ──────────")
        for r in dirty_rows[:20]:
            print(
                f"  {r['name'][:30]:<32} "
                f"AI={r['ai']:>2} You={r['recruiter']:>2}  "
                f"{(r['feedback'][:70] or '')}"
            )
        if len(dirty_rows) > 20:
            print(f"  ... and {len(dirty_rows) - 20} more")

    # Summary stats — computed via the shared helper so the CLI agrees
    # with the round-summary UI and the meta-benchmark down to the digit.
    # We pass the raw verdict-like dicts so the helper re-runs the dirty
    # filter (defensive — the rows here are already clean, but using the
    # helper keeps one source of truth).
    stats = compute_benchmark_stats(
        [
            {
                "ai_rating": r["ai"],
                "recruiter_rating": r["recruiter"],
                "feedback_text": r["feedback"],
            }
            for r in rows
        ],
    )
    n = stats["count"]
    if n == 0:
        print()
        print("─── Summary ──────────────────────────────────────────────")
        print(f"  (no clean verdicts — {len(dirty_rows)} dirty rows excluded)")
        return 0

    overcalls = sum(1 for r in rows if r["delta"] > 0)
    undercalls = sum(1 for r in rows if r["delta"] < 0)
    disc = stats["discrimination_ratio"]
    disc_warning = ""
    if disc is not None and disc < 0.5:
        disc_warning = "  ⚠ AI collapsing (σ ratio < 0.5)"
    elif disc is not None and disc > 1.5:
        disc_warning = "  ⚠ AI over-spread (σ ratio > 1.5)"

    print()
    print("─── Summary ──────────────────────────────────────────────")
    print(f"  n            : {n}" + (f"  ({len(dirty_rows)} dirty excluded)" if dirty_rows else ""))
    print(f"  mean |Δ|     : {stats['mae']:.2f}")
    print(f"  RMSE         : {stats['rmse']:.2f}")
    print(f"  within ±1    : {stats['within_1'] * 100:.1f}%")
    print(f"  within ±2    : {stats['within_2'] * 100:.1f}%")
    print(f"  bias (AI−you): {stats['bias']:+.2f}  "
          f"({'AI over-rates' if stats['bias'] > 0 else 'AI under-rates' if stats['bias'] < 0 else 'no bias'})")
    print(f"  AI σ / Rec σ : "
          + (f"{stats['std_ai']:.2f} / {stats['std_recruiter']:.2f}"
             if stats['std_ai'] is not None and stats['std_recruiter'] is not None
             else "—"))
    print(f"  σ ratio      : "
          + (f"{disc:.2f}" if disc is not None else "—")
          + disc_warning)
    print(f"  false-neg    : {stats['false_negatives']}  "
          f"(AI≤{FN_AI_MAX} & you≥{FN_REC_MIN} — would reject who you'd interview)")
    print(f"  false-pos    : {stats['false_positives']}  "
          f"(AI≥{FP_AI_MIN} & you≤{FP_REC_MAX} — would advance who you'd skip)")
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


async def cmd_backfill_feedback() -> int:
    """One-shot: copy every calibration_verdict that has a recruiter_rating
    into the `feedback` table. The learned_rubric pipeline reads from
    `feedback`, not `calibration_verdicts`, so any verdicts recorded before
    the dual-write fix in record_verdict are invisible to rubric synthesis.

    Idempotent: skips verdicts whose (candidate_uid, position_uid) already
    have a feedback row.

    Usage:
        python -m app.cli backfill-feedback

    Typical workflow:
      1. backfill-feedback
      2. refresh-rubrics
      3. rescore-all <position_uid>
      4. benchmark <position_uid>
    """
    from sqlalchemy import select, desc
    from .db import db_session
    from .feedback import save_feedback
    from .models import (
        CalibrationVerdict, PositionClass, DebugScoring, Feedback,
    )

    inserted = 0
    skipped_dup = 0
    skipped_no_class = 0
    skipped_no_rating = 0
    errors = 0

    with db_session() as ses:
        verdicts = ses.scalars(
            select(CalibrationVerdict)
            .where(CalibrationVerdict.recruiter_rating.is_not(None))
            .order_by(CalibrationVerdict.created_at)
        ).all()

    log.info("backfill-feedback: found %d calibration verdicts with ratings", len(verdicts))

    backfilled_axes = 0  # rows where we copied broken_axes onto an existing feedback row

    for v in verdicts:
        try:
            # Normalise the verdict's broken_axes once; reused below.
            v_axes = None
            raw_axes = getattr(v, "broken_axes_json", None)
            if isinstance(raw_axes, list) and raw_axes:
                v_axes = [str(a) for a in raw_axes if a]

            with db_session() as ses:
                # Skip if a feedback row already exists for this candidate
                # in this position (idempotency). EXCEPT: if the existing row
                # has no broken_axes and the verdict has them, top them up.
                # This recovers per-axis signal for rows backfilled before
                # the broken_axes column existed (i.e. before migration
                # 0012). Without this, every rerun would no-op on
                # the rows we care about.
                existing = ses.scalar(
                    select(Feedback).where(
                        (Feedback.candidate_uid == v.candidate_uid)
                        & (Feedback.position_uid == v.position_uid)
                    ).limit(1)
                )
                if existing:
                    if v_axes and not (existing.broken_axes_json or []):
                        existing.broken_axes_json = v_axes
                        ses.commit()
                        backfilled_axes += 1
                    skipped_dup += 1
                    continue

                cls_row = ses.scalar(
                    select(PositionClass).where(
                        PositionClass.position_uid == v.position_uid
                    )
                )
                if not cls_row or not cls_row.class_id:
                    skipped_no_class += 1
                    continue

                ds_row = ses.scalar(
                    select(DebugScoring)
                    .where(
                        (DebugScoring.candidate_uid == v.candidate_uid)
                        & (DebugScoring.position_uid == v.position_uid)
                    )
                    .order_by(desc(DebugScoring.timestamp))
                    .limit(1)
                )
                candidate_name = (ds_row.candidate_name if ds_row else "") or ""
                position_name = (ds_row.position_name if ds_row else "") or ""

            save_feedback(
                class_id=cls_row.class_id,
                class_name=cls_row.class_name or cls_row.class_id,
                position_uid=v.position_uid,
                position_name=position_name,
                candidate_uid=v.candidate_uid,
                candidate_name=candidate_name,
                ai_rating=v.ai_rating,
                recruiter_rating=v.recruiter_rating,
                note=(v.feedback_text or "").strip(),
                recruiter_email=v.recruiter_name or "",
                broken_axes=v_axes,
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "backfill-feedback: failed on verdict id=%s candidate=%s: %s",
                v.id, v.candidate_uid, exc,
            )
            errors += 1

    log.info(
        "backfill-feedback done: inserted=%d skipped_dup=%d "
        "skipped_no_class=%d skipped_no_rating=%d errors=%d "
        "broken_axes_topped_up=%d",
        inserted, skipped_dup, skipped_no_class, skipped_no_rating, errors,
        backfilled_axes,
    )
    log.info("next: 'refresh-rubrics' then 'rescore-all <position>'")
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


async def cmd_reset_debug_scoring(position_uid: str | None = None) -> int:
    """Wipe ALL debug_scoring rows AND all score_done locks globally.
    Preserves rubrics + feedback + verdicts + thresholds + position classes
    + geo overlays.

    The score_done lock wipe was added 2026-05-22 after a trial revealed
    that wiping debug_scoring alone leaves the scan flow's lock layer
    pointing at the now-deleted scoring history → every candidate is
    skipped as "already scored" on the next scan. Two-stage reset keeps
    that gotcha invisible.

    No `position_uid` arg — this is a global reset by design. Pass
    nothing.

    Usage:
        python -m app.cli reset-debug-scoring
    """
    from .db import db_session
    from .models import CandidateLock, DebugScoring

    if position_uid:
        log.warning(
            "reset-debug-scoring: position_uid arg %r ignored — this command "
            "wipes ALL debug_scoring rows globally. Use reset-position for "
            "per-position wipes.",
            position_uid,
        )

    with db_session() as ses:
        ds_before = ses.query(DebugScoring).count()
        ses.query(DebugScoring).delete()
        # Wipe score_done locks so the scan flow re-queues every candidate
        # under the new prompts. Note/last-review locks are preserved —
        # those track recruiter-facing state, not AI-scored state.
        locks_before = (
            ses.query(CandidateLock)
            .filter(CandidateLock.key.like("score_done:%"))
            .count()
        )
        ses.query(CandidateLock).filter(
            CandidateLock.key.like("score_done:%")
        ).delete(synchronize_session=False)
        ses.commit()
    log.info(
        "reset-debug-scoring done: deleted %d debug_scoring rows + %d "
        "score_done locks",
        ds_before, locks_before,
    )
    log.info(
        "next: 'rescore-all <position_uid>' on each position you want "
        "scored under the new framework, OR let the calibration UI's "
        "lazy-fill score candidates on demand as the recruiter opens batches."
    )
    return 0


async def cmd_reset_position(position_uid: str | None = None) -> int:
    """Wipe everything for ONE position. Used post-trial to reset the
    test position to a clean state without affecting any other position.

    Deletes:
      - debug_scoring rows for this position_uid
      - calibration_verdicts rows for this position_uid
      - feedback rows for this position_uid
      - recruiter_thresholds for this position_uid (every recruiter)
      - position-specific learned_rubric (the (class_id, position_uid)
        row in learned_rubrics; the class-level rubric stays intact)

    Preserves:
      - position_classes (class assignment + recruiter notes)
      - applied_tags (so re-tagging is idempotent)
      - the class-level learned_rubric (used by other positions in the
        same class)

    Usage:
        python -m app.cli reset-position <position_uid>
    """
    from .db import db_session
    from .models import (
        CalibrationVerdict, DebugScoring, Feedback, LearnedRubric,
        RecruiterThreshold,
    )

    uid = (position_uid or "").strip()
    if not uid:
        log.error("reset-position requires a position_uid arg")
        return 2

    counts: dict[str, int] = {}
    with db_session() as ses:
        counts["debug_scoring"] = (
            ses.query(DebugScoring).filter(DebugScoring.position_uid == uid).delete()
        )
        counts["calibration_verdicts"] = (
            ses.query(CalibrationVerdict).filter(CalibrationVerdict.position_uid == uid).delete()
        )
        counts["feedback"] = (
            ses.query(Feedback).filter(Feedback.position_uid == uid).delete()
        )
        counts["recruiter_thresholds"] = (
            ses.query(RecruiterThreshold).filter(RecruiterThreshold.position_uid == uid).delete()
        )
        counts["position_rubric"] = (
            ses.query(LearnedRubric).filter(LearnedRubric.position_uid == uid).delete()
        )
        ses.commit()

    log.info(
        "reset-position %s done: %s",
        uid,
        ", ".join(f"{k}={v}" for k, v in counts.items()),
    )
    log.info("position class assignment + recruiter notes preserved.")
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
    "backfill-feedback": cmd_backfill_feedback,
    "reset-for-launch": cmd_reset_for_launch,
    "rescore-all": cmd_rescore_all,
    "reset-and-rescore": cmd_reset_and_rescore,
    "benchmark": cmd_benchmark,
    "reset-debug-scoring": cmd_reset_debug_scoring,
    "reset-position": cmd_reset_position,
}

# Commands that accept an optional position_uid positional arg.
COMMANDS_WITH_POSITION = {
    "backfill-tags", "clear-score-locks", "reset-thresholds",
    "reset-rubric", "rescore-all", "benchmark", "reset-position",
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
