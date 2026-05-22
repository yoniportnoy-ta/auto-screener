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
from .feedback import feedback_count_for_class, list_feedback_for_class
from .models import LearnedRubric

log = logging.getLogger(__name__)

MAX_RUBRIC_TOKENS = 1200  # bumped from 900 to fit the new PER-AXIS section


def get_learned_rubric_for_class(class_id: str, class_name: str) -> str:
    """Returns the rubric text for a class, regenerating if stale.

    Empty string when there are too few feedback rows or generation fails.
    """
    if not class_id:
        return ""
    count = feedback_count_for_class(class_id)
    if count < settings.learned_rubric_min_samples:
        return ""

    cached = _read_cached_rubric(class_id)
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
        _save_rubric(class_id, class_name, rubric_text, count)
    return rubric_text or (cached.rubric if cached else "")


def refresh_learned_rubric(class_id: str, class_name: str) -> dict:
    """Force-regenerate, even if cache is fresh. Returns a result dict."""
    if not settings.anthropic_api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}
    try:
        rubric_text = _regenerate_rubric(class_id, class_name)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not rubric_text:
        return {"ok": False, "error": f"insufficient feedback (< {settings.learned_rubric_min_samples})"}
    count = feedback_count_for_class(class_id)
    _save_rubric(class_id, class_name, rubric_text, count)
    return {
        "ok": True, "class_id": class_id, "class_name": class_name,
        "rubric_length": len(rubric_text), "feedback_count": count,
    }


# ─── Internals ───────────────────────────────────────────────────────────────
def _read_cached_rubric(class_id: str) -> LearnedRubric | None:
    with db_session() as session:
        return session.scalar(select(LearnedRubric).where(LearnedRubric.class_id == class_id))


def _save_rubric(class_id: str, class_name: str, rubric_text: str, feedback_count: int) -> None:
    with db_session() as session:
        stmt = pg_insert(LearnedRubric).values(
            class_id=class_id,
            class_name=class_name,
            generated_at=datetime.now(timezone.utc),
            feedback_count=feedback_count,
            rubric=rubric_text,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[LearnedRubric.class_id],
            set_={
                "class_name": stmt.excluded.class_name,
                "generated_at": stmt.excluded.generated_at,
                "feedback_count": stmt.excluded.feedback_count,
                "rubric": stmt.excluded.rubric,
            },
        )
        session.execute(stmt)


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

    prompt = (
        f'You are analysing recruiter feedback for the position class "{class_name}" to derive '
        f"a scoring rubric the AI screener should follow.\n\n"
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
    log.info("rubric: regenerating for class=%s with %d entries", class_id, len(valid))
    msg = client.messages.create(
        model=settings.claude_model,
        max_tokens=MAX_RUBRIC_TOKENS,
        temperature=0.3,
        system="You synthesise recruiter feedback into actionable scoring rubrics for an AI screener.",
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    return text


__all__ = ["get_learned_rubric_for_class", "refresh_learned_rubric"]
