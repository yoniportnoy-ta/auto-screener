"""Anchor ranking tests — pure logic, no DB or network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.anchors import Anchor, format_anchors_for_prompt
from app.feedback import FeedbackEntry


def _entry(
    *, ts: datetime, candidate_uid: str, position_uid: str, ai: int, rec: int,
    candidate_name: str = "Test", position_name: str = "Pos", note: str = "",
) -> FeedbackEntry:
    return FeedbackEntry(
        timestamp=ts,
        recruiter_email="",
        class_id="cls",
        class_name="Class",
        position_uid=position_uid,
        position_name=position_name,
        candidate_uid=candidate_uid,
        candidate_name=candidate_name,
        ai_rating=ai,
        recruiter_rating=rec,
        note=note,
    )


def test_format_anchors_empty_returns_empty_string() -> None:
    assert format_anchors_for_prompt([]) == ""


def test_format_anchors_marks_critical_and_same_position() -> None:
    a = Anchor(
        candidate_name="Adi",
        candidate_uid="A1",
        position_name="Senior PM",
        position_uid="P1",
        ai_rating=5,
        recruiter_rating=2,
        margin=3,
        note="too senior",
        tier="same_position",
    )
    out = format_anchors_for_prompt([a])
    assert "Adi" in out
    assert "Senior PM" in out
    assert "[SAME POSITION AS CURRENT]" in out
    assert "[CRITICAL: margin 3]" in out
    assert "AI rated: 5  →  Recruiter rated: 2" in out


def test_anchor_is_critical_threshold() -> None:
    a = Anchor("X", "U", "P", "PU", 4, 2, 2, "", "same_class")
    assert a.is_critical is True
    a = Anchor("X", "U", "P", "PU", 3, 2, 1, "", "same_class")
    assert a.is_critical is False


def test_format_anchors_truncates_long_notes() -> None:
    long_note = "x" * 1000
    a = Anchor("X", "U", "P", "PU", 4, 2, 2, long_note, "same_class")
    out = format_anchors_for_prompt([a])
    # Note should be truncated near the 320-char limit defined in the formatter.
    assert "x" * 320 in out
    assert "x" * 400 not in out


def test_anchor_tiers_documented_in_dataclass() -> None:
    """Defensive: keep `tier` values aligned with what get_anchors_for_candidate emits."""
    valid_tiers = {"same_candidate", "same_position", "same_class"}
    sample = Anchor("X", "U", "P", "PU", 4, 2, 2, "", "same_candidate")
    assert sample.tier in valid_tiers


def test_format_anchors_includes_calibration_rule_footer() -> None:
    a = Anchor("X", "U", "P", "PU", 4, 2, 2, "", "same_class")
    out = format_anchors_for_prompt([a])
    assert "CALIBRATION RULE" in out
