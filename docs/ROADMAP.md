# Auto-Screener Roadmap — Where We Are

**Last updated:** 2026-09-27 · **Owner:** Yoni · **Companions:** `REDESIGN.md`, `OPERATING-PROTOCOL.md`, `TEACHING-LOOP-V1.md`

---

## 🎯 North star

> **An auto-screener that decides 70–90% of incoming CVs on its own — high-confidence
> advances and rejects — and routes only genuinely uncertain ones to a human.
> Rejections always stay human-confirmed.**

### The metric is TWO numbers, not one

Coverage is not symmetric, and treating it as one number hid this for a month.

- **`reject_coverage@P`** — share of the funnel auto-rejectable at precision ≥ P
- **`advance_coverage@P`** — share auto-advanceable at precision ≥ P

They move in **opposite** directions with the position's pass rate `p`:

| pass rate | auto-reject | auto-advance |
|---|---|---|
| low (BDR, 17%) | easy — most of the funnel is genuinely a no | near-impossible |
| high (Sr PM, 66%) | little to gain | easy |

**Hard ceiling (arithmetic, not model quality):** auto-advancing the top `q` of the
funnel caps at precision `p/q`. Advancing the top 20% at 85% therefore *requires*
`p ≥ 23.5%`. Below that, no amount of teaching can reach it — the passes do not exist
to fill the slice.

---

## 0️⃣ TRIAGE — do this before teaching anything

One free query decides which end of the funnel a position can automate:

```sql
SELECT position_uid, max(position_name),
       count(*) FILTER (WHERE screen_label=1 AND has_resume) pass,
       count(*) FILTER (WHERE screen_label=0 AND has_resume) rej
FROM corpus_screen_labels GROUP BY position_uid;
```

- `p ≥ 23.5%` → **advance-side candidate**, chase `advance_coverage`
- `p < 23.5%` → **reject-side only**, chase `reject_coverage` — and that is fine
- labelled `< ~150` → **not measurable yet**; fix labels first (§ Blockers), don't teach

As of today only **4 of 23** measured positions can ever support top-20% auto-advance
(Sr PM 66%, Agency AE-EMEA 38%, Agency AE 32%, Sr Backend Platform 29%). The other 19
are reject-side plays. That is not failure — it is most of the 70–90%.

---

## 🗺️ The ladder (per position — every role earns autonomy separately)

```
① TEACH    recruiter rates ~10 boundary-spanning CVs in Slack (Advance/Reject + why)
   │        → builds the position BRIEF (criteria in the recruiter's own words)
   ▼
② SHADOW   judge scores incoming CVs; takes NO action, nobody is notified
   │        → measure agreement vs real recruiter decisions, leakage-free
   ▼        → gates to leave ②: AUC ≥ 0.80, κ_cv ≥ 0.45, curve plateaued
③ SELF     judge acts ONLY inside bands meeting the precision bar for that side;
            everything else → human. Rejections stay one-click-confirm. Drift → ②.
```

**Shadow costs the recruiter nothing.** It is scoring + storage. Any position with a
brief should be in shadow permanently; there is no reason to gate it.

---

## 📍 Per-position state (measured, leakage-free, current briefs)

| position | uid | taught | brief | AUC | κ_cv | τ | pass rate | phase | play |
|---|---|---|---|---|--:|--:|--:|---|---|
| Agency Account Executive | 48.16A | Jade, 10 | ✅ n=10 🔒 enriched | **0.841** | **0.556** | 33 | 32% | ② gates passed | either side; reject ≤10 = 31% @ 93% |
| Business Development Rep | 41.264 | Yoni, 19 | ✅ n=19 | **0.807** | **0.500** | 44 | 17% | ② gates passed | **reject wedge** ≤20 = 26% @ 97% |
| Senior Product Manager | 99.667 | Noga, 17 | ✅ n=17 | 0.851 | 0.412 | 33 | 66% | ② κ short | advance side looks strong, n too small |
| Senior Frontend Infra | EE.E69 | Mor, 10 | ✅ n=10 | — | — | — | 4% | ① never scored | reject-side; needs a shadow run |
| Controller | AE.078 | Gili+Yoni, 14 | ✅ n=14 | — | — | — | — | req CLOSED | history lost; recoverable via `--all` mine |

**Caveats worth carrying:** BDR is a single measurement on a brief built the same day —
do not lock τ=44 on it. Sr PM's numbers rest on 38 held-out candidates and its corpus
is capped at 43 (see Blockers).

---

## ✅ What we have actually proved

| date | finding | evidence |
|---|---|---|
| 08-28 | Teaching transfers at all | Jade's round → AUC 0.756 on 50+50 holdout |
| 08-28 | **Binary teaching beats 3-way** — "hard to tell" as a reason, not an option | Noga's 7/10-borderline round taught ~nothing |
| 08-29 | **Prompt redesign is a dead end** — compression is an honest read, not framing | full n=99 ×1 and ×3; both panel prompts lost to v0 |
| 08-29 | 3-run ensemble is real but modest (+0.01 AUC, +0.04 κ) | adopted as production judge |
| 08-30 | **Brief enrichment is the biggest known lever** | Agency AE 0.795 → **0.844**, κ 0.376 → 0.495 |
| 09-26 | **One good round can clear both gates** | BDR: 7 new cards, n=19 brief → 0.807 / 0.500 first try |
| 09-26 | **Auto-advance is base-rate capped** | top-20% ceiling = p/0.20; BDR maxes at 83.5% |
| 09-26 | Reject side is where coverage lives on low-p reqs | BDR ≤25 → 39% of funnel at ~97.5% |

**Repeatable brief recipe:** real JD (public posting if Comeet's field is empty) +
must-haves composed from the recruiter's own reason tags → validate on that position's
decided holdout → lock.

---

## 🧱 What is ACTUALLY blocking (not teaching)

These cost more progress than brief quality ever did.

**1. Nothing runs on a schedule.** The three auto-screener crons all belong to the
legacy v2 scorer. The taught judge has **no cron**. It has produced **296 scores in 7
weeks, across 7 distinct days** — every one a day someone SSH'd in and fired it by
hand. 10,991 labelled candidates sit unscored. *Fix: nightly scoring cron + weekly
metrics roll-up into `position_taus`.* **Highest leverage item on this page.**

**2. The label classifier discards ~1,066 valid labels.** `classify()` requires a step
whose name contains `"cv screen"`. Reqs that reject straight from the application, or
name the step differently, lose their history:
  - **987** candidates rejected with reason *"Doesn't Meet Minimum Qualifications"* —
    the cleanest CV-only rejections that exist — are labelled NULL
  - **79** who reached `Phone screen / Recruiter` or later are dropped instead of
    labelled 1
  - **213** *"Rejected by knockout questionnaire"* must stay excluded — automated form
    rule, no human read the CV. **Do not loosen this one.**
  - Effect: BDR labels at 90%, Sr PM at 8.5% — a workflow-config difference, not a
    role difference. Sr PM's ceiling of 43 holdout candidates is entirely this.

**3. Corpus history was being destroyed.** *(fixed 2026-09-26, `64d15d3` + `844f54b`)*
The miner dropped and rebuilt the table every run from open reqs only — so a position's
whole screening history was erased the day it was filled, and 463 closed reqs were never
mined at all. Now an upsert; a req mined while open keeps its history after closing.
Scope is active-only by request; `--all` is a one-off to recover already-closed reqs
(Controller included).

---

## ⬜ Next (ordered by leverage, not by appetite)

1. **Nightly judge cron + weekly roll-up.** Turns seven snapshots into an actual curve
   and makes every other item cheaper. ~1h.
2. **Fix `classify()`** — disposition-reason allowlist → label 0; post-screen interview
   step → label 1; keep knockout excluded. +~1,066 labels, rescues Sr PM and the other
   thin reqs. ~15 lines.
3. **Ship the reject wedge on BDR** at fit ≤ 20 (~19 CVs/week, ~97% precision), as a
   one-click-confirm queue to whoever screens the req day-to-day. Overrides become
   fresh labels on exactly the population being automated.
4. **Shadow everything taught** — Sr FE Infra and Sr PM have briefs and have never been
   scored on a schedule. Free once (1) exists.
5. **Teach in parallel, not serially.** Four reqs are ready with 1,000+ labelled
   candidates each: Mor/Senior Backend Eng, Kourtney/SMB CSM, Jayme/AE-EMEA,
   Noga/DevOps. ~20 recruiter-minutes each.
6. **Enrich the BDR brief** (recipe above) and re-measure — the one lever known to move
   AUC materially.
7. Human-ceiling check: inter-recruiter κ via small double-labelling, before chasing
   AUC > 0.9. Recruiters use source/referral/LinkedIn signal absent from the CV.

---

## ⚠️ Standing constraints & lessons

- **Fairness (DO-NOT-LEARN)** is enforced in code on every judge prompt: never use or
  infer accent, national origin, country of education, years-in-country, or
  name/photo-based nationality/gender/age. Job-relevant evidence only. Work
  authorisation and genuine language requirements are legitimate and separate.
- **Rejections are never auto-final** — human-confirmed by protocol.
- **Pedigree filters are actively harmful here.** On BDR hire outcomes, prior BD
  experience, outbound volume and university tier all failed to separate good hires
  from mishires; a screen weighting them ranks the mishires first.
- Comeet quirks: live per-position feed hides rejects (use the corpus); résumé URLs
  expire in 15 min (use `/cv/<uid>`); internal DB hostnames don't resolve cross-region;
  `/candidates/<uid>/files` holds reference PDFs only — never the CV, never the offer;
  numeric UI ids 404 the API (resolve to the alphanumeric uid first).
- Roles where CV signal is thin (Designer, 20% pass, 33 passes) will not clear the
  gates — that is the system working.
- **Ops rule:** deploy only when no session has rated in the last ~30 min.

## 🔎 How to check where we are

```bash
# per-position learning curve + phase
python -m app.learning_curve <position_uid> [--recruiter <slack_id>]

# threshold + both coverage sides (the goal metric)
python -m app.threshold <position_uid> --no-save

# leakage-free measurement against decided history (~$1.80 / 100 candidates)
python -m app.rejudge <position_uid> --per-class 50

# recover history for already-closed reqs (one-off, slow)
python -m app.corpus.mine_screen_labels --all
```
