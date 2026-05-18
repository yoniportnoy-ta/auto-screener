"""Background pre-scoring so calibration sessions start with a full batch.

The recruiter UX is "click a position → in seconds, see 5 already-scored
candidates to thumb through". That only works if scoring happens before the
recruiter arrives. This module exposes:

  - `prewarm_position(position_uid, n=15)` — fire-and-forget request to
    score the next N unscored candidates for a position in a daemon thread.
    Idempotent: a second call while one is already running is a no-op.
    Returns immediately (HTTP-friendly).

  - `prewarm_all_open_positions(n_per_position=15, time_budget_s=...)` —
    walks every open Comeet position and prewarms each in sequence. Wired
    from the existing cron in `automation.run_autoscan` so the system
    self-populates over time.

Why a thread (not Celery / a queue)? We're on a one-worker Render service.
A daemon thread inside the same process is simpler, doesn't add an extra
service to monitor, and we already eat the OOM risk; one extra in-process
scan won't change that. If we grow past one worker we'll need a real
queue, but that day is not today.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from typing import Any

log = logging.getLogger(__name__)


# Per-position lock. Held while a prewarm thread is in flight for that
# position; second concurrent calls short-circuit instead of stacking.
_inflight_lock = threading.Lock()
_inflight: dict[str, float] = {}

# Hard ceiling on how stale an "in flight" entry can be before we let
# another thread start. Acts as a self-heal in case a worker died without
# clearing its slot.
_INFLIGHT_TTL_S = 15 * 60


def _claim_slot(position_uid: str) -> bool:
    """Try to claim the prewarm slot for this position. True = caller may
    proceed and must call _release_slot when done. False = a healthy
    prewarm is already running; caller should bail."""
    now = _time.monotonic()
    with _inflight_lock:
        existing = _inflight.get(position_uid)
        if existing is not None and (now - existing) < _INFLIGHT_TTL_S:
            return False
        _inflight[position_uid] = now
        return True


def _release_slot(position_uid: str) -> None:
    with _inflight_lock:
        _inflight.pop(position_uid, None)


def _do_prewarm(position_uid: str, n: int) -> None:
    """Body of the background thread. Scores up to N candidates for the
    given position using the same begin/score/finish pipeline that the
    interactive scan + calibration lazy-fill use.
    """
    try:
        # Lazy imports — keep app boot fast, and avoid pulling Claude / Comeet
        # client init costs into the request thread that spawned us.
        from .scan import (
            begin_scan_batch,
            finish_scan_batch,
            score_candidate_in_session,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("prewarm: scan import failed: %s", exc)
        return

    try:
        batch = begin_scan_batch(position_uid)
    except Exception as exc:  # noqa: BLE001
        log.info("prewarm: begin_scan_batch failed for %s: %s", position_uid, exc)
        return

    if batch.empty or not batch.uids:
        log.info("prewarm: nothing to score for %s", position_uid)
        return

    to_score = batch.uids[: max(1, n)]
    log.info("prewarm: scoring %d candidates for %s", len(to_score), position_uid)

    started = _time.monotonic()
    scored = 0
    for uid in to_score:
        try:
            summary = score_candidate_in_session(batch.session_id, uid)
        except Exception as exc:  # noqa: BLE001
            log.warning("prewarm: score failed for %s: %s", uid, exc)
            continue
        if not getattr(summary, "error", None):
            scored += 1

    try:
        finish_scan_batch(batch.session_id, to_score)
    except Exception as exc:  # noqa: BLE001
        log.info("prewarm: finish_scan_batch failed for %s: %s", position_uid, exc)

    log.info(
        "prewarm: done %s — scored %d/%d in %.1fs",
        position_uid, scored, len(to_score), _time.monotonic() - started,
    )


def prewarm_position(position_uid: str, *, n: int = 15) -> dict[str, Any]:
    """Public entry point. Spawns a daemon thread (if no other prewarm is
    already running for this position) and returns immediately.

    Response shape:
      { "status": "started" | "already_running" | "no_op", "positionUid": ... }
    """
    uid = (position_uid or "").strip()
    if not uid:
        return {"status": "no_op", "positionUid": "", "reason": "missing position_uid"}

    # Master pause switch for benchmark/A-B testing.
    from .config import settings
    if settings.scoring_pause_auto:
        log.info("prewarm_position: SCORING_PAUSE_AUTO is set — skipping %s", uid)
        return {"status": "no_op", "positionUid": uid, "reason": "auto-scoring paused"}

    if not _claim_slot(uid):
        return {"status": "already_running", "positionUid": uid}

    def _runner() -> None:
        try:
            _do_prewarm(uid, n)
        finally:
            _release_slot(uid)

    t = threading.Thread(target=_runner, name=f"prewarm-{uid}", daemon=True)
    t.start()
    return {"status": "started", "positionUid": uid, "n": n}


def prewarm_all_open_positions(
    *,
    n_per_position: int = 15,
    time_budget_s: float = 900.0,
) -> dict[str, Any]:
    """Sequentially prewarm every open Comeet position. Intended for the
    hourly cron so the system keeps the queue warm over time without
    requiring recruiters to opt-in per position.

    Sequential (not parallel) on purpose — the 512 MB → 2 GB jump fixed
    most OOMs but Claude scoring is still memory-heavy enough that
    parallel workers risk eating each other. Sequential plus per-position
    timing keeps it predictable.
    """
    from .comeet_client import ComeetClient
    from .config import settings

    if settings.scoring_pause_auto:
        log.info("prewarm_all: SCORING_PAUSE_AUTO is set — exiting early")
        return {"scanned": 0, "elapsed_s": 0.0, "note": "auto-scoring paused"}

    started = _time.monotonic()
    try:
        with ComeetClient() as client:
            positions = client.list_open_positions()
    except Exception as exc:  # noqa: BLE001
        log.warning("prewarm_all: list_open_positions failed: %s", exc)
        return {"error": f"list_open_positions: {exc}", "scanned": 0}

    scanned = 0
    for p in positions:
        uid = str(p.get("uid") or "")
        if not uid:
            continue
        if (_time.monotonic() - started) > time_budget_s - 30:
            log.info("prewarm_all: time budget low, stopping after %d", scanned)
            break
        # Run inline (not threaded) — we're already in a background task
        # and we want bounded serial work, not a thread explosion.
        if not _claim_slot(uid):
            log.info("prewarm_all: %s already in flight, skipping", uid)
            continue
        try:
            _do_prewarm(uid, n_per_position)
        finally:
            _release_slot(uid)
        scanned += 1

    return {"scanned": scanned, "elapsed_s": round(_time.monotonic() - started, 1)}


__all__ = ["prewarm_position", "prewarm_all_open_positions"]
