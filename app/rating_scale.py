"""Internal rating scale = 1-10. External (Comeet tag names) = 1-5.

Everything inside this codebase — DebugScoring rows, calibration verdicts,
recruiter thresholds, admin floor, UI display — uses a 10-point scale
because that gives Claude room to distinguish a "6" from a "7" instead of
clustering at 4/5.

The Comeet TAGS we apply still use 5 names (AI: Way Off / Not a Fit /
Maybe / Great / Superstar) because (a) recruiters got used to the 1-5
ladder and (b) 10 tag names in Comeet would be cluttered. We convert
1-10 → 1-5 at the tagging boundary only.

Mapping is straight halving with a slight asymmetry at the top so 10
maps to 5 cleanly (not 5.5):

    internal (1-10)  →  external (1-5)
    1, 2             →  1
    3, 4             →  2
    5, 6             →  3
    7, 8             →  4
    9, 10            →  5

Edge cases:
    None  →  None        (no rating to convert)
    < 1   →  clamped 1
    > 10  →  clamped 10
"""
from __future__ import annotations


# Single source of truth for the internal scale boundaries.
INTERNAL_MIN = 1
INTERNAL_MAX = 10

# 5-scale (kept for Comeet tag names + legacy interfaces).
EXTERNAL_MIN = 1
EXTERNAL_MAX = 5


def internal_to_external(internal: int | None) -> int | None:
    """Map an internal 1-10 rating to a 1-5 rating for Comeet tagging.

    Pair-of-two mapping: 1-2→1, 3-4→2, 5-6→3, 7-8→4, 9-10→5.
    None passes through. Out-of-range values are clamped.
    """
    if internal is None:
        return None
    try:
        n = int(internal)
    except (TypeError, ValueError):
        return None
    n = max(INTERNAL_MIN, min(INTERNAL_MAX, n))
    # 1→1, 2→1, 3→2, 4→2, ... 9→5, 10→5
    return (n + 1) // 2


def clamp_internal(rating: int | float | None) -> int | None:
    """Coerce a rating into the internal 1-10 range. Used when parsing
    Claude's response — it might return out-of-range or float ratings.
    """
    if rating is None:
        return None
    try:
        n = int(round(float(rating)))
    except (TypeError, ValueError):
        return None
    return max(INTERNAL_MIN, min(INTERNAL_MAX, n))


__all__ = [
    "INTERNAL_MIN",
    "INTERNAL_MAX",
    "EXTERNAL_MIN",
    "EXTERNAL_MAX",
    "internal_to_external",
    "clamp_internal",
]
