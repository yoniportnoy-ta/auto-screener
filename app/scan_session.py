"""Scan-session bookkeeping.

Mirrors the CacheService-backed session in beginScanBatch / scoreScanSessionCandidate /
finishScanBatch. We back the session with `candidate_locks` keyed by `scan_session:<id>`
so it survives request boundaries without needing Redis.

A session contains:
  - position_uid + position_name + class_id + class_name
  - the ordered list of UIDs to score
  - last_review cursor before the scan (for rollback / "next page" pagination)
  - feedback narrative + JD text snapshots (stored separately when large)
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import db_session
from .models import CandidateLock

log = logging.getLogger(__name__)

SESSION_KEY_PREFIX = "scan_session:"
SESSION_TTL_SECONDS = 6 * 3600


@dataclass
class ScanSession:
    session_id: str
    position_uid: str
    position_name: str
    class_id: str
    class_name: str
    uids: list[str]
    last_review_before: str | None = None
    pending_new_count: int = 0
    capped: bool = False
    remaining_new_count: int = 0
    batch_size: int = 0
    jd_text: str = ""
    feedback_context: str = ""
    position_notes: str = ""
    recruiter_notes: str = ""
    created_at: float = field(default_factory=lambda: time.time())

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "ScanSession":
        return cls(**json.loads(raw))


def new_session_id() -> str:
    return secrets.token_hex(12)


def save_session(session: ScanSession) -> None:
    key = SESSION_KEY_PREFIX + session.session_id
    with db_session() as ses:
        stmt = pg_insert(CandidateLock).values(key=key, value=session.to_json())
        stmt = stmt.on_conflict_do_update(
            index_elements=[CandidateLock.key],
            set_={"value": stmt.excluded.value, "updated_at": datetime.now(timezone.utc)},
        )
        ses.execute(stmt)


def load_session(session_id: str) -> ScanSession | None:
    key = SESSION_KEY_PREFIX + (session_id or "").strip()
    with db_session() as ses:
        row = ses.scalar(select(CandidateLock).where(CandidateLock.key == key))
        if not row or not row.value:
            return None
        try:
            sess = ScanSession.from_json(row.value)
        except Exception as exc:  # noqa: BLE001
            log.warning("scan_session: unparseable row for %s: %s", session_id, exc)
            return None
        # Best-effort TTL: prune sessions older than 6h.
        if sess.created_at and (time.time() - sess.created_at) > SESSION_TTL_SECONDS:
            log.info("scan_session: %s expired by TTL", session_id)
            delete_session(session_id)
            return None
        return sess


def delete_session(session_id: str) -> None:
    key = SESSION_KEY_PREFIX + (session_id or "").strip()
    with db_session() as ses:
        ses.query(CandidateLock).filter(CandidateLock.key == key).delete()


__all__ = ["ScanSession", "new_session_id", "save_session", "load_session", "delete_session"]
