"""Learning-curve + agreement tracker (OPERATING-PROTOCOL build-order #3).

Turns accumulating recruiter teaching into a measurable curve and the phase/gate
state machine from docs/OPERATING-PROTOCOL.md + REDESIGN §7:

    ① TEACH  → ② SHADOW+CORRECT → ③ SELF (confidence + learning-curve gates)

Joins the recruiter's ground-truth decisions (`screen_ratings`, written by the
Comeet Helper teaching flow via the bridge) with the judge's scores
(`candidate_scores`) on the same candidates, per position and — because the
target is the *recruiter's* decision — optionally per recruiter.

Metrics (§7): ranking agreement (Spearman ρ), decision agreement at the
advance/reject boundary (Cohen's κ), and separability (AUC of judge fit vs the
recruiter's advance/reject label). Pure-Python — no numpy/scipy dependency.

CLI:  python -m app.learning_curve <position_uid> [--recruiter <email>]
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

# ── Gates (REDESIGN §7 — tuned to the validated per-position numbers:
#    Eng Director AUC 0.899, BDR 0.814; generic-untaught ≈ 0.74 = the floor to beat).
#    Illustrative bars; re-tune to the measured human ceiling once κ double-labeling exists.
TEACH_MIN_N = 10          # protocol: ~10 actively-selected rounds before we read the curve
SHADOW_SPEARMAN = 0.55    # ① → ② : ranking tracks the recruiter (L1 Shadow gate)
SELF_AUC = 0.80           # ② → ③ : separability bar (below the validated 0.81/0.90, above the 0.74 floor)
SELF_KAPPA = 0.45         # ② → ③ : decision agreement at the boundary (L2 κ gate)
PLATEAU_WINDOW = 3        # cumulative points that must sit within…
PLATEAU_TOL = 0.03        # …±tol of each other for the curve to count as stabilized
CONF_BAND_ACT = 0.90      # ③ confidence gate: only auto-act in score bands with >90% historical agreement

_ORDINAL = {"reject": 0, "borderline": 1, "advance": 2}


# ── Pure-Python statistics ────────────────────────────────────────────────
def _ranks(xs: Sequence[float]) -> List[float]:
    """Average (tie-corrected) ranks, 1-based."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # positions i..j share the average rank
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    n = len(x)
    if n < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _auc(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """AUROC via the rank (Mann-Whitney U) identity. labels ∈ {0,1}."""
    pos = [s for s, l in zip(scores, labels) if l == 1]
    neg = [s for s, l in zip(scores, labels) if l == 0]
    if not pos or not neg:
        return None
    ranks = _ranks(list(scores))
    rank_pos_sum = sum(r for r, l in zip(ranks, labels) if l == 1)
    n1, n0 = len(pos), len(neg)
    return (rank_pos_sum - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def _cohen_kappa(a: Sequence[int], b: Sequence[int]) -> Optional[float]:
    """Cohen's κ for two binary labelers (advance=1 / not=0)."""
    n = len(a)
    if n == 0:
        return None
    agree = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1 = sum(a) / n
    pb1 = sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe >= 1.0:
        return 1.0 if agree >= 1.0 else 0.0
    return (agree - pe) / (1 - pe)


# ── Data ──────────────────────────────────────────────────────────────────
@dataclass
class Pair:
    candidate_uid: str
    decision: str          # recruiter: advance / borderline / reject
    created_at: float
    fit: Optional[int]     # judge 0-100
    recommendation: Optional[str]
    confidence: Optional[float]


def load_pairs(position_uid: str, recruiter: Optional[str] = None) -> List[Pair]:
    """Recruiter decisions joined to judge scores, oldest first (curve order)."""
    sql = (
        "SELECT r.candidate_uid, r.decision, r.created_at, s.fit, s.recommendation, s.confidence "
        "FROM screen_ratings r "
        "LEFT JOIN candidate_scores s "
        "  ON s.candidate_uid = r.candidate_uid AND s.position_uid = r.position_uid "
        "WHERE r.position_uid = :p "
        + ("AND r.recruiter = :rec " if recruiter else "")
        + "ORDER BY r.created_at ASC"
    )
    params: Dict[str, Any] = {"p": position_uid}
    if recruiter:
        params["rec"] = recruiter
    try:
        from sqlalchemy import text  # lazy — keeps the stats/phase engine importable without a DB
        from .db import engine
        with engine.connect() as c:
            rows = c.execute(text(sql), params).all()
    except Exception as exc:  # noqa: BLE001 — tables may not exist until first teaching
        log.info("load_pairs: %s", exc)
        return []
    return [Pair(str(r[0]), (r[1] or "").lower(), float(r[2] or 0), r[3], r[4], r[5]) for r in rows]


# ── Metrics over a set of pairs ───────────────────────────────────────────
def _metrics(pairs: Sequence[Pair]) -> Dict[str, Any]:
    scored = [p for p in pairs if p.fit is not None]
    # Spearman: judge fit vs recruiter ordinal (reject<borderline<advance)
    spearman = _spearman([p.fit for p in scored],
                         [_ORDINAL.get(p.decision, 1) for p in scored]) if len(scored) >= 3 else None
    # Boundary metrics exclude borderline (the uncertain band)
    binr = [p for p in scored if p.decision in ("advance", "reject")]
    labels = [1 if p.decision == "advance" else 0 for p in binr]
    auc = _auc([p.fit for p in binr], labels) if binr else None
    judge_adv = [1 if (p.recommendation or "") == "advance" else 0 for p in binr]
    kappa = _cohen_kappa(labels, judge_adv) if binr else None
    return {"n": len(pairs), "n_scored": len(scored), "n_boundary": len(binr),
            "spearman": spearman, "auc": auc, "kappa": kappa}


def _confidence_bands(pairs: Sequence[Pair]) -> List[Dict[str, Any]]:
    """Per-confidence-band decision agreement — feeds the ③ confidence gate."""
    bands = [(0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)]
    out = []
    for lo, hi in bands:
        grp = [p for p in pairs if p.fit is not None and p.decision in ("advance", "reject")
               and p.confidence is not None and lo <= p.confidence < hi]
        if not grp:
            continue
        agree = sum(1 for p in grp
                    if ((p.recommendation or "") == "advance") == (p.decision == "advance")) / len(grp)
        out.append({"band": f"{lo:.2f}-{hi:.2f}", "n": len(grp), "agreement": round(agree, 3),
                    "auto_act": agree >= CONF_BAND_ACT})
    return out


def _curve(pairs: Sequence[Pair]) -> List[Dict[str, Any]]:
    """Cumulative AUC/κ/Spearman as cases accumulate — the learning curve itself."""
    pts = []
    for k in range(5, len(pairs) + 1):
        m = _metrics(pairs[:k])
        pts.append({"n": k, "auc": _round(m["auc"]), "kappa": _round(m["kappa"]),
                    "spearman": _round(m["spearman"])})
    return pts


def _plateaued(curve: List[Dict[str, Any]], key: str = "auc") -> bool:
    vals = [pt[key] for pt in curve if pt[key] is not None]
    if len(vals) < PLATEAU_WINDOW:
        return False
    window = vals[-PLATEAU_WINDOW:]
    return (max(window) - min(window)) <= PLATEAU_TOL


def _round(x: Optional[float]) -> Optional[float]:
    return round(x, 3) if isinstance(x, (int, float)) else None


# ── Phase / gate assessment ───────────────────────────────────────────────
@dataclass
class Assessment:
    position_uid: str
    recruiter: Optional[str]
    phase: str            # "① TEACH" | "② SHADOW" | "③ SELF"
    metrics: Dict[str, Any]
    curve: List[Dict[str, Any]]
    confidence_bands: List[Dict[str, Any]]
    blocking: str         # what's keeping it out of the next phase
    ready_to_advance: bool


def assess(position_uid: str, recruiter: Optional[str] = None) -> Assessment:
    pairs = load_pairs(position_uid, recruiter)
    m = _metrics(pairs)
    curve = _curve(pairs)
    bands = _confidence_bands(pairs)
    n = m["n"]
    sp, auc, kappa = m["spearman"], m["auc"], m["kappa"]

    # ① TEACH — until we have enough taught cases with a ranking that tracks the recruiter
    if n < TEACH_MIN_N or sp is None or sp < SHADOW_SPEARMAN:
        need = []
        if n < TEACH_MIN_N:
            need.append(f"{n}/{TEACH_MIN_N} cases taught")
        if sp is None:
            need.append("ranking agreement not yet computable (need scored advance+reject cases)")
        elif sp < SHADOW_SPEARMAN:
            need.append(f"Spearman {sp:.2f} < {SHADOW_SPEARMAN} bar")
        return Assessment(position_uid, recruiter, "① TEACH", m, curve, bands,
                          "; ".join(need), ready_to_advance=False)

    # ② → ③ gate: separability + boundary agreement + a stabilized curve
    stable = _plateaued(curve, "auc")
    self_ok = (auc is not None and auc >= SELF_AUC
               and kappa is not None and kappa >= SELF_KAPPA and stable)
    if self_ok:
        return Assessment(position_uid, recruiter, "③ SELF", m, curve, bands,
                          "gates clear — autonomy kept while rolling agreement holds; "
                          "rejections stay human-confirmed; act only in >90% bands",
                          ready_to_advance=True)

    # In ② SHADOW: report exactly which gate blocks graduation
    block = []
    if auc is None:
        block.append("AUC not computable (need scored advance+reject cases)")
    elif auc < SELF_AUC:
        block.append(f"AUC {auc:.2f} < {SELF_AUC}")
    if kappa is None:
        block.append("κ not computable")
    elif kappa < SELF_KAPPA:
        block.append(f"κ {kappa:.2f} < {SELF_KAPPA}")
    if not stable:
        block.append(f"curve not stabilized (last {PLATEAU_WINDOW} AUC within ±{PLATEAU_TOL})")
    return Assessment(position_uid, recruiter, "② SHADOW", m, curve, bands,
                      "; ".join(block) or "holding in shadow", ready_to_advance=False)


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m app.learning_curve <position_uid> [--recruiter <email>]")
        sys.exit(2)
    pos = sys.argv[1]
    rec = None
    if "--recruiter" in sys.argv:
        rec = sys.argv[sys.argv.index("--recruiter") + 1]
    a = assess(pos, rec)
    print(json.dumps(asdict(a), indent=2))


if __name__ == "__main__":
    _main()
