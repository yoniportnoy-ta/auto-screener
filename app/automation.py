"""Scheduled scan across all open positions.

Render runs `python -m app.cli scan-all` every hour. This module is the
implementation: walk all open positions with an assigned class, score new
candidates, apply rating tags. Cursor across positions in Postgres so a
crashed run resumes where it left off.

The cron container is short-lived (~5 min budget per run), so we cap:
  - positions per run         (env: AUTOSCAN_MAX_POSITIONS_PER_RUN, default 6)
  - candidates per position   (env: AUTOSCAN_MAX_CANDIDATES_PER_POS, default 10)
  - total time budget         (env: AUTOSCAN_TIME_BUDGET_S, default 270)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .comeet_client import ComeetClient
from .config import settings
from .db import db_session
from .models import CandidateLock
from .position_classes import get_position_class, list_auto_screen_positions
from .scan import begin_scan_batch, score_candidate_in_session, finish_scan_batch

log = logging.getLogger(__name__)

CURSOR_KEY = "autoscan:position_cursor"
RUNLOG_KEY = "autoscan:last_run"


# ─── Tunables ────────────────────────────────────────────────────────────────
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        n = int(raw)
        if lo <= n <= hi:
            return n
    except ValueError:
        pass
    return default


def get_time_budget_seconds() -> int:
    """Wall-clock cap for one cron invocation. Render cron jobs run in their
    own container with a generous limit, but we still cap so the tail end of
    a job doesn't run into the next scheduled tick. Score-done locks make
    partial completion safe — we just resume next hour.
    """
    return _env_int("AUTOSCAN_TIME_BUDGET_S", 1500, 60, 3000)


# ─── Cursor + run log persistence ────────────────────────────────────────────
def _read_cursor() -> int:
    with db_session() as ses:
        row = ses.scalar(select(CandidateLock).where(CandidateLock.key == CURSOR_KEY))
        if not row or not row.value:
            return 0
        try:
            return max(0, int(row.value))
        except ValueError:
            return 0


def _write_cursor(value: int) -> None:
    with db_session() as ses:
        stmt = pg_insert(CandidateLock).values(key=CURSOR_KEY, value=str(value))
        stmt = stmt.on_conflict_do_update(
            index_elements=[CandidateLock.key],
            set_={"value": stmt.excluded.value, "updated_at": datetime.now(timezone.utc)},
        )
        ses.execute(stmt)


def _write_runlog(payload: dict) -> None:
    import json
    with db_session() as ses:
        stmt = pg_insert(CandidateLock).values(key=RUNLOG_KEY, value=json.dumps(payload)[:9000])
        stmt = stmt.on_conflict_do_update(
            index_elements=[CandidateLock.key],
            set_={"value": stmt.excluded.value, "updated_at": datetime.now(timezone.utc)},
        )
        ses.execute(stmt)


def get_last_run_log() -> dict | None:
    """Read the most recent autoscan run summary. Useful for ops dashboards."""
    import json
    with db_session() as ses:
        row = ses.scalar(select(CandidateLock).where(CandidateLock.key == RUNLOG_KEY))
        if not row or not row.value:
            return None
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return None


def reset_cursor() -> None:
    """Force the next run to start from the first position. Run from CLI for ops."""
    with db_session() as ses:
        ses.query(CandidateLock).filter(CandidateLock.key == CURSOR_KEY).delete()


# ─── Per-run summary types ───────────────────────────────────────────────────
@dataclass
class PositionRunResult:
    position_uid: str
    position_name: str
    class_id: str | None = None
    scored: int = 0
    skipped: int = 0
    tags_applied: int = 0
    errors: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class AutoscanResult:
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    positions_scanned: int = 0
    candidates_scored: int = 0
    tags_applied: int = 0
    cursor_before: int = 0
    cursor_after: int = 0
    total_positions: int = 0
    ran_out_of_time: bool = False
    auto_tag_enabled: bool = False
    per_position: list[PositionRunResult] = field(default_factory=list)
    error: str | None = None


# ─── Main entry ──────────────────────────────────────────────────────────────
def run_autoscan() -> AutoscanResult:
    """Walk a slice of open positions, score new candidates, apply rating tags."""
    start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    result = AutoscanResult(started_at=started_at, auto_tag_enabled=settings.auto_tag_enabled)

    if not settings.anthropic_api_key:
        result.error = "ANTHROPIC_API_KEY not set"
        result.finished_at = datetime.now(timezone.utc).isoformat()
        _write_runlog(_serialise(result))
        return result
    if not (settings.comeet_api_key and settings.comeet_api_secret):
        result.error = "COMEET_API_KEY / COMEET_API_SECRET not set"
        result.finished_at = datetime.now(timezone.utc).isoformat()
        _write_runlog(_serialise(result))
        return result

    time_budget = get_time_budget_seconds()

    try:
        with ComeetClient() as client:
            positions = client.list_open_positions()
    except Exception as exc:  # noqa: BLE001
        log.exception("autoscan: list_open_positions failed")
        result.error = f"list_open_positions: {exc}"
        result.finished_at = datetime.now(timezone.utc).isoformat()
        _write_runlog(_serialise(result))
        return result

    if not positions:
        result.note = "no open positions"
        result.finished_at = datetime.now(timezone.utc).isoformat()
        _write_runlog(_serialise(result))
        return result

    # Only walk positions the recruiter has explicitly opted in via the UI.
    enabled_uids = set(list_auto_screen_positions())
    if not enabled_uids:
        result.note = "no positions have auto-screen enabled"
        result.finished_at = datetime.now(timezone.utc).isoformat()
        _write_runlog(_serialise(result))
        return result

    enabled_positions = [p for p in positions if str(p.get("uid") or "") in enabled_uids]
    if not enabled_positions:
        result.note = (
            f"{len(enabled_uids)} positions have auto-screen enabled but none are currently open"
        )
        result.finished_at = datetime.now(timezone.utc).isoformat()
        _write_runlog(_serialise(result))
        return result

    cursor = _read_cursor()
    if cursor >= len(enabled_positions):
        cursor = 0
    result.cursor_before = cursor
    result.total_positions = len(enabled_positions)

    for offset in range(len(enabled_positions)):
        if (time.monotonic() - start) > time_budget:
            result.ran_out_of_time = True
            break

        idx = (cursor + offset) % len(enabled_positions)
        pos = enabled_positions[idx]
        position_uid = str(pos.get("uid") or "")
        position_name = str(pos.get("name") or position_uid)
        if not position_uid:
            continue

        cls = get_position_class(position_uid)
        if not cls:
            # Shouldn't happen — auto_screen_enabled requires a class — but be defensive.
            log.warning("autoscan: position %s opted in without class; skipping", position_uid)
            continue

        per = _scan_one_position(
            position_uid=position_uid,
            position_name=position_name,
            class_id=cls["classId"],
            time_remaining=max(5.0, time_budget - (time.monotonic() - start)),
        )
        result.per_position.append(per)
        result.positions_scanned += 1
        result.candidates_scored += per.scored
        result.tags_applied += per.tags_applied
        if (time.monotonic() - start) > time_budget:
            result.ran_out_of_time = True
            break

    new_cursor = (cursor + max(1, result.positions_scanned)) % max(1, len(enabled_positions))
    _write_cursor(new_cursor)
    result.cursor_after = new_cursor
    result.finished_at = datetime.now(timezone.utc).isoformat()
    result.duration_s = round(time.monotonic() - start, 2)
    _write_runlog(_serialise(result))
    log.info(
        "autoscan done: positions=%d scored=%d tagged=%d duration=%.1fs cursor=%d->%d",
        result.positions_scanned, result.candidates_scored, result.tags_applied,
        result.duration_s, result.cursor_before, result.cursor_after,
    )
    return result


def _scan_one_position(
    *,
    position_uid: str,
    position_name: str,
    class_id: str,
    time_remaining: float,
) -> PositionRunResult:
    """Drain every eligible candidate for one position.

    Calls begin_scan_batch repeatedly: each batch returns up to SCREENER_MAX_PER_RUN
    candidates and the score-done lock prevents re-queueing on the next iteration.
    Stops when begin reports `empty` or the time budget is exhausted.
    """
    out = PositionRunResult(position_uid=position_uid, position_name=position_name, class_id=class_id)
    start = time.monotonic()
    consecutive_empty_batches = 0

    while True:
        if (time.monotonic() - start) > time_remaining - 5:
            log.info("autoscan: time budget low on %s; stopping after %d scored", position_uid, out.scored)
            break

        try:
            begin = begin_scan_batch(position_uid)
        except Exception as exc:  # noqa: BLE001
            log.warning("autoscan: begin_scan_batch failed for %s: %s", position_uid, exc)
            out.errors.append(f"begin: {exc}")
            return out

        if begin.empty:
            if not out.note:
                out.note = begin.message or "no candidates"
            break

        # Defensive: if begin returns the same UIDs again (e.g. score_done lock not
        # being written for some reason), avoid an infinite loop.
        if not begin.uids:
            consecutive_empty_batches += 1
            if consecutive_empty_batches >= 2:
                break
            continue
        consecutive_empty_batches = 0

        processed: list[str] = []
        for uid in begin.uids:
            if (time.monotonic() - start) > time_remaining - 5:
                break
            try:
                summary = score_candidate_in_session(begin.session_id, uid)
            except Exception as exc:  # noqa: BLE001
                log.warning("autoscan: score failed for %s/%s: %s", position_uid, uid, exc)
                out.errors.append(f"{uid}: {exc}")
                continue
            processed.append(uid)
            if summary.error:
                out.skipped += 1
                continue
            out.scored += 1
            if summary.tag_applied:
                out.tags_applied += 1

        try:
            finish_scan_batch(begin.session_id, processed)
        except Exception as exc:  # noqa: BLE001
            log.warning("autoscan: finish failed for %s: %s", position_uid, exc)

        # If we processed fewer than the queue (time-bounded), don't keep looping.
        if len(processed) < len(begin.uids):
            break

    # Post-scan normalization: when we scored a sizeable batch, spread the
    # distribution out so the calibration UI sees a meaningful bell curve
    # instead of bunching at one rating. No-op for small batches.
    try:
        from .normalization import normalize_position_if_needed
        norm = normalize_position_if_needed(position_uid, batch_scored=out.scored)
        if norm.get("ran"):
            log.info(
                "autoscan: normalization on %s — %s; before=%s after=%s",
                position_uid, norm.get("reason"), norm.get("before"), norm.get("after"),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("autoscan: normalization failed on %s: %s", position_uid, exc)

    return out


def _serialise(result: AutoscanResult) -> dict:
    out = asdict(result)
    out["per_position"] = [asdict(p) for p in result.per_position]
    return out


def scan_one_position_now(
    position_uid: str, *, time_budget_s: float = 600.0,
) -> PositionRunResult:
    """Public wrapper around _scan_one_position for the "Scan now" button.

    Calls the same eligibility + score-and-tag pipeline the cron uses, on a
    single position, synchronously. Time-bounded; partial completion is safe
    because the score-done lock means re-clicking continues from the next
    unscored candidate.
    """
    from .comeet_client import ComeetClient

    with ComeetClient() as client:
        position = client.get_position(position_uid)
    if not position:
        raise ValueError(f"Position not found: {position_uid}")
    cls = get_position_class(position_uid)
    if not cls:
        raise ValueError("No class assigned to this position")
    return _scan_one_position(
        position_uid=position_uid,
        position_name=str(position.get("name") or position_uid),
        class_id=cls["classId"],
        time_remaining=time_budget_s,
    )


__all__ = [
    "AutoscanResult",
    "PositionRunResult",
    "run_autoscan",
    "scan_one_position_now",
    "get_last_run_log",
    "reset_cursor",
]
