"""Benchmark harness for CV-judge screening logics.

Scores a FIXED holdout of decided candidates (the same uids across variants, so
comparisons are apples-to-apples) with alternative judge configurations, then
compares them on the metrics that matter for the end goal — auto-screen 70-90%
of CVs at high confidence, route the uncertain to a human:

  - AUC            (ranking quality vs the recruiter's real screen outcome)
  - κ_cv / acc_cv  (decision quality at a cross-validated per-position τ)
  - spread         (p10-p90 of fit — compressed scores can't power a confidence gate)
  - coverage@90/85 (fraction auto-decidable at ≥90%/85% agreement — THE goal metric)
  - $/candidate    (scaling cost; shared Anthropic account, budget-guarded)

Variant scores land in `bench_scores` (NEVER candidate_scores — that table feeds
the live Comeet Helper tally and must stay production-only). Runs are resume-safe:
already-scored (variant, uid, run) rows are skipped, so a container restart just
continues. A hard budget guard aborts scoring when estimated spend crosses the cap.

CLI:
  python -m app.bench_screen run <position_uid> --variants anchored,percentile [--runs N] [--budget USD]
  python -m app.bench_screen analyze <position_uid>
"""
from __future__ import annotations

import json
import logging
import statistics as st
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from .db import engine
from .comeet_client import ComeetClient, candidate_full_name
from .screen_judge import score_candidate, SYSTEM as SYSTEM_V0, MODEL as MODEL_V0
from . import learning_curve as lc
from . import threshold as th

log = logging.getLogger(__name__)

_BENCH_DDL = (
    "CREATE TABLE IF NOT EXISTS bench_scores ("
    " variant text, run int, candidate_uid text, position_uid text, candidate_name text,"
    " fit int, recommendation text, confidence real, rationale text, model text,"
    " in_tokens int, out_tokens int, scored_at timestamptz default now(),"
    " PRIMARY KEY (variant, run, candidate_uid, position_uid))"
)

# Sonnet-5 intro pricing (through 2026-08-31); haiku-4.5 standard.
_PRICE = {  # $ per M tokens (in, out)
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _cost(model: str, tin: int, tout: int) -> float:
    pin, pout = _PRICE.get(model, (2.0, 10.0))
    return tin * pin / 1e6 + tout * pout / 1e6


# ── Variant configs ────────────────────────────────────────────────────────
# Filled by the prompt-design panel; each MUST contain DO-NOT-LEARN verbatim
# (score_candidate enforces it). v0_current is the production config — its
# scores are read from candidate_scores, never re-bought.
DO_NOT_LEARN = (
    "DO-NOT-LEARN (hard rule): NEVER use or infer accent, national origin, country of education, "
    "years-in-country, or name/photo-based nationality/gender/age. Judge only job-relevant evidence in "
    "the CV. Your rationale must never cite any of these."
)

VARIANTS: Dict[str, Dict[str, Any]] = {}  # populated below after panel output


def register_variant(key: str, system: str, *, model: Optional[str] = None,
                     effort: str = "low", use_thinking: bool = True,
                     brief: Optional[str] = None) -> None:
    if "DO-NOT-LEARN" not in system:
        raise ValueError(f"variant {key}: missing DO-NOT-LEARN")
    if brief is not None and "DO-NOT-LEARN" not in brief:
        raise ValueError(f"variant {key}: brief override missing DO-NOT-LEARN")
    VARIANTS[key] = {"system": system, "model": model, "effort": effort,
                     "use_thinking": use_thinking, "brief": brief}


# ── Bench set ─────────────────────────────────────────────────────────────
def bench_set(position_uid: str) -> List[Tuple[str, int]]:
    """The canonical holdout: uids already scored by the production judge with a
    taught brief (candidate_scores), joined to their real screen outcome.

    LEAKAGE GUARD: candidates the recruiter rated in a teaching session are
    excluded — the brief quotes their names + decisions, so any judge reading the
    brief has effectively seen their labels."""
    with engine.connect() as c:
        try:
            rows = c.execute(text(
                "SELECT s.candidate_uid, cl.screen_label FROM candidate_scores s "
                "JOIN corpus_screen_labels cl ON s.candidate_uid=cl.candidate_uid AND s.position_uid=cl.position_uid "
                "WHERE s.position_uid=:p AND s.brief_built_from_n > 0 AND cl.screen_label IN (0,1) "
                "AND NOT EXISTS (SELECT 1 FROM screen_ratings r "
                "  WHERE r.position_uid=s.position_uid AND r.candidate_uid=s.candidate_uid) "
                "ORDER BY s.candidate_uid"), {"p": position_uid}).all()
        except Exception as exc:  # noqa: BLE001 — screen_ratings absent pre-teaching
            log.info("bench_set taught-exclusion failed (%s); retrying without", exc)
            rows = c.execute(text(
                "SELECT s.candidate_uid, cl.screen_label FROM candidate_scores s "
                "JOIN corpus_screen_labels cl ON s.candidate_uid=cl.candidate_uid AND s.position_uid=cl.position_uid "
                "WHERE s.position_uid=:p AND s.brief_built_from_n > 0 AND cl.screen_label IN (0,1) "
                "ORDER BY s.candidate_uid"), {"p": position_uid}).all()
    return [(str(r[0]), int(r[1])) for r in rows]


def _prior_spend(position_uid: str) -> float:
    """Spend already recorded in bench_scores for this position — seeds the budget
    guard so a resume/restart CONTINUES the budget instead of re-arming it."""
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT model, sum(in_tokens), sum(out_tokens) FROM bench_scores "
            "WHERE position_uid=:p GROUP BY model"), {"p": position_uid}).all()
    return sum(_cost(m, ti or 0, to or 0) for m, ti, to in rows)


# ── Run ───────────────────────────────────────────────────────────────────
# Conservative per-call estimate charged to the budget when a billed API call
# returns nothing parseable (no tool_use / missing fit) — those calls still cost
# real money on the shared account and MUST count against the guard.
_UNPARSED_CALL_EST_USD = 0.04
_MAX_CONSECUTIVE_FAILURES = 8  # a variant that can't emit the tool gets skipped


def run_bench(position_uid: str, variant_keys: List[str], runs: int = 1,
              budget_usd: float = 10.0) -> Dict[str, Any]:
    from .score_round import get_brief
    unknown = [k for k in variant_keys if k not in VARIANTS]
    if unknown:
        return {"error": "unknown_variants", "unknown": unknown, "known": sorted(VARIANTS)}
    brief = get_brief(position_uid)
    if not brief:
        return {"error": "no_brief"}
    sample = bench_set(position_uid)
    if len(sample) < 20:
        return {"error": "bench_set_too_small", "n": len(sample)}

    with engine.begin() as c:
        c.execute(text(_BENCH_DDL))
        done = {(r[0], r[1], r[2]) for r in c.execute(text(
            "SELECT variant, run, candidate_uid FROM bench_scores WHERE position_uid=:p"),
            {"p": position_uid}).all()}

    # Budget CONTINUES across restarts — seeded from rows already bought.
    spent = _prior_spend(position_uid)
    counts: Dict[str, int] = {}
    aborted = False
    skipped_variants: List[str] = []
    with ComeetClient() as cc:
        position = cc.get_position(position_uid)
        for key in variant_keys:
            cfg = VARIANTS[key]
            consec_fail = 0
            for run in range(1, runs + 1):
                for uid, _label in sample:
                    if (key, run, uid) in done:
                        continue
                    if spent >= budget_usd:
                        aborted = True
                        break
                    if consec_fail >= _MAX_CONSECUTIVE_FAILURES:
                        log.warning("bench %s: %d consecutive failures — skipping variant", key, consec_fail)
                        skipped_variants.append(key)
                        break
                    try:
                        cand = cc.get_candidate(uid)
                        use_brief = cfg.get("brief") or brief["brief"]
                        res = score_candidate(cand, position, use_brief,
                                              system=cfg["system"], model=cfg["model"],
                                              effort=cfg["effort"], use_thinking=cfg["use_thinking"]) if cand else None
                    except Exception as exc:  # noqa: BLE001
                        log.warning("bench %s/%s run%d: %s", key, uid, run, exc)
                        spent += _UNPARSED_CALL_EST_USD  # API may have billed before failing
                        consec_fail += 1
                        continue
                    if not res:
                        # Billed but unparseable (model skipped the tool call) —
                        # still charge the guard, and count toward the failure cap.
                        spent += _UNPARSED_CALL_EST_USD
                        consec_fail += 1
                        continue
                    consec_fail = 0
                    with engine.begin() as conn:
                        conn.execute(text(
                            "INSERT INTO bench_scores (variant, run, candidate_uid, position_uid,"
                            " candidate_name, fit, recommendation, confidence, rationale, model,"
                            " in_tokens, out_tokens) VALUES (:v,:r,:u,:p,:n,:f,:rec,:c,:ra,:m,:ti,:to) "
                            "ON CONFLICT DO NOTHING"),
                            {"v": key, "r": run, "u": uid, "p": position_uid,
                             "n": candidate_full_name(cand), "f": res["fit"],
                             "rec": res["recommendation"], "c": res["confidence"],
                             "ra": res["rationale"], "m": res["model"],
                             "ti": res["in_tokens"], "to": res["out_tokens"]})
                    spent += _cost(res["model"], res["in_tokens"], res["out_tokens"])
                    counts[key] = counts.get(key, 0) + 1
                if aborted or key in skipped_variants:
                    break
            if aborted:
                break
    return {"scored": counts, "spent_usd_cumulative": round(spent, 2), "budget_usd": budget_usd,
            "aborted_on_budget": aborted, "skipped_variants": skipped_variants,
            "bench_n": len(sample)}


# ── Analyze ───────────────────────────────────────────────────────────────
def _variant_rows(position_uid: str) -> Dict[str, List[Tuple[str, float, int, Optional[float]]]]:
    """{variant: [(uid, mean_fit, label, fit_std)]}; v0_current from candidate_scores."""
    out: Dict[str, List[Tuple[str, float, int, Optional[float]]]] = {}
    with engine.connect() as c:
        v0 = c.execute(text(
            "SELECT s.candidate_uid, s.fit, cl.screen_label FROM candidate_scores s "
            "JOIN corpus_screen_labels cl ON s.candidate_uid=cl.candidate_uid AND s.position_uid=cl.position_uid "
            "WHERE s.position_uid=:p AND s.brief_built_from_n > 0 AND cl.screen_label IN (0,1) "
            "AND NOT EXISTS (SELECT 1 FROM screen_ratings r "
            "  WHERE r.position_uid=s.position_uid AND r.candidate_uid=s.candidate_uid)"),
            {"p": position_uid}).all()
        out["v0_current"] = [(str(r[0]), float(r[1]), int(r[2]), None) for r in v0]
        rows = c.execute(text(
            "SELECT b.variant, b.candidate_uid, avg(b.fit), stddev_pop(b.fit), max(cl.screen_label), count(*) "
            "FROM bench_scores b JOIN corpus_screen_labels cl "
            "  ON b.candidate_uid=cl.candidate_uid AND b.position_uid=cl.position_uid "
            "WHERE b.position_uid=:p AND cl.screen_label IN (0,1) "
            "GROUP BY b.variant, b.candidate_uid"), {"p": position_uid}).all()
    for v, uid, mean_fit, std, label, n in rows:
        # stddev_pop over a single run is 0, not NULL — that would fake perfect
        # consistency. Only report per-candidate std when there are >=2 runs.
        out.setdefault(v, []).append((str(uid), float(mean_fit), int(label),
                                      float(std) if (std is not None and n and n >= 2) else None))
    return out


def _cost_per_cand(position_uid: str, variant: str) -> Optional[float]:
    with engine.connect() as c:
        r = c.execute(text(
            "SELECT model, sum(in_tokens), sum(out_tokens), count(*) FROM bench_scores "
            "WHERE position_uid=:p AND variant=:v GROUP BY model"), {"p": position_uid, "v": variant}).all()
    if not r:
        return None
    total = sum(_cost(m, ti or 0, to or 0) for m, ti, to, _ in r)
    n = sum(cnt for _, _, _, cnt in r)
    return round(total / n, 4) if n else None


def analyze(position_uid: str) -> Dict[str, Any]:
    data = _variant_rows(position_uid)
    # Apples-to-apples: restrict every variant to the COMMON candidate set, so a
    # production prime adding candidate_scores rows mid-bench can't skew v0.
    complete = {v: rows for v, rows in data.items() if len(rows) >= 20}
    if len(complete) > 1:
        common = set.intersection(*[{r[0] for r in rows} for rows in complete.values()])
        data = {v: ([r for r in rows if r[0] in common] if v in complete else rows)
                for v, rows in data.items()}
    report: Dict[str, Any] = {"position_uid": position_uid, "variants": {},
                              "common_n": min((len(r) for v, r in data.items() if v in complete), default=0)}
    for v, rows in sorted(data.items()):
        if len(rows) < 20:
            report["variants"][v] = {"n": len(rows), "note": "too few scored"}
            continue
        fit = [r[1] for r in rows]
        lab = [r[2] for r in rows]
        if len(set(lab)) < 2:
            report["variants"][v] = {"n": len(rows), "note": "single-class labels — metrics undefined"}
            continue
        auc = lc._auc(fit, lab)
        tau, _ = th.best_tau(fit, lab)
        cv = th.cv_metrics(fit, lab)
        # HONEST curve: per-item out-of-fold τ so coverage@90 isn't tuned on itself.
        curve = th.coverage_curve(fit, lab, tau, taus=th._cv_fold_taus(fit, lab))
        stds = [r[3] for r in rows if r[3] is not None]
        fs = sorted(fit)
        report["variants"][v] = {
            "n": len(rows), "auc": round(auc, 3) if auc is not None else None,
            "tau": tau, "kappa_cv": cv["kappa_cv"], "acc_cv": cv["acc_cv"],
            "spread_p10_p90": [fs[int(0.1 * len(fs))], fs[int(0.9 * len(fs)) - 1]],
            "fit_std": round(st.pstdev(fit), 1),
            "mean_fit_pass": round(st.mean([f for f, l in zip(fit, lab) if l == 1]), 1),
            "mean_fit_reject": round(st.mean([f for f, l in zip(fit, lab) if l == 0]), 1),
            "coverage_at_90": th.coverage_at(curve, 0.90),
            "coverage_at_85": th.coverage_at(curve, 0.85),
            "consistency_std_mean": round(st.mean(stds), 1) if stds else None,
            "cost_per_cand_usd": _cost_per_cand(position_uid, v),
            "coverage_curve": curve[:8],
        }
    return report


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("usage: python -m app.bench_screen run <uid> --variants a,b [--runs N] [--budget USD]\n"
              "       python -m app.bench_screen analyze <uid>")
        sys.exit(2)
    cmd, pos = sys.argv[1], sys.argv[2]
    if cmd == "run":
        keys = sys.argv[sys.argv.index("--variants") + 1].split(",") if "--variants" in sys.argv else list(VARIANTS)
        runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 1
        budget = float(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 10.0
        t0 = time.time()
        out = run_bench(pos, keys, runs=runs, budget_usd=budget)
        out["seconds"] = int(time.time() - t0)
        print(json.dumps(out, indent=2))
    elif cmd == "analyze":
        print(json.dumps(analyze(pos), indent=2))
    else:
        print(f"unknown command {cmd}")
        sys.exit(2)


# ── Registered variants (prompts from the 2026-08-28 design panel) ────────
_ANCHORED_SYSTEM = """You are an expert recruiter performing an initial CV screen for the SPECIFIC role in the POSITION CRITERIA.

DO-NOT-LEARN (hard rule): NEVER use or infer accent, national origin, country of education, years-in-country, or name/photo-based nationality/gender/age. Judge only job-relevant evidence in the CV. Your rationale must never cite any of these.

Procedure:
1. From the POSITION CRITERIA, extract the MUST-HAVES and NICE-TO-HAVES. If the brief does not label them, treat its most emphasized requirements as must-haves. If the brief is too thin, infer standard expectations for the role title and lower confidence.
2. Score the 8 dimensions 1-5 on CV evidence alone.
3. Set overall_fit_0_100 from the ABSOLUTE RUBRIC below. It is a rubric score of evidence strength — NOT a probability of advancing, NOT relative to the applicant pool, NOT anchored to any pass rate, and NOT an average of the 8 dimensions. Identical evidence gets an identical score in any pool.

RUBRIC (place the score inside its band by evidence strength; do not sit on band edges):
- 90-100: every must-have explicitly evidenced plus several nice-to-haves; exceptional match.
- 75-89: CLEAR ADVANCE — all must-haves evidenced; gaps only in nice-to-haves.
- 60-74: lean advance — must-haves mostly evidenced; one inferable but not explicit.
- 40-59: GENUINELY UNCERTAIN — evidence materially mixed, or one must-have unverifiable; a human should decide.
- 25-39: lean reject — a must-have clearly weak or missing, with little compensating strength.
- 10-24: CLEAR REJECT — multiple must-haves absent, or wrong function or seniority.
- 0-9: no relevant background for this role.

Hard rules:
- Judge what the CV shows: no evidence of a must-have counts as lacking it.
- Screening pools are mostly clear cases; expect most scores OUTSIDE 40-59. The middle band marks genuine ambiguity, never a safe default.
- If the CV is empty, truncated, or unreadable, score 45-55 with confidence ≤0.3.

Then set advance_recommendation from the score: ≥75 advance, 40-74 borderline, ≤39 reject. confidence_0_1 = how sure you are the band is correct (thin CV or thin brief → lower). Rationale ≤240 chars: the decisive evidence. You MUST call submit_assessment."""

_PERCENTILE_SYSTEM = """You are an expert recruiter performing an initial CV screen for the SPECIFIC role described in the POSITION CRITERIA.

DO-NOT-LEARN (hard rule): NEVER use or infer accent, national origin, country of education, years-in-country, or name/photo-based nationality/gender/age. Judge only job-relevant evidence in the CV. Your rationale must never cite any of these.

REFERENCE POOL: Picture 100 typical applicants to this exact role — including the off-target and under-qualified majority every opening attracts. Every score compares this candidate to that pool, never to an ideal hire.

DIMENSIONS (1-5): 3 = typical applicant; 4 = clearly above; 5 = top ~10%; 2 = clearly below; 1 = bottom ~10%.

OVERALL_FIT_0_100 is a PERCENTILE, not a probability: the number of those 100 applicants this candidate beats on fit for the brief. Weight the brief's must-haves most heavily; a missing stated must-have caps the score below 35. The median applicant is mediocre — a CV that cleanly meets every core requirement is already 70+.

Band anchors:
- 90-100 exceptional; would excite the hiring manager
- 75-89 clearly stronger than most; advance
- 60-74 above median with one real gap
- 40-59 genuinely mixed: comparable strengths and weaknesses; a human should look
- 25-39 below median; likely reject
- 10-24 clearly weak against the criteria
- 0-9 no meaningful match (wrong function, or far below required seniority)

ANTI-BUNCHING: Percentiles are uniform by construction — across candidates half must land below 50 and scores must span the full range. Commit to the band the evidence supports. Use 40-59 only for truly mixed profiles, never as a refuge for thin evidence: a short CV that fails to show the must-haves scores like the weak applicant it resembles. Express uncertainty through confidence_0_1 instead — lower it for sparse CVs, thin briefs, or ambiguous evidence. If the CV is empty, truncated, or unreadable (a data failure, not evidence), score 45-55 with confidence ≤0.3.

ADVANCE_RECOMMENDATION follows the score exactly: ≥75 advance; ≤39 reject; otherwise borderline.

Rationale (≤240 chars): decisive evidence only. If the brief is thin, judge on core function, seniority, and recency, and lower confidence. You MUST call submit_assessment."""

register_variant("anchored", _ANCHORED_SYSTEM)
register_variant("percentile", _PERCENTILE_SYSTEM)
# v0 = the production prompt, registered so multi-run (×3 mean) controls can be
# bought through the same bench path as the challengers — apples to apples.
register_variant("v0", SYSTEM_V0)
# axis: does more reasoning effort improve calibration? (same anchored prompt)
register_variant("anchored_med", _ANCHORED_SYSTEM, effort="medium")
# axis: can the cheap model match? (production scaling cost; intro sonnet pricing ends 2026-08-31)
register_variant("anchored_haiku", _ANCHORED_SYSTEM, model="claude-haiku-4-5", use_thinking=False)



# ── Enriched-brief experiment (Agency AE only — brief is position-specific) ─
# Hypothesis: the AUC ceiling (~0.795) comes from a thin, generic brief (tag
# counts, no role definition) + an empty Comeet JD. This brief makes the same
# recruiter signal SPECIFIC: what Riverside sells, what the agency-AE core
# function is, and what "right company type / industry" concretely mean —
# grounded ONLY in the recruiter's 10 ratings + the public JD (2+yrs closing at
# a tech company, consultative, high-velocity pipeline, media a plus).
_AAE_RICH_BRIEF = """POSITION CRITERIA — Agency Account Executive (Riverside, Canada, remote, senior IC).

ROLE CONTEXT: Riverside is an AI-powered audio/video content-creation SaaS platform
(podcast/video recording, editing, production tooling) sold to businesses, media teams,
and agencies. This role is a quota-carrying, full-cycle ACCOUNT EXECUTIVE for the agency
segment: prospect-to-close ownership, consultative selling, high-velocity pipeline.

MUST-HAVES — the recruiter consistently rejected candidates missing ANY of these:
1. REAL B2B CLOSING EXPERIENCE (the core function): 2+ years as a quota-carrying AE /
   closing seller at a technology company, owning full sales cycles to close. Backgrounds
   that are only account management, customer success, SDR/BDR prospecting without
   closing, retail, or consumer sales do NOT satisfy this.
2. RIGHT COMPANY TYPE: product/technology companies — SaaS or software vendors.
   Careers spent only at non-tech employers (traditional services, industrial,
   finance-ops, staffing) were consistently rejected as "wrong company type/tier".
3. RELEVANT INDUSTRY/DOMAIN: SaaS sales, media/content/creator technology,
   marketing/advertising technology, or selling INTO agencies. Purely unrelated
   verticals were consistently rejected as "wrong industry/domain".

SUPPORTING SIGNALS the recruiter advanced on: seniority matched to a senior IC AE
(not executive-heavy, not entry-level), role-relevant experience that is RECENT,
evidence of consultative selling and a fast, multi-deal pipeline, track record of
quota attainment. Media-industry exposure is a plus.

Weigh the three MUST-HAVES most heavily: a CV clearly evidencing all three merits a
high score; a CV clearly missing any one of them merits a low score. Use the middle
of the range only for genuinely mixed evidence.

DO-NOT-LEARN (hard rule): never use or infer accent, national origin, country of
education, years-in-country, or name/photo-based nationality/gender/age. Judge only
job-relevant evidence in the CV."""

register_variant("v0_richbrief", SYSTEM_V0, brief=_AAE_RICH_BRIEF)


if __name__ == "__main__":
    _main()  # NOTE: must stay LAST — variant registration above runs first
