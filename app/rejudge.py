"""Re-judge a position's DECIDED candidates and measure shadow agreement.

The live per-position Comeet feed omits rejected candidates, so the normal
scoring round can only judge people who PASSED screen. This module instead
samples decided candidates (passed AND rejected) from the corpus, fetches each
CV fresh by uid, scores it with the position's TAUGHT brief, and measures the
judge's agreement against the real screen outcome (AUC / Cohen's κ / accuracy).

This is the leakage-free "did teaching lift the judge?" test — the sampled
candidates are historical decisions, not the recruiter's teaching set.

CLI:  python -m app.rejudge <position_uid> [--per-class N] [--keep]
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict, List, Tuple

from sqlalchemy import text

from .db import engine
from .comeet_client import ComeetClient, candidate_full_name
from .screen_judge import score_candidate
from .score_round import get_brief, _UPSERT, _DDL
from . import learning_curve as lc

log = logging.getLogger(__name__)


def _corpus_sample(position_uid: str, per_class: int) -> List[Tuple[str, int]]:
    """Up to `per_class` passed + `per_class` rejected decided candidates with a
    CV, most-recently-screened first (reflects the current bar)."""
    with engine.begin() as c:
        c.execute(text(_DDL))  # ensure candidate_scores exists
        rows = c.execute(text(
            "SELECT candidate_uid, screen_label FROM corpus_screen_labels "
            "WHERE position_uid=:p AND screen_label IN (0,1) AND has_resume "
            "ORDER BY cv_screen_time DESC NULLS LAST"), {"p": position_uid}).all()
    passed = [(str(r[0]), 1) for r in rows if r[1] == 1][:per_class]
    rejected = [(str(r[0]), 0) for r in rows if r[1] == 0][:per_class]
    return passed + rejected


def rejudge(position_uid: str, per_class: int = 50, clear: bool = True) -> Dict[str, Any]:
    brief = get_brief(position_uid)
    if not brief:
        return {"error": "no_brief", "message": f"No taught brief for {position_uid}."}
    sample = _corpus_sample(position_uid, per_class)
    if clear:
        with engine.begin() as c:
            c.execute(text("DELETE FROM candidate_scores WHERE position_uid=:p"), {"p": position_uid})

    scored: List[Tuple[str, int, int, int]] = []  # (uid, label, fit, judge_advance)
    tin = tout = skipped = 0
    with ComeetClient() as cc:
        position = cc.get_position(position_uid)
        for uid, label in sample:
            try:
                cand = cc.get_candidate(uid)
                res = score_candidate(cand, position, brief["brief"]) if cand else None
            except Exception as exc:  # noqa: BLE001
                log.warning("rejudge %s: %s", uid, exc)
                skipped += 1
                continue
            if not res:
                skipped += 1
                continue
            row = {"candidate_uid": uid, "position_uid": position_uid,
                   "candidate_name": candidate_full_name(cand), "fit": res["fit"],
                   "recommendation": res["recommendation"], "confidence": res["confidence"],
                   "dims_json": json.dumps(res["dims"]), "rationale": res["rationale"],
                   "model": res["model"], "brief_built_from_n": brief["built_from_n"],
                   "in_tokens": res["in_tokens"], "out_tokens": res["out_tokens"]}
            with engine.begin() as conn:
                conn.execute(text(_UPSERT), row)
            scored.append((uid, label, res["fit"], 1 if res["recommendation"] == "advance" else 0))
            tin += res["in_tokens"]; tout += res["out_tokens"]

    fit = [s[2] for s in scored]; lab = [s[1] for s in scored]; jadv = [s[3] for s in scored]
    auc = lc._auc(fit, lab); kap = lc._cohen_kappa(lab, jadv)
    acc = sum(1 for a, b in zip(jadv, lab) if a == b) / len(scored) if scored else 0.0
    cost = tin * 2.0 / 1_000_000 + tout * 10.0 / 1_000_000  # Sonnet-5 intro rate
    npass = sum(lab); nrej = len(lab) - npass
    return {"position_uid": position_uid, "brief_built_from_n": brief["built_from_n"],
            "scored": len(scored), "skipped": skipped, "passed": npass, "rejected": nrej,
            "auc": round(auc, 3) if auc is not None else None,
            "kappa": round(kap, 3) if kap is not None else None,
            "accuracy": round(acc, 3), "cost_usd": round(cost, 2)}


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m app.rejudge <position_uid> [--per-class N] [--keep]")
        sys.exit(2)
    pos = sys.argv[1]
    per = int(sys.argv[sys.argv.index("--per-class") + 1]) if "--per-class" in sys.argv else 50
    t0 = time.time()
    out = rejudge(pos, per_class=per, clear="--keep" not in sys.argv)
    out["seconds"] = int(time.time() - t0)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()
