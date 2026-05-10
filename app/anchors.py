"""Per-candidate calibration anchors.

Port of SimilarPastCandidates.gs. For each candidate being scored, fetches the
top-K most-relevant past-rated candidates and formats them as a prompt block:
  - Tier 0: same candidate (highest priority — past ratings of THIS person)
  - Tier 1: same position
  - Tier 2: same class

Within each tier, sort by largest disagreement margin (most informative for
calibration), then newest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .feedback import FeedbackEntry, list_feedback_for_class

log = logging.getLogger(__name__)

DEFAULT_MAX_ANCHORS = 4
CRITICAL_MARGIN = 2


@dataclass
class Anchor:
    """One calibration anchor — a past-rated candidate similar to the current one."""
    candidate_name: str
    candidate_uid: str
    position_name: str
    position_uid: str
    ai_rating: int
    recruiter_rating: int
    margin: int
    note: str
    tier: str  # 'same_candidate' | 'same_position' | 'same_class'

    @property
    def is_critical(self) -> bool:
        return self.margin >= CRITICAL_MARGIN


def get_anchors_for_candidate(
    *,
    class_id: str,
    position_uid: str,
    candidate_uid: str,
    max_anchors: int = DEFAULT_MAX_ANCHORS,
) -> list[Anchor]:
    """Tiered ranking: same-candidate first, then same-position, then same-class."""
    if not class_id:
        return []
    rows = list_feedback_for_class(class_id)
    if not rows:
        return []

    same_candidate: list[FeedbackEntry] = []
    same_position: list[FeedbackEntry] = []
    same_class: list[FeedbackEntry] = []

    for row in rows:
        if row.ai_rating is None or row.recruiter_rating is None:
            continue
        if candidate_uid and row.candidate_uid == candidate_uid:
            same_candidate.append(row)
        elif position_uid and row.position_uid == position_uid:
            same_position.append(row)
        else:
            same_class.append(row)

    # Same-candidate: newest first (latest correction wins).
    same_candidate.sort(key=lambda r: r.timestamp, reverse=True)
    # Other tiers: biggest margin first, then newest.
    same_position.sort(key=lambda r: (-r.margin, -r.timestamp.timestamp()))
    same_class.sort(key=lambda r: (-r.margin, -r.timestamp.timestamp()))

    picked: list[Anchor] = []
    for entries, tier in (
        (same_candidate, "same_candidate"),
        (same_position, "same_position"),
        (same_class, "same_class"),
    ):
        for row in entries:
            if len(picked) >= max_anchors:
                break
            picked.append(_to_anchor(row, tier))
        if len(picked) >= max_anchors:
            break
    return picked


def format_anchors_for_prompt(anchors: list[Anchor]) -> str:
    """Format anchors as a high-authority prompt block for the scoring call."""
    if not anchors:
        return ""
    lines = [
        "══════════════════════════════════════════════════════════",
        "CALIBRATION ANCHORS (specific past candidates the recruiter has rated)",
        "══════════════════════════════════════════════════════════",
        "These are real prior cases — AI gave a rating, the recruiter then corrected it.",
        "Compare the CURRENT candidate to each anchor before rating. If the current",
        "candidate strongly resembles an anchor, your rating should match the recruiter's",
        "rating for that anchor (not the AI's original rating).",
        "",
    ]
    for anchor in anchors:
        head = f"— {anchor.candidate_name or 'Candidate'}"
        if anchor.position_name:
            head += f" [{anchor.position_name}]"
        if anchor.tier == "same_position":
            head += " [SAME POSITION AS CURRENT]"
        elif anchor.tier == "same_candidate":
            head += " [SAME CANDIDATE]"
        if anchor.is_critical:
            head += f" [CRITICAL: margin {anchor.margin}]"
        lines.append(head)
        lines.append(f"   AI rated: {anchor.ai_rating}  →  Recruiter rated: {anchor.recruiter_rating}")
        if anchor.note:
            note = " ".join(anchor.note.split())[:320]
            lines.append(f'   Recruiter said: "{note}"')
        lines.append("")
    lines.append("══════════════════════════════════════════════════════════")
    lines.append(
        "CALIBRATION RULE: Anchors marked [CRITICAL] are cases where you (the AI) were "
        "most wrong historically. When the current candidate matches one of those patterns, "
        "your rating should DEFAULT to the recruiter's rating for that anchor. Only deviate "
        "when there is concrete, candidate-specific evidence to do so.\n"
    )
    return "\n".join(lines)


def _to_anchor(row: FeedbackEntry, tier: str) -> Anchor:
    return Anchor(
        candidate_name=row.candidate_name,
        candidate_uid=row.candidate_uid,
        position_name=row.position_name,
        position_uid=row.position_uid,
        ai_rating=row.ai_rating or 0,
        recruiter_rating=row.recruiter_rating or 0,
        margin=row.margin,
        note=row.note,
        tier=tier,
    )


__all__ = ["Anchor", "get_anchors_for_candidate", "format_anchors_for_prompt"]
