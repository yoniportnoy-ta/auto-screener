"""One-shot importer for the legacy Apps Script auto-screener data.

What we migrate:
  * Per-class feedback tabs from the "Screener Feedback" Google Sheet
    (export each tab as CSV and put them all in one folder).
  * Position-class assignments from Apps Script properties (a JSON dump
    keyed by position_uid).

Idempotent: re-running won't duplicate feedback (we de-dupe by timestamp +
candidate_uid), and re-importing position-class assignments simply UPSERTs.

Usage from the Render web service Shell:

    # 1. Upload your CSV exports + class-map JSON to /tmp:
    #    (use Render's File-Browser, scp, or paste-via-cat heredoc)
    ls /tmp/feedback_csvs/                  # tab name → CSV file
    cat /tmp/position_classes.json          # {"<uid>": {"classId":"backend","level":""}, ...}

    # 2. Run the imports:
    python -m app.migrate_apps_script feedback /tmp/feedback_csvs
    python -m app.migrate_apps_script classes  /tmp/position_classes.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import db_session
from .logging_config import configure_logging
from .models import Feedback, PositionClass
from .position_classes import DEFAULT_CLASSES, list_all_classes

log = logging.getLogger(__name__)

# Apps Script feedback sheet column order — see Code.gs FEEDBACK_COLS_:
#   Timestamp | RecruiterEmail | PositionUID | PositionName | CandidateUID
#   | CandidateName | AIRating | Verdict | Note
EXPECTED_HEADERS = (
    "Timestamp", "RecruiterEmail", "PositionUID", "PositionName",
    "CandidateUID", "CandidateName", "AIRating", "Verdict", "Note",
)


@dataclass
class ImportSummary:
    files_processed: int = 0
    rows_seen: int = 0
    rows_imported: int = 0
    rows_skipped: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def bump_skip(self, reason: str) -> None:
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1
        self.rows_skipped += 1


# ─── Feedback CSV importer ───────────────────────────────────────────────────
def import_feedback_csvs(directory: Path) -> ImportSummary:
    """Read every *.csv in `directory`. Each filename should match the class tab name.

    e.g. "Backend.csv", "Product Management.csv". The filename (sans extension) becomes
    `class_name`; we look up `class_id` by exact-name match in our class catalogue.
    """
    summary = ImportSummary()
    if not directory.is_dir():
        summary.errors.append(f"not a directory: {directory}")
        return summary

    name_to_id = {c["name"]: c["id"] for c in list_all_classes()}
    # Also accept legacy-style filenames that include the class id as fallback.
    id_to_id = {c["id"]: c["id"] for c in list_all_classes()}

    for path in sorted(directory.glob("*.csv")):
        summary.files_processed += 1
        stem = path.stem.strip()
        class_id = name_to_id.get(stem) or id_to_id.get(stem.lower())
        class_name = stem
        if class_id is None:
            log.warning("import_feedback: %s — class %r not in catalogue; rows will use class_id='general'", path.name, stem)
            class_id = "general"
        log.info("importing %s as class_id=%s class_name=%s", path.name, class_id, class_name)

        try:
            with path.open("r", newline="", encoding="utf-8-sig") as fh:
                reader = csv.reader(fh)
                rows = list(reader)
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(f"{path.name}: read failed: {exc}")
            continue

        if not rows or rows[0] != list(EXPECTED_HEADERS):
            summary.errors.append(
                f"{path.name}: header mismatch (expected {EXPECTED_HEADERS}, got {rows[0] if rows else '<empty>'})"
            )
            continue

        for raw in rows[1:]:
            summary.rows_seen += 1
            if not raw:
                continue
            row = (raw + [""] * len(EXPECTED_HEADERS))[: len(EXPECTED_HEADERS)]
            ts_raw, recruiter_email, pos_uid, pos_name, cand_uid, cand_name, ai_str, rec_str, note = row
            if not (cand_uid and pos_uid):
                summary.bump_skip("missing candidate or position uid")
                continue

            ai_rating = _coerce_rating(ai_str)
            rec_rating = _coerce_rating(rec_str)
            if rec_rating is None and ai_rating is None:
                summary.bump_skip("no ratings")
                continue

            ts = _parse_iso(ts_raw)
            if _feedback_already_imported(ts, cand_uid, rec_rating):
                summary.bump_skip("duplicate")
                continue

            try:
                with db_session() as ses:
                    ses.add(Feedback(
                        timestamp=ts or datetime.now(timezone.utc),
                        recruiter_email=(recruiter_email or "").strip()[:200],
                        class_id=class_id,
                        class_name=class_name,
                        position_uid=pos_uid.strip(),
                        position_name=(pos_name or "").strip(),
                        candidate_uid=cand_uid.strip(),
                        candidate_name=(cand_name or "").strip(),
                        ai_rating=ai_rating,
                        recruiter_rating=rec_rating,
                        note=(note or "").strip()[:2000],
                    ))
                summary.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                summary.errors.append(f"{path.name}:{summary.rows_seen}: insert failed: {exc}")

    return summary


# ─── Position-class JSON importer ────────────────────────────────────────────
def import_position_classes(json_path: Path) -> ImportSummary:
    """Read a JSON file shaped like:

        {
          "<position_uid>": {"classId": "backend", "className": "Backend", "level": ""},
          ...
        }

    Apps Script stored these in `SCREENER_POS_CLASS_<uid>` script properties; export
    them via `PropertiesService.getScriptProperties().getProperties()` and post-process
    into this shape. UPSERTs into position_classes.
    """
    summary = ImportSummary()
    if not json_path.is_file():
        summary.errors.append(f"not a file: {json_path}")
        return summary

    try:
        data = json.loads(json_path.read_text())
    except Exception as exc:  # noqa: BLE001
        summary.errors.append(f"json parse: {exc}")
        return summary

    if not isinstance(data, dict):
        summary.errors.append("expected JSON object keyed by position_uid")
        return summary

    catalogue_ids = {c["id"] for c in list_all_classes()}
    name_lookup = {c["id"]: c["name"] for c in list_all_classes()}
    custom_seen: set[str] = set()  # avoid duplicate custom-class inserts in one run

    for position_uid, payload in data.items():
        summary.rows_seen += 1
        if not isinstance(payload, dict):
            summary.bump_skip("payload not an object")
            continue
        class_id = (payload.get("classId") or payload.get("class_id") or "").strip()
        class_name = (
            payload.get("className") or payload.get("class_name") or name_lookup.get(class_id) or ""
        ).strip()
        level = (payload.get("level") or "").strip() or None
        if not class_id or not class_name:
            summary.bump_skip("missing class_id or class_name")
            continue

        # Auto-register classes that aren't in the default catalogue (e.g. legacy
        # values like "it", "nlp", "talent_acquisition" that the recruiter created
        # in the Apps Script version). Otherwise the UI dropdown wouldn't show them.
        if class_id not in catalogue_ids and class_id not in custom_seen:
            _ensure_custom_class(class_id, class_name)
            custom_seen.add(class_id)

        with db_session() as ses:
            stmt = pg_insert(PositionClass).values(
                position_uid=position_uid.strip(),
                class_id=class_id,
                class_name=class_name,
                level=level,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[PositionClass.position_uid],
                set_={
                    "class_id": stmt.excluded.class_id,
                    "class_name": stmt.excluded.class_name,
                    "level": stmt.excluded.level,
                },
            )
            ses.execute(stmt)
        summary.rows_imported += 1

    return summary


def _ensure_custom_class(class_id: str, class_name: str) -> None:
    """Insert a custom class row if not already present. Used by the position-class
    importer to register classes that exist in legacy data but not in the catalogue."""
    from .models import CustomPositionClass

    with db_session() as ses:
        existing = ses.scalar(
            select(CustomPositionClass).where(CustomPositionClass.class_id == class_id)
        )
        if existing:
            return
        stmt = pg_insert(CustomPositionClass).values(
            class_id=class_id, class_name=class_name, levels_json=[],
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=[CustomPositionClass.class_id])
        ses.execute(stmt)
    log.info("auto-registered custom class: id=%s name=%s", class_id, class_name)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _coerce_rating(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        n = int(float(s))
        if 1 <= n <= 5:
            return n
    except (ValueError, TypeError):
        pass
    return None


def _parse_iso(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _feedback_already_imported(ts: datetime | None, candidate_uid: str, recruiter_rating: int | None) -> bool:
    """Cheap de-dupe: same (timestamp, candidate, recruiter rating) tuple."""
    if ts is None:
        return False
    with db_session() as ses:
        existing = ses.scalar(
            select(Feedback).where(
                Feedback.timestamp == ts,
                Feedback.candidate_uid == candidate_uid,
                Feedback.recruiter_rating == recruiter_rating,
            )
        )
        return existing is not None


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="app.migrate_apps_script")
    sub = parser.add_subparsers(dest="cmd", required=True)
    fb = sub.add_parser("feedback", help="import feedback CSVs from a directory")
    fb.add_argument("directory")
    cls = sub.add_parser("classes", help="import position-class assignments from a JSON dump")
    cls.add_argument("json_path")
    args = parser.parse_args()

    if args.cmd == "feedback":
        summary = import_feedback_csvs(Path(args.directory))
    elif args.cmd == "classes":
        summary = import_position_classes(Path(args.json_path))
    else:
        return 2

    log.info(
        "migrate %s done: files=%d seen=%d imported=%d skipped=%d errors=%d",
        args.cmd, summary.files_processed, summary.rows_seen, summary.rows_imported,
        summary.rows_skipped, len(summary.errors),
    )
    if summary.skipped_reasons:
        log.info("skipped breakdown: %s", summary.skipped_reasons)
    if summary.errors:
        for err in summary.errors[:20]:
            log.warning("  err: %s", err)
        if len(summary.errors) > 20:
            log.warning("  … (%d more errors elided)", len(summary.errors) - 20)
    return 0 if not summary.errors else 1


if __name__ == "__main__":
    sys.exit(main())
