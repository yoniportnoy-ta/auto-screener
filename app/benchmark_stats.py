"""Shared calibration / benchmark statistics.

Centralises every metric we surface in the benchmark CLI, the per-batch
mini-chart (`/api/calibration/batch-complete`), and the meta-benchmark
(`/api/calibration/finalize`). One source of truth means the CLI output,
the round-summary UI, and the meta-benchmark chart can never disagree
on what "agreement" or "bias" means for a given set of verdicts.

Metrics included
─────────────────
- count / clean_count / dirty_count (data quality split)
- mean_ai / mean_recruiter / bias (AI − recruiter)
- std_ai / std_recruiter / discrimination_ratio
    AI σ divided by recruiter σ. When this drops below ~0.5 it means the
    AI has collapsed onto a single value (e.g. always returning 3) while
    the recruiter is still discriminating — a Goodhart's-law failure
    where the bias metric looks great because the AI stopped trying.
    Surfaced on 76.85A trial: R5 had AI σ=0.0 vs Rec σ=1.1 even though
    bias was 0.0.
- mae / rmse / within_1_pct / within_2_pct
- false_negatives / false_positives
    FN = AI≤3 AND Rec≥6 (recruiter would interview, AI would reject —
    the most expensive asymmetric miss).
    FP = AI≥7 AND Rec≤3 (AI would advance, recruiter would reject — wastes
    interview slots).
- per_axis (broken_axes_json tallies)
- agreement_pct under the supplied threshold

Dirty-verdict filter
────────────────────
Any verdict whose feedback_text matches the error pattern (stale
enrichment cache, transient 404s from a deprecated model id, etc.) is
moved into the `dirty` bucket and EXCLUDED from every numeric stat —
including count. The dirty count is surfaced separately so the UI can
nudge the operator to investigate. Precedent: verdict #324 on 76.85A
had `model: claude-3-5-haiku-20241022` in feedback_text because the
candidate_enrichment cache held a row from before the model fix; that
single bad row would otherwise pollute the round's bias by +1.5.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


# Verdicts with these patterns in feedback_text are data-quality casualties
# from upstream failures (stale enrichment cache, deprecated model ids,
# transient Anthropic 5xx). Treat them as missing data — exclude from every
# numeric stat but count them in `dirty_count` so we can surface the issue.
_DIRTY_PATTERN = re.compile(
    r"error code|extraction failed|not_found_error|"
    r"model:\s*claude-|invalid_request_error|api[_\s-]?error",
    re.IGNORECASE,
)


# False-negative / false-positive boundaries. Asymmetric on purpose:
# FN is the expensive miss because the recruiter would have interviewed —
# the AI rejected a real candidate. FP is the cheaper-but-still-wasteful
# miss where we'd burn an interview slot the recruiter would have skipped.
# Boundaries match the new rating_to_verdict cut (4→down, 7+→up).
FN_AI_MAX = 3
FN_REC_MIN = 6
FP_AI_MIN = 7
FP_REC_MAX = 3


@dataclass
class VerdictRow:
    """Lightweight container so callers can hand us either ORM rows or dicts.

    `feedback_text` is checked against `_DIRTY_PATTERN`; rows that match
    are moved into the dirty bucket and ignored for stats.
    """
    ai_rating: int | None
    recruiter_rating: int | None
    verdict: str | None = None
    feedback_text: str | None = None
    broken_axes: list[str] | None = None
    round_num: int | None = None
    candidate_uid: str | None = None


def is_dirty_feedback(text: str | None) -> bool:
    """Pure helper — does this feedback_text look like a captured error
    string from a failed upstream call? Used by both the round summary
    and the meta-benchmark; exposed for tests."""
    if not text:
        return False
    return bool(_DIRTY_PATTERN.search(text))


def _to_rows(items: Iterable[Any]) -> list[VerdictRow]:
    """Normalise heterogeneous inputs (ORM rows, dicts) into VerdictRow.

    We accept anything with `ai_rating` + `recruiter_rating` attributes
    or matching dict keys. `broken_axes_json` is aliased to `broken_axes`
    so the ORM-row case Just Works.
    """
    out: list[VerdictRow] = []
    for it in items:
        if isinstance(it, VerdictRow):
            out.append(it)
            continue
        if isinstance(it, dict):
            get = it.get
        else:
            get = lambda k, default=None, _o=it: getattr(_o, k, default)  # noqa: E731
        out.append(VerdictRow(
            ai_rating=get("ai_rating"),
            recruiter_rating=get("recruiter_rating"),
            verdict=get("verdict"),
            feedback_text=get("feedback_text"),
            broken_axes=get("broken_axes") or get("broken_axes_json"),
            round_num=get("round_num"),
            candidate_uid=get("candidate_uid"),
        ))
    return out


def _stddev(values: list[float]) -> float | None:
    """Population std-dev. Returns None for empty or single-element lists
    so callers can render '—' instead of a misleading 0.0."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var)


def compute_benchmark_stats(
    verdicts: Iterable[Any],
    *,
    threshold: int | None = None,
) -> dict[str, Any]:
    """Compute every benchmark metric for a flat list of verdicts.

    `threshold` is the "👍 bar" for agreement_pct — when supplied, agreement
    counts the fraction of verdicts where (ai>=t) == (recruiter>=t). When
    None, agreement is left null (caller probably wants a running threshold
    instead, computed at a higher level).

    Returns a dict shaped for direct inclusion in JSON responses.
    """
    rows = _to_rows(verdicts)

    clean: list[VerdictRow] = []
    dirty: list[VerdictRow] = []
    for r in rows:
        if is_dirty_feedback(r.feedback_text):
            dirty.append(r)
        else:
            clean.append(r)

    # Only verdicts with both ratings present feed the numeric stats.
    rated = [r for r in clean if r.ai_rating is not None and r.recruiter_rating is not None]
    ai_vals = [float(r.ai_rating) for r in rated]
    rec_vals = [float(r.recruiter_rating) for r in rated]
    deltas = [a - r for a, r in zip(ai_vals, rec_vals)]
    abs_deltas = [abs(d) for d in deltas]

    mean_ai = (sum(ai_vals) / len(ai_vals)) if ai_vals else None
    mean_rec = (sum(rec_vals) / len(rec_vals)) if rec_vals else None
    bias = (sum(deltas) / len(deltas)) if deltas else None
    mae = (sum(abs_deltas) / len(abs_deltas)) if abs_deltas else None
    rmse = (
        math.sqrt(sum(d * d for d in deltas) / len(deltas))
        if deltas else None
    )
    within_1 = (
        sum(1 for d in abs_deltas if d <= 1) / len(abs_deltas)
        if abs_deltas else None
    )
    within_2 = (
        sum(1 for d in abs_deltas if d <= 2) / len(abs_deltas)
        if abs_deltas else None
    )

    std_ai = _stddev(ai_vals)
    std_rec = _stddev(rec_vals)
    # Discrimination ratio: how much spread the AI keeps relative to the
    # recruiter. < 0.5 = AI flattened; > 1.5 = AI is over-discriminating.
    # Null when recruiter has zero spread (every Rec rating identical) —
    # nothing meaningful to compare against.
    if std_ai is not None and std_rec is not None and std_rec > 0:
        discrimination_ratio = std_ai / std_rec
    else:
        discrimination_ratio = None

    false_negatives = sum(
        1 for a, r in zip(ai_vals, rec_vals)
        if a <= FN_AI_MAX and r >= FN_REC_MIN
    )
    false_positives = sum(
        1 for a, r in zip(ai_vals, rec_vals)
        if a >= FP_AI_MIN and r <= FP_REC_MAX
    )

    per_axis: dict[str, int] = {}
    for r in clean:
        if isinstance(r.broken_axes, list):
            for a in r.broken_axes:
                if a:
                    per_axis[a] = per_axis.get(a, 0) + 1

    agreement_pct: float | None = None
    agreement_n = 0
    if threshold is not None and rated:
        agree = 0
        for a, r in zip(ai_vals, rec_vals):
            if (a >= threshold) == (r >= threshold):
                agree += 1
        agreement_n = len(rated)
        agreement_pct = agree / agreement_n if agreement_n else None

    return {
        # Counts (data-quality split first so callers can decide whether
        # to render the dirty banner before showing the rest).
        "count": len(rated),
        "dirty_count": len(dirty),
        "dirty_uids": [r.candidate_uid for r in dirty if r.candidate_uid],
        # Central tendencies
        "mean_ai": mean_ai,
        "mean_recruiter": mean_rec,
        "bias": bias,
        "avg_delta": bias,  # alias kept for legacy callers
        # Error magnitudes
        "mae": mae,
        "rmse": rmse,
        "within_1": within_1,
        "within_2": within_2,
        # Spread / discrimination
        "std_ai": std_ai,
        "std_recruiter": std_rec,
        "discrimination_ratio": discrimination_ratio,
        # Asymmetric error tracking
        "false_negatives": false_negatives,
        "false_positives": false_positives,
        # Axes + bucket agreement
        "per_axis": per_axis,
        "agreement_pct": agreement_pct,
        "agreement_n": agreement_n,
    }


__all__ = [
    "VerdictRow",
    "compute_benchmark_stats",
    "is_dirty_feedback",
    "FN_AI_MAX",
    "FN_REC_MIN",
    "FP_AI_MIN",
    "FP_REC_MAX",
]
