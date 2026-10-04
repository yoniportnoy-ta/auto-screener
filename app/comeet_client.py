"""Comeet access for the screener.

The client itself now lives in `comeet-core`, shared with pipey and every other
service that talks to Comeet — see that package's QUIRKS.md before changing
anything about transport, auth or pagination.

What stays here is POLICY: the two questions whose answer depends on this
service's configuration rather than on Comeet's data shape. `excluded_statuses`
and `step_substrings` are screener settings; another service reading the same
candidate would answer differently.

Everything else is re-exported so call sites did not have to move.
"""
from __future__ import annotations

from typing import Any

from comeet import (
    ComeetBandwidthError,
    ComeetClient,
    ComeetError,
    ComeetTransientError,
    mint_token,
)
from comeet.fields import (
    candidate_full_name,
    candidate_max_activity_iso,
    position_country,
    position_full_location,
    position_jd_text,
    position_lead_recruiter,
    position_recruiter_notes,
)

from .config import settings

__all__ = [
    "ComeetClient",
    "ComeetError",
    "ComeetTransientError",
    "ComeetBandwidthError",
    "mint_token",
    "candidate_full_name",
    "candidate_max_activity_iso",
    "position_country",
    "position_full_location",
    "position_jd_text",
    "position_lead_recruiter",
    "position_recruiter_notes",
    "candidate_active_for_screening",
    "candidate_in_allowed_step",
]


# ─── Policy — screener configuration, not Comeet shape ───────────────────────
def candidate_active_for_screening(candidate: dict[str, Any]) -> bool:
    """False when recruiting status is one of the excluded values (Rejected etc.)."""
    excluded = settings.excluded_statuses_list
    if not excluded:
        return True
    status = (candidate.get("status") or "").strip().lower()
    if not status:
        return True  # blank status: let the profile fetch decide
    return status not in excluded


def candidate_in_allowed_step(candidate: dict[str, Any]) -> bool:
    """True if any current step name/type substring-matches the configured patterns."""
    steps = candidate.get("current_steps") or []
    if not steps:
        return False
    patterns = settings.step_substrings_list
    if patterns == ["*"]:
        return True
    for step in steps:
        haystack = f"{step.get('name', '')} {step.get('type', '')}".lower()
        for pattern in patterns:
            if pattern in haystack:
                return True
    return False


