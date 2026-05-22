"""Auto-learned class rubrics.

Port of LearnedRubrics.gs. For each position class with >= MIN_SAMPLES feedback
entries, ask Claude to synthesize the recruiter's patterns into a prose rubric.
The rubric is cached in Postgres (`learned_rubrics` table) and refreshed when
new feedback rows arrive (cache key = feedback row count).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from anthropic import Anthropic
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .config import settings
from .db import db_session
from .feedback import (
    feedback_count_for_class,
    list_feedback_for_class,
    list_feedback_for_position,
)
from .models import LearnedRubric

log = logging.getLogger(__name__)

MAX_RUBRIC_TOKENS = 1200  # bumped from 900 to fit the new PER-AXIS section

# Sentinel used in learned_rubrics.position_uid for the class-level rubric
# (i.e. the legacy row that pools all class feedback). Migration 0014
# backfilled NULLs to '' so the composite PK works.
CLASS_LEVEL_RUBRIC_KEY = ""

# Minimum feedback rows on a specific position before we switch from the
# class rubric to that position's bespoke rubric. Lower than the class
# minimum because position rubrics need less data to be useful (the
# narrow scope helps).
POSITION_RUBRIC_MIN_SAMPLES = 5


def get_learned_rubric_for_class(class_id: str, class_name: str) -> str:
    """Class-level rubric (legacy entry point).

    Kept for back-compat with callers that don't yet pass position context.
    New code should call `get_rubric_for_position` which tries the
    position-specific rubric first, then falls back to the class rubric.

    Empty string when there are too few feedback rows or generation fails.
    """
    if not class_id:
        return ""
    count = feedback_count_for_class(class_id)
    if count < settings.learned_rubric_min_samples:
        return ""

    cached = _read_cached_rubric(class_id, CLASS_LEVEL_RUBRIC_KEY)
    if cached and cached.feedback_count == count and cached.rubric:
        return cached.rubric

    if not settings.anthropic_api_key:
        log.warning("rubric: ANTHROPIC_API_KEY missing; falling back to stale cached rubric")
        return cached.rubric if cached else ""

    try:
        rubric_text = _regenerate_rubric(class_id, class_name)
    except Exception as exc:  # noqa: BLE001
        log.exception("rubric regeneration failed for %s: %s", class_id, exc)
        return cached.rubric if cached else ""

    if rubric_text:
        _save_rubric(class_id, CLASS_LEVEL_RUBRIC_KEY, class_name, rubric_text, count)
    return rubric_text or (cached.rubric if cached else "")


def get_rubric_for_position(
    position_uid: str,
    class_id: str,
    class_name: str,
) -> tuple[str, str]:
    """Three-layer rubric lookup: position-specific first, then class.

    Returns:
        (rubric_text, source)
        rubric_text: prose to inject in the scoring prompt; "" if nothing
            applicable.
        source: "position" | "class" | "" (none) — for logging + the
            scoring debug log so we know which layer was applied.

    Read order:
      1. learned_rubrics row at (class_id, position_uid) — bespoke rubric
         for THIS position, synthesised from this position's feedback only.
         Used when the position has accumulated >= POSITION_RUBRIC_MIN_SAMPLES
         feedback rows.
      2. learned_rubrics row at (class_id, '') — the class-level rubric
         used as cold-start fallback when no position-specific rubric
         exists yet.

    Stale-cache regeneration is opportunistic: if the cached row's
    feedback_count is behind the current count, we synthesise fresh on
    this call (slow path) and overwrite. Cheap when warm.
    """
    if not class_id:
        return ("", "")

    # ── 1. Try position-specific rubric ────────────────────────────────
    if position_uid:
        pos_feedback_count = _feedback_count_for_position(position_uid)
        if pos_feedback_count >= POSITION_RUBRIC_MIN_SAMPLES:
            cached = _read_cached_rubric(class_id, position_uid)
            if cached and cached.feedback_count == pos_feedback_count and cached.rubric:
                return (cached.rubric, "position")
            # Stale or missing — regenerate position-specific.
            if settings.anthropic_api_key:
                try:
                    text = _regenerate_position_rubric(
                        position_uid, class_id, class_name,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "position rubric regen failed for %s: %s",
                        position_uid, exc,
                    )
                    text = ""
                if text:
                    _save_rubric(
                        class_id, position_uid, class_name, text, pos_feedback_count,
                    )
                    return (text, "position")
            # Fell through — use cached even if stale, before the class
            # fallback.
            if cached and cached.rubric:
                return (cached.rubric, "position")

    # ── 2. Fall back to class-level rubric ─────────────────────────────
    class_text = get_learned_rubric_for_class(class_id, class_name)
    return (class_text, "class" if class_text else "")


def refresh_learned_rubric(class_id: str, class_name: str) -> dict:
    """Force-regenerate the class-level rubric, even if cache is fresh.

    Used by the CLI refresh-rubrics command. Position-specific rubrics
    regenerate automatically via `get_rubric_for_position` once their
    feedback_count diverges from the cache.
    """
    if not settings.anthropic_api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}
    try:
        rubric_text = _regenerate_rubric(class_id, class_name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not rubric_text:
        return {"ok": False, "error": f"insufficient feedback (< {settings.learned_rubric_min_samples})"}
    count = feedback_count_for_class(class_id)
    _save_rubric(class_id, CLASS_LEVEL_RUBRIC_KEY, class_name, rubric_text, count)
    return {
        "ok": True, "class_id": class_id, "class_name": class_name,
        "rubric_length": len(rubric_text), "feedback_count": count,
    }


def refresh_position_rubric(position_uid: str, class_id: str, class_name: str) -> dict:
    """Force-regenerate a position-specific rubric.

    Used by the batch-complete hook after a calibration batch lands a
    fresh set of feedback rows on this position.
    """
    if not settings.anthropic_api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}
    if not (position_uid and class_id):
        return {"ok": False, "error": "position_uid and class_id required"}
    pos_feedback_count = _feedback_count_for_position(position_uid)
    if pos_feedback_count < POSITION_RUBRIC_MIN_SAMPLES:
        return {
            "ok": False,
            "error": f"insufficient feedback (< {POSITION_RUBRIC_MIN_SAMPLES})",
            "feedback_count": pos_feedback_count,
        }
    try:
        text = _regenerate_position_rubric(position_uid, class_id, class_name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not text:
        return {"ok": False, "error": "rubric synthesis produced empty text"}
    _save_rubric(class_id, position_uid, class_name, text, pos_feedback_count)
    return {
        "ok": True,
        "position_uid": position_uid,
        "class_id": class_id,
        "rubric_length": len(text),
        "feedback_count": pos_feedback_count,
    }


# ─── Internals ───────────────────────────────────────────────────────────────
def _feedback_count_for_position(position_uid: str) -> int:
    """Count of valid feedback rows (both ai_rating and recruiter_rating present)
    for one position."""
    if not position_uid:
        return 0
    rows = list_feedback_for_position(position_uid)
    return sum(1 for r in rows if r.ai_rating and r.recruiter_rating)


def _read_cached_rubric(class_id: str, position_uid: str) -> LearnedRubric | None:
    """Load one rubric row by composite key (class_id, position_uid).

    Use position_uid='' (CLASS_LEVEL_RUBRIC_KEY) for the class-level row,
    or the actual position uid for the position-specific row.
    """
    with db_session() as session:
        return session.scalar(
            select(LearnedRubric).where(
                (LearnedRubric.class_id == class_id)
                & (LearnedRubric.position_uid == position_uid)
            )
        )


def _save_rubric(
    class_id: str,
    position_uid: str,
    class_name: str,
    rubric_text: str,
    feedback_count: int,
) -> None:
    """UPSERT a rubric row keyed by (class_id, position_uid).

    position_uid='' → class-level row.
    """
    with db_session() as session:
        stmt = pg_insert(LearnedRubric).values(
            class_id=class_id,
            position_uid=position_uid,
            class_name=class_name,
            generated_at=datetime.now(timezone.utc),
            feedback_count=feedback_count,
            rubric=rubric_text,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[LearnedRubric.class_id, LearnedRubric.position_uid],
            set_={
                "class_name": stmt.excluded.class_name,
                "generated_at": stmt.excluded.generated_at,
                "feedback_count": stmt.excluded.feedback_count,
                "rubric": stmt.excluded.rubric,
            },
        )
        session.execute(stmt)


def _regenerate_position_rubric(
    position_uid: str,
    class_id: str,
    class_name: str,
) -> str:
    """Call Claude to synthesise a rubric from ONE position's feedback only.

    Same prompt structure as the class-level synth, but the input pool is
    narrower — only the recruiter ratings on this specific position. The
    output is a tighter, more position-specific rubric that overrides the
    class rubric for that position only.

    Returns "" when there are too few rows. Caller is responsible for
    persisting via `_save_rubric(class_id, position_uid, ...)`.
    """
    rows = list_feedback_for_position(position_uid)
    valid = [r for r in rows if r.ai_rating and r.recruiter_rating]
    if len(valid) < POSITION_RUBRIC_MIN_SAMPLES:
        return ""
    # Reuse the synthesis machinery — only difference is the prompt
    # framing names the POSITION rather than the class, and the data
    # block is filtered.
    return _synthesise_rubric_text(
        scope_label=f'position "{class_name}" ({position_uid})',
        feedback_rows=valid,
        scope_kind="position",
    )


def _regenerate_rubric(class_id: str, class_name: str) -> str:
    """Call Claude to synthesize a rubric from the class's full feedback log.

    Recruiters tag specific broken axes on large disagreements (|delta| >= 1.5)
    via the calibration follow-up modal. We surface both the per-row tags
    AND a class-level tally so the LLM can anchor per-axis corrections
    instead of just one global "AI over-rates" pattern. That's the whole
    point of multi-select axis tagging: targeted rubric updates.
    """
    rows = list_feedback_for_class(class_id)
    valid = [r for r in rows if r.ai_rating and r.recruiter_rating]
    if len(valid) < settings.learned_rubric_min_samples:
        return ""
    return _synthesise_rubric_text(
        scope_label=f'position class "{class_name}"',
        feedback_rows=valid,
        scope_kind="class",
    )


def _synthesise_rubric_text(
    *,
    scope_label: str,
    feedback_rows: list,
    scope_kind: str,
) -> str:
    """Shared synthesis path for class + position rubrics.

    `scope_label` flows into the prompt header.
    `scope_kind`: "class" | "position" — affects guidance wording.
    Returns the rubric prose or "" on failure.
    """
    valid = feedback_rows

    # Largest disagreements first (most informative), then newest.
    valid.sort(key=lambda r: (-r.margin, -r.timestamp.timestamp()))

    # Aggregate per-axis disagreement counts across the whole class — feeds
    # the "PER-AXIS CALIBRATION" section of the rubric. We count each axis
    # once per feedback row that flagged it; direction (AI over- vs
    # under-rated) is inferred from the sign of (recruiter - ai).
    axis_tally: dict[str, dict[str, int]] = {}
    for row in valid:
        axes = getattr(row, "broken_axes", None) or []
        if not axes:
            continue
        # Sign of the global delta tells us whether AI was high or low
        # on this candidate. We attribute that direction to every tagged
        # axis (the recruiter said "the AI broke ON these axes" — same
        # direction as the overall miss).
        if row.ai_rating is None or row.recruiter_rating is None:
            continue
        ai_too_high = row.ai_rating > row.recruiter_rating
        ai_too_low = row.ai_rating < row.recruiter_rating
        for ax in axes:
            slot = axis_tally.setdefault(ax, {"over": 0, "under": 0, "n": 0})
            slot["n"] += 1
            if ai_too_high:
                slot["over"] += 1
            elif ai_too_low:
                slot["under"] += 1

    feedback_lines: list[str] = []
    for idx, row in enumerate(valid, start=1):
        line = f"{idx}. {row.candidate_name or 'Candidate'}"
        if row.position_name:
            line += f" [{row.position_name}]"
        line += f" — AI: {row.ai_rating} → Recruiter: {row.recruiter_rating}"
        if row.margin >= 2:
            line += f" (BIG MISS, margin {row.margin})"
        axes = getattr(row, "broken_axes", None) or []
        if axes:
            line += f"\n   Recruiter flagged broken axes: {', '.join(axes)}"
        if row.note:
            note = " ".join(row.note.split())[:280]
            line += f'\n   Note: "{note}"'
        feedback_lines.append(line)

    # Build the per-axis tally block. Sorted by frequency desc so the
    # most-broken axes lead.
    axis_summary_block = ""
    if axis_tally:
        ordered = sorted(axis_tally.items(), key=lambda kv: -kv[1]["n"])
        lines = []
        for ax, stats in ordered:
            direction_parts = []
            if stats["over"]:
                direction_parts.append(f"AI over-rated in {stats['over']}")
            if stats["under"]:
                direction_parts.append(f"AI under-rated in {stats['under']}")
            direction = "; ".join(direction_parts) if direction_parts else "mixed"
            lines.append(f"  - {ax}: flagged in {stats['n']} verdicts ({direction})")
        axis_summary_block = (
            "\nPER-AXIS DISAGREEMENT TALLY (recruiter explicitly tagged these axes "
            "as wrong on large disagreements):\n" + "\n".join(lines) + "\n"
        )

    scope_hint = (
        "This rubric will apply to all positions in this class as a "
        "cold-start baseline."
        if scope_kind == "class"
        else "This rubric will apply ONLY to this specific position. Lean "
        "into the position-specific patterns the recruiter has shown."
    )

    prompt = (
        f"You are analysing recruiter feedback for {scope_label} to derive "
        f"a scoring rubric the AI screener should follow. {scope_hint}\n\n"
        f"Below are {len(valid)} candidate evaluations: AI gave a rating; the recruiter then "
        "gave their own rating. The recruiter is the ground truth — your job is to synthesise their "
        "judgement into a rubric the AI can apply on future candidates.\n"
        + axis_summary_block +
        "\nFormat your output as PROSE under FIVE headings:\n\n"
        "1) STRONG SIGNAL (8-10 territory): the patterns the recruiter rewards. Be SPECIFIC — quote "
        'concrete patterns from the notes (e.g. "led migration of monolith to microservices", '
        '"shipped revenue features with measurable lift") not vague platitudes ("strong communication").\n\n'
        "2) WEAK SIGNAL (1-3 territory): the patterns the recruiter rejects. Again, specific phrases "
        "from the notes wherever possible.\n\n"
        "3) BORDERLINE (4-6 territory): the candidates that lean on judgement — what tips them either way.\n\n"
        "4) AI BIAS CORRECTIONS (holistic): where the AI tends to misjudge overall (over-rate vs under-rate). "
        'Anchor each bias to specific examples by name from the feedback. State the correction concretely '
        '(e.g. "When candidate has X without Y, AI tends to rate 7 but recruiter rates 4 — correct '
        "this pattern by …\").\n\n"
        "5) PER-AXIS CALIBRATION: use the PER-AXIS DISAGREEMENT TALLY above. For each axis the "
        "recruiter flagged, write a SPECIFIC anchor correction. Skip axes that weren't flagged. "
        'Example: "company_domain — recruiter flagged 18 of 25 verdicts as over-rated. The AI '
        'counts general-SaaS companies as 7-8 but recruiter caps them at 5-6 unless they\'re creator-tools. '
        'Anchor: SaaS companies that don\'t make creator/video/audio tools max out at 6 on company_domain."\n\n'
        "Maximum 700 words. Plain prose. No JSON, no markdown headings, just the five numbered sections.\n\n"
        "FEEDBACK DATA:\n" + "\n".join(feedback_lines)
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    log.info(
        "rubric: synthesising (%s scope) %s with %d entries",
        scope_kind, scope_label, len(valid),
    )
    msg = client.messages.create(
        model=settings.claude_model,
        max_tokens=MAX_RUBRIC_TOKENS,
        temperature=0.3,
        system="You synthesise recruiter feedback into actionable scoring rubrics for an AI screener.",
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    return text


__all__ = [
    "get_learned_rubric_for_class",
    "get_rubric_for_position",
    "refresh_learned_rubric",
    "refresh_position_rubric",
    "POSITION_RUBRIC_MIN_SAMPLES",
    "CLASS_LEVEL_RUBRIC_KEY",
]
