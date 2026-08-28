"""Per-position decision-threshold calibration (τ) — REDESIGN §7 option B.

The judge's RANKING is sound (validated AUC) but its absolute fit scores sit low
(the calibrated-probability framing clusters them near the pool base rate), so a
fixed advance/reject cutoff fails. This module calibrates a per-position τ from
shadow data — judge scores joined to REAL screen outcomes — and persists it in
`position_taus` for consumers (scoring rounds, the Comeet Helper live tally).

Honesty: κ/accuracy are estimated with stratified 5-fold cross-validation (τ fit
on train folds, evaluated on the held-out fold), so the reported quality is not
an artifact of picking τ on the same data it's scored on. The DEPLOYED τ is then
fit on all data.

Also computes the coverage↔agreement curve for the confidence gate: "if the AI
auto-decides only when |fit − τ| ≥ d, what fraction of candidates does it cover,
and how often does it agree with the human outcome?" — the direct measure of the
end goal (auto-screen 70–90% of CVs, route the uncertain band to a human).

CLI:  python -m app.threshold <position_uid> [--no-save] [--source bench:<variant>]
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import learning_curve as lc

log = logging.getLogger(__name__)

_TAU_DDL = (
    "CREATE TABLE IF NOT EXISTS position_taus ("
    " position_uid text PRIMARY KEY, tau int, kappa_cv real, acc_cv real, auc real,"
    " n int, n_pass int, n_reject int, coverage_json text, source text,"
    " calibrated_at timestamptz default now())"
)


# ── Pure calibration math (no DB — unit-testable anywhere) ────────────────
def _kappa_at(fit: Sequence[float], lab: Sequence[int], tau: float) -> Optional[float]:
    pred = [1 if f >= tau else 0 for f in fit]
    return lc._cohen_kappa(list(lab), pred)


def best_tau(fit: Sequence[float], lab: Sequence[int],
             grid: range = range(5, 96)) -> Tuple[int, float]:
    """τ maximizing Cohen's κ on the given data. Ties -> the MIDDLE of the tying
    plateau (a cutoff centered in the empty gap generalizes better than its edge)."""
    scored = [(t, _kappa_at(fit, lab, t)) for t in grid]
    scored = [(t, k) for t, k in scored if k is not None]
    if not scored:
        return (50, 0.0)
    kmax = max(k for _, k in scored)
    ties = [t for t, k in scored if k == kmax]
    return (ties[len(ties) // 2], kmax)


def _stratified_folds(n_items: int, labels: Sequence[int], k: int = 5) -> List[List[int]]:
    """Deterministic stratified k-fold: round-robin each class into folds."""
    folds: List[List[int]] = [[] for _ in range(k)]
    for cls in (0, 1):
        idxs = [i for i in range(n_items) if labels[i] == cls]
        for j, i in enumerate(idxs):
            folds[j % k].append(i)
    return folds


def _cv_fold_taus(fit: Sequence[float], lab: Sequence[int], k: int = 5) -> Optional[List[Optional[int]]]:
    """Per-item out-of-fold τ: item i gets the τ fit on the folds NOT containing i.
    None per-item when that fold's train split is single-class (no calibratable τ)."""
    n = len(fit)
    if n < k or len(set(lab)) < 2:
        return None
    taus: List[Optional[int]] = [None] * n
    for fold in _stratified_folds(n, lab, k):
        train = [i for i in range(n) if i not in set(fold)]
        tl = [lab[i] for i in train]
        if len(set(tl)) < 2:
            continue  # degenerate fold — leave its items un-predicted, don't fake τ=50
        t, _ = best_tau([fit[i] for i in train], tl)
        for i in fold:
            taus[i] = t
    return taus


def cv_metrics(fit: Sequence[float], lab: Sequence[int], k: int = 5) -> Dict[str, Any]:
    """Cross-validated κ/accuracy: τ fit on train folds, predictions collected on
    held-out folds, metrics computed over the pooled held-out predictions —
    predictions kept ALIGNED with their labels (skipped items drop label too)."""
    taus = _cv_fold_taus(fit, lab, k)
    if taus is None:
        return {"kappa_cv": None, "acc_cv": None}
    pairs = [(1 if fit[i] >= taus[i] else 0, lab[i]) for i in range(len(fit)) if taus[i] is not None]
    if not pairs:
        return {"kappa_cv": None, "acc_cv": None}
    pred = [p for p, _ in pairs]
    plab = [l for _, l in pairs]
    kap = lc._cohen_kappa(plab, pred)
    acc = sum(1 for a, b in pairs if a == b) / len(pairs)
    return {"kappa_cv": round(kap, 3) if kap is not None else None, "acc_cv": round(acc, 3)}


def coverage_curve(fit: Sequence[float], lab: Sequence[int], tau: float,
                   taus: Optional[Sequence[Optional[float]]] = None) -> List[Dict[str, Any]]:
    """Sweep the auto-act margin d: auto-decide iff |fit − τ| ≥ d. For each d,
    report coverage (fraction auto-decided) and agreement with the human outcome
    among the auto-decided — split by auto-advance / auto-reject (rejections stay
    human-confirmed per the protocol, so the split matters operationally).

    Pass `taus` (per-item out-of-fold τ from _cv_fold_taus) for an HONEST curve —
    each item is judged against a τ that never saw it. Omitting taus gives the
    in-sample curve (optimistic; fine for deployment bands, not for evaluation)."""
    out = []
    per_item_tau: List[Optional[float]] = list(taus) if taus is not None else [tau] * len(fit)
    items = [(f, l, t) for f, l, t in zip(fit, lab, per_item_tau) if t is not None]
    n = len(items)
    if not n:
        return out
    for d in range(0, 41, 2):
        # advance band f >= τ+d, reject band f < τ−d: a partition at d=0
        # (f==τ is an advance, matching best_tau's `>=` decision rule).
        adv = [(f, l) for f, l, t in items if f >= t + d]
        rej = [(f, l) for f, l, t in items if f < t - d]
        auto = adv + rej
        if not auto:
            break
        agree = (sum(1 for _, l in adv if l == 1) + sum(1 for _, l in rej if l == 0)) / len(auto)
        out.append({"margin": d, "coverage": round(len(auto) / n, 3),
                    "agreement": round(agree, 3),
                    "adv_n": len(adv), "adv_precision": round(sum(1 for _, l in adv if l == 1) / len(adv), 3) if adv else None,
                    "rej_n": len(rej), "rej_precision": round(sum(1 for _, l in rej if l == 0) / len(rej), 3) if rej else None})
    return out


def coverage_at(curve: List[Dict[str, Any]], min_agreement: float) -> float:
    """Best coverage achievable at ≥min_agreement — the end-goal number."""
    ok = [pt["coverage"] for pt in curve if pt["agreement"] >= min_agreement]
    return max(ok) if ok else 0.0


# ── DB-backed calibration ─────────────────────────────────────────────────
def _load(position_uid: str, source: str = "candidate_scores") -> List[Tuple[float, int]]:
    """(fit, real screen outcome) pairs. source 'candidate_scores' uses live judge
    scores (taught brief only); 'bench:<variant>' uses benchmark variant scores
    (mean fit across runs)."""
    from sqlalchemy import text
    from .db import engine
    # LEAKAGE GUARD: candidates the recruiter rated in a teaching session are
    # excluded — the brief quotes their names + decisions, so the judge has
    # effectively seen their labels. (screen_ratings may not exist pre-teaching.)
    taught = ("AND NOT EXISTS (SELECT 1 FROM screen_ratings r "
              " WHERE r.position_uid=%(alias)s.position_uid AND r.candidate_uid=%(alias)s.candidate_uid)")
    if source.startswith("bench:"):
        sql = text(
            "SELECT avg(b.fit), cl.screen_label FROM bench_scores b "
            "JOIN corpus_screen_labels cl ON b.candidate_uid=cl.candidate_uid AND b.position_uid=cl.position_uid "
            "WHERE b.position_uid=:p AND b.variant=:v AND cl.screen_label IN (0,1) "
            + taught % {"alias": "b"} +
            " GROUP BY b.candidate_uid, cl.screen_label ORDER BY b.candidate_uid")
        params = {"p": position_uid, "v": source.split(":", 1)[1]}
    else:
        sql = text(
            "SELECT s.fit, cl.screen_label FROM candidate_scores s "
            "JOIN corpus_screen_labels cl ON s.candidate_uid=cl.candidate_uid AND s.position_uid=cl.position_uid "
            "WHERE s.position_uid=:p AND s.brief_built_from_n > 0 AND cl.screen_label IN (0,1) "
            + taught % {"alias": "s"} +
            " ORDER BY s.candidate_uid")
        params = {"p": position_uid}
    with engine.connect() as c:
        try:
            rows = c.execute(sql, params).all()
        except Exception as exc:  # noqa: BLE001 — screen_ratings absent pre-teaching
            log.info("_load with taught-exclusion failed (%s); retrying without", exc)
            rows = c.execute(text(str(sql.text).replace(taught % {"alias": "b"}, "")
                                  .replace(taught % {"alias": "s"}, "")), params).all()
    return [(float(r[0]), int(r[1])) for r in rows if r[0] is not None]


def calibrate(position_uid: str, save: bool = True,
              source: str = "candidate_scores") -> Dict[str, Any]:
    pairs = _load(position_uid, source)
    if len(pairs) < 20 or len({l for _, l in pairs}) < 2:
        return {"error": "insufficient_data",
                "message": f"{len(pairs)} labeled+scored pairs for {position_uid} "
                           f"(need >=20 with both classes)."}
    fit = [p[0] for p in pairs]
    lab = [p[1] for p in pairs]
    tau, kappa_fit = best_tau(fit, lab)
    cv = cv_metrics(fit, lab)
    auc = lc._auc(fit, lab)
    # HONEST coverage: out-of-fold τ per item, so the reported coverage@90 is not
    # an artifact of tuning τ on the same data. The stored deployment curve uses
    # the full-data τ (that's what production will apply going forward).
    fold_taus = _cv_fold_taus(fit, lab)
    curve = coverage_curve(fit, lab, tau, taus=fold_taus)
    result = {"position_uid": position_uid, "tau": tau,
              "kappa_fit": round(kappa_fit, 3),  # optimistic (fit==eval data) — report CV as truth
              "kappa_cv": cv["kappa_cv"], "acc_cv": cv["acc_cv"],
              "auc": round(auc, 3) if auc is not None else None,
              "n": len(pairs), "n_pass": sum(lab), "n_reject": len(lab) - sum(lab),
              "coverage_at_90": coverage_at(curve, 0.90),
              "coverage_at_85": coverage_at(curve, 0.85),
              "coverage_curve": curve, "source": source}
    if save and source != "candidate_scores":
        # NEVER persist a bench-variant τ: position_taus is read by the LIVE
        # Comeet Helper tally against production scores — a τ calibrated on a
        # different score distribution would flip its leans.
        result["note"] = "bench-source calibration is never saved (production tau untouched)"
        save = False
    if save:
        from sqlalchemy import text
        from .db import engine
        with engine.begin() as c:
            c.execute(text(_TAU_DDL))
            c.execute(text(
                "INSERT INTO position_taus (position_uid, tau, kappa_cv, acc_cv, auc, n,"
                " n_pass, n_reject, coverage_json, source, calibrated_at) "
                "VALUES (:p,:t,:k,:a,:auc,:n,:np,:nr,:cov,:src,now()) "
                "ON CONFLICT (position_uid) DO UPDATE SET tau=EXCLUDED.tau,"
                " kappa_cv=EXCLUDED.kappa_cv, acc_cv=EXCLUDED.acc_cv, auc=EXCLUDED.auc,"
                " n=EXCLUDED.n, n_pass=EXCLUDED.n_pass, n_reject=EXCLUDED.n_reject,"
                " coverage_json=EXCLUDED.coverage_json, source=EXCLUDED.source, calibrated_at=now()"),
                {"p": position_uid, "t": tau, "k": cv["kappa_cv"], "a": cv["acc_cv"],
                 "auc": result["auc"], "n": len(pairs), "np": sum(lab),
                 "nr": len(lab) - sum(lab), "cov": json.dumps(curve), "src": source})
    return result


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m app.threshold <position_uid> [--no-save] [--source bench:<variant>]")
        sys.exit(2)
    pos = sys.argv[1]
    save = "--no-save" not in sys.argv
    source = sys.argv[sys.argv.index("--source") + 1] if "--source" in sys.argv else "candidate_scores"
    print(json.dumps(calibrate(pos, save=save, source=source), indent=2))


if __name__ == "__main__":
    _main()
