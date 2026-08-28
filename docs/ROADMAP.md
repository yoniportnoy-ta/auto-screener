# Auto-Screener Roadmap — Where We Are

**Last updated:** 2026-08-28 · **Owner:** Yoni · **Companions:** `REDESIGN.md`, `OPERATING-PROTOCOL.md`, `TEACHING-LOOP-V1.md`

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

## 🔄 In flight right now (2026-08-28)

- **Prompt-variant benchmark** running on the render host (fixed 99-candidate holdout,
  50/50 pass-reject, budget-capped ≤$8):
  `anchored` (evidence rubric) · `percentile` (rank-in-pool) · `anchored_med` (more
  reasoning) · `anchored_haiku` (cheap model — sonnet-5 intro pricing ends 08-31).
  Judged on AUC, κ_cv, spread, **coverage@90/85**, $/candidate.
- **Recruiter rounds open:** Noga (7 cards, proper reject-mix redo), Mor (10), Jade done.
  Ratings watch armed — agreement updates as they rate.

## ⬜ Next (in order)

1. **Benchmark verdict** → adopt winning prompt as the production judge; self-consistency
   runs (×3) on the winner → variance as a second confidence signal.
2. **Recalibrate τ + re-measure coverage@90** with the winning variant. Target: a real
   number > 0. This tells us how far from 70–90% we actually are.
3. **Confidence gate v1**: auto-act only in ≥90% bands; wire "uncertain → human" routing
   into Slack (borderline queue to the recruiter).
4. **Scale teaching**: kickoff remaining published positions (binary rounds, ~5 min each);
   `kickoff <position> [@recruiter]` in Comeet Helper.
5. **Phase ② SHADOW for Agency AE** (first candidate to graduate): judge scores incoming
   CVs continuously; weekly agreement + drift readout.
6. **Then ③ SELF** behind both gates — auto-advance only at first; rejects stay
   human-confirmed queues.

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
