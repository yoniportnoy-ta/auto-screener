# Auto-Screener Roadmap — Where We Are

**Last updated:** 2026-08-29 · **Owner:** Yoni · **Companions:** `REDESIGN.md`, `OPERATING-PROTOCOL.md`, `TEACHING-LOOP-V1.md`

---

## 🎯 North star

> **An auto-screener that screens 70–90% of incoming CVs on its own — high-confidence
> advances and rejects — and routes only the genuinely uncertain ones to a human.
> Rejections always stay human-confirmed.**

**The goal metric:** `coverage@90` — the fraction of candidates the judge can auto-decide
in score bands where it historically agrees with the recruiter ≥90% of the time
(measured out-of-fold, leakage-free). Today that number is the gap between "nice demo"
and the north star.

---

## 🗺️ The path (per position — every role earns autonomy separately)

```
① TEACH    recruiter rates ~10 boundary-spanning CVs in Slack (Advance/Reject + why)
   │        → builds the position BRIEF (criteria in the recruiter's own words)
   ▼
② SHADOW   judge scores incoming CVs with the brief; takes NO action
   │        → measure agreement vs recruiter decisions (AUC / κ at calibrated τ)
   ▼        → gates: AUC ≥ 0.80, κ_cv ≥ 0.45, curve plateaued
③ SELF     judge auto-decides ONLY in >90%-agreement confidence bands;
            everything else → human. Drift → auto-demote to ②.
```

---

## 🏗️ System map

| piece | where | role |
|---|---|---|
| **Comeet Helper** (pipey-bot) | Slack bot, Render worker (Oregon) | recruiter-facing: teaching sessions, live agreement tally, kickoffs |
| **auto-screener** | Render web svc (Frankfurt) + Postgres | the judge (Claude), corpus mining, τ calibration, benchmarks |
| **bridge** | `SCREENER_DATABASE_URL` (external host!) | pipey ⇄ auto-screener Postgres: ratings/briefs in, scores/τ out |
| key tables | auto-screener Postgres | `screen_ratings`, `position_briefs`, `candidate_scores` (production), `corpus_screen_labels` (history, re-mined 6-hourly), `position_taus`, `bench_scores` (experiments only) |

---

## ✅ Done (proven, deployed)

| date | milestone | evidence |
|---|---|---|
| 08-26 | Teaching flow in Slack (rate + reason → brief) | briefs land in `position_briefs` |
| 08-26 | Judge scores with taught briefs (`screen_judge`, v4 config) | validated AUC 0.814 BDR / 0.899 Eng Director (earlier corpus) |
| 08-27 | 3 pilots launched w/ lead recruiters (Jade/AE, Noga/SrPM, Mor/FEInfra) | DMs + sessions live |
| 08-27 | **Bridge outage found+fixed** (internal DB host cross-region = dead; all errors swallowed) | external host; ratings/scores flow |
| 08-27 | Rejected candidates wired into teaching (live feed has zero rejects → corpus source) + 4/3/3 reject/advance/fresh mix | queue composition verified |
| 08-27 | CV links fixed (15-min presigned S3 → signed always-fresh redirect `/cv/<uid>`) | click → real CV, 403 on tamper |
| 08-28 | **First proof teaching works:** Jade's decisive round → AUC 0.756, κ 0.48 @ optimal τ | `rejudge` on 50 pass + 50 reject |
| 08-28 | **Binary teaching** (Advance/Reject only; "Hard to tell from CV alone" = reason, excluded from metrics) | Noga's 7/10-borderline round taught ~nothing → root cause |
| 08-28 | **τ calibration** (`app/threshold.py`): per-position threshold, 5-fold CV, persisted, used by live tally | Agency AE: **τ=30** saved |
| 08-28 | Leakage guards (teaching-set candidates excluded from all eval — the brief names them) + 23 review findings fixed | adversarial review workflow |

## 📌 Honest baseline (Agency AE, v0 judge, n=99, cross-validated)

| metric | value | meaning |
|---|--:|---|
| AUC | **0.754** | ranking is good |
| κ_cv @ τ=30 | **0.374** | decisions are mediocre |
| **coverage@90** | **0.0** | **can auto-decide NOTHING at the 90% bar** |
| why | scores compressed 15–40 | "calibrated likelihood" framing → scores cluster at the ~31% base rate |

**The insight driving the current round:** the compression is *caused by what we asked
for*. Fix = change what the score *means* (absolute rubric / percentile-in-pool),
not "please use the full range."

---

## 🧪 Benchmark round verdict (FINAL, 2026-08-29 — control-corrected)

Full n=99 holdout, every config ×1 and ×3, leakage-free, cross-validated.
**The v0×3 control overturned the interim verdict** (interim "anchored×3 wins" was an
n=75 subset artifact — the control run caught it before we shipped a worse prompt):

| config | AUC | κ_cv | cov@85 | cov@80 |
|---|--:|--:|--:|--:|
| **v0 ×3 (ADOPTED)** | **0.795** | **0.396** | **50.5%** | **63.6%** |
| v0 ×1 (previous prod) | 0.783 | 0.356 | 48.5% | 48.5% |
| percentile ×3 | 0.766 | 0.334 | 40.4% | 48.5% |
| anchored ×3 | 0.772 | 0.316 | 0% | 42.4% |
| anchored @haiku-4.5 | 0.686 | 0.135 | 0% | 0% |

**Adopted 2026-08-29:** production judge = original v0 prompt, `JUDGE_RUNS=3`
(3-run mean fit, ~$0.056/CV), τ recalibrated to **24**, `position_taus` updated
(live tally follows automatically).

**What the round proved:**
1. **Prompt redesign FAILED** — both panel prompts underperform the original on the
   full holdout. Score compression is the judge's honest read of the pool vs a
   10-rating brief, not a framing artifact. Prompt work on this axis is closed.
2. **3-run averaging is real but modest** (+0.01–0.02 AUC, +0.04 κ) — adopted.
3. **The deployable wedge (v0×3, τ=24):** margin 6 → **50% of CVs at 86% agreement**
   (auto-rejects 94% precise); margin 8 → 35% at 86% with **auto-rejects 100% precise
   (13/13)**. coverage@90 ≈ 1% — the 90-bar needs better briefs, full stop.
4. haiku-4.5 rejected (quality loss, barely cheaper); medium effort no help.

## 🧬 Brief-enrichment verdict (2026-08-30) — the lever confirmed & ADOPTED

Same judge (v0 ×3), same holdout, ONLY the brief changed — from the deterministic
tag-count brief to a composed one (what Riverside sells, what agency-AE core
function means, concrete company-type/industry definitions; grounded in Jade's 10
ratings + the public JD — the Comeet JD field is empty):

| Agency AE (production basis, n=99) | old brief | **enriched brief** |
|---|--:|--:|
| AUC | 0.795 | **0.844** |
| κ_cv | 0.376 | **0.495** ✅ gate |
| accuracy | 0.687 | **0.747** |
| candidates in ≥80%-agreement bands | 64% | **88%** |
| τ | 24 | **33** |

**Adopted in production:** enriched brief written to `position_briefs` with
`locked=true` (the per-rating rebuild can't clobber it — pipey mirror respects
the lock), richbrief ×3 scores promoted, τ recalibrated. coverage@90 still ~0 —
next push: more teaching cases + recompose the brief from 20–30 ratings.

**Repeatable recipe for every position:** fetch the real JD (public posting if
Comeet's field is empty) + compose specific must-haves from the recruiter's
reason tags → validate on that position's decided-candidate holdout → lock.

**Eval hygiene note:** the 6-hourly corpus re-mine shifted holdout labels
mid-experiment (99→81 joinable). TODO: freeze holdout labels in a snapshot table
per experiment.

## 🧪 Sr PM enrichment test (2026-08-30) — brief lever, second data point

Noga's redo round (17 ratings, rejects included) re-measured with 3-run scoring:

| Sr PM (n=52 decided) | before redo | after redo |
|---|--:|--:|
| AUC | 0.708 | **0.741** |
| κ | 0.165 | **0.204** |

Still far below Agency AE (0.844 / 0.495). Two live hypotheses, not yet separated:
(a) Sr PM still has the THIN auto-generated brief — Agency AE's jump came from the
*enriched* brief; (b) Sr PM leans on non-CV signal (Yoni corrected a candidate from
LinkedIn, not the résumé). **Next test:** enrichment recipe on Sr PM — jump ⇒ briefs
confirmed as the lever; flat ⇒ real evidence about the role's CV-learnability.

## 🐞 Teaching-flow QA + fix (2026-08-30, pipey 9099e7a)

Adversarial QA confirmed the mixed-card-order / "!" incidents were a REAL
concurrency bug (deploy churn only amplified it): the rate→next-card path ran
entirely on the Bolt listener thread (PG mirror + 2 PG connects + Slack file ops
+ Comeet/S3 CV fetch with 5×60s retries) = 7–45s typical, minutes worst case,
**with the rated card's buttons still live** → re-click forked the flow (second
modal, duplicate live card, CV uploads deleting each other, double completion).

Fixed: instant card neutralization before slow I/O; already-rated clicks refused;
per-user single-flight lock with `next_unrated` re-read inside it; one-shot
completion guard (undo re-arms); bounded interactive CV fetch (Comeet 8s/2
retries, S3 streamed with 20s deadline + 12MB cap). Verified by concurrency test
+ live smoke (card in 4.3s, CV above card).
**Ops rule:** deploy only when no session has rated in the last ~30 min.

## ⬜ Next (in order)

1. **Deepen briefs — the only remaining AUC lever** (hand-built Eng Director brief
   hit 0.90 vs 0.795 today): (a) get Noga + Mor through their open rounds; (b) second
   teaching round for Jade (20–30 total cases); (c) LLM-composed brief from
   ratings+notes+JD, validated vs the deterministic builder on this same holdout.
2. **Ship the reject wedge** in Slack: candidates with fit ≤ τ−8 land in a
   "pre-rejected — one-click confirm" queue (100%-precision band today, protocol-safe).
3. **Confidence gate v1** for advances as ≥90% bands emerge with better briefs.
4. **Scale teaching** to remaining published positions (`kickoff <position> [@recruiter]`).
5. **Phase ② SHADOW for Agency AE** — continuous ×3 scoring + weekly agreement/drift.
6. Human-ceiling check: small double-labeling exercise (inter-recruiter κ) before
   chasing AUC > 0.9.

## 🔄 Also open

- Recruiter rounds: Noga (7-card reject-mix redo), Mor (10 cards) — both waiting.
- Human-ceiling question: recruiters may pass on non-CV signal (source/referral/LinkedIn)
  — measure inter-recruiter κ via a small double-labeling exercise before chasing AUC>0.9.

---

## 📍 Per-position state

| position | recruiter | taught | brief | phase | notes |
|---|---|---|---|---|---|
| Agency Account Executive | Jade | 10 (decisive ✅) | ✅ n=10 | **① → ② candidate** | AUC 0.75, τ=30 live; best prospect |
| Senior Product Manager | Noga | 10 (7 borderline ⚠️) + 7-card redo open | ✅ weak | ① | flat AUC ~0.71; redo should sharpen |
| Senior Frontend Infra | Mor | 0 (10 cards open) | — | ① | ~1 decided historically — teaches on fresh CVs |
| everything else | — | — | — | not started | corpus + kickoff ready when we are |

---

## ⚠️ Standing constraints & lessons

- **Shared Anthropic key, $15/day** — every scoring run is budget-capped; benchmarks
  charge failed calls too. Batch API for any full-corpus scoring later.
- **Fairness (DO-NOT-LEARN)** is enforced in code on every judge prompt: no accent /
  national origin / country-of-education / name-based inference, ever.
- **Rejections are never auto-final** — human-confirmed by protocol.
- Comeet quirks: live per-position feed hides rejects (use corpus); résumé URLs expire
  in 15 min (use `/cv/<uid>` redirect); internal DB hostnames don't resolve cross-region.
- Roles where CV signal is thin (Designer) will *never* clear the gates — that's the
  system working, not failing.

## 🔎 How to check where we are

```
# per-position learning curve + phase (run on auto-screener host)
python -m app.learning_curve <position_uid> [--recruiter <slack_id>]

# threshold + coverage (the goal metric)
python -m app.threshold <position_uid> --no-save

# benchmark comparison
python -m app.bench_screen analyze 48.16A

# in Slack (Comeet Helper)
screen brief <position>       # the learned brief
kickoff <position> [@person]  # start a teaching round for its recruiter
```
