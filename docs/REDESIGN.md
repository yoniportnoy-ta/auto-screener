# Auto-Screener — Redesign: *Teach, Prove, then Delegate*

**Status:** Draft v1 — for review
**Date:** 2026-08-09
**Author:** Yoni + Claude Code session
**Supersedes the direction in:** `HANDOFF.md` (2026-05-23), README "Status" phases

---

## 1. Why we're rethinking it

The current system tries to **learn an absolute 1–10 scoring function from live recruiter
feedback**, online and compounding, with **no held-out check**. This is the root cause of the
failure observed on the 76.85A trial (AI collapsed to a uniform "3" in rounds 4–5, σ ratio → 0):

- Feedback of the form *"you scored too high / too low"* mathematically drags every score toward
  the mean. Discrimination dies. This is a **structural** property of the loop, not a tuning bug.
- Learning and evaluation happen in the **same** calibration rounds, so there is no way to know
  whether the model has actually *learned to screen* versus *learned to please the last few clicks*.
- The plan was to grant decision rights (auto-tagging candidates) before the model had ever passed
  a clean, held-out exam.

**Guiding principle (Yoni):** *first teach the AI to screen correctly, and only then allow it to
make real decisions on CVs.*

This document reframes the whole system around that principle.

---

## 2. Decisions locked for this version

These were chosen deliberately; revisit explicitly if we change our minds.

| Decision | Choice | Rationale |
|---|---|---|
| **Ground-truth target** | **Recruiter decisions (proximal)** | Match what recruiters decided at screen. Most data, fastest to stand up. We accept that it encodes recruiter noise/bias; we mitigate by measuring the human ceiling and auditing for adverse impact. Downstream hire outcomes are kept as an *optional later validation signal*, not the primary target. |
| **Teaching data** | **History-first** | Mine the existing Comeet decision history (hundreds–thousands of already-labeled candidates) as the training + gold corpus. Live human calibration becomes a small, targeted, active-learning supplement — not the main engine. |
| **Autonomy model** | **Earned ladder, rejections human-confirmed** | The AI climbs one rung only after clearing a numeric gate on a frozen gold set + sustained shadow agreement. It never silently rejects a CV. |

---

## 3. The four shifts

| Old design | New design | Why |
|---|---|---|
| Learn an **absolute 1–10 score** | Learn a **ranking / pairwise preference** for a role | You can't collapse a ranking the way you collapse a score. Screening is rank-and-threshold, not absolute grading. |
| Ground truth = **25 live verdicts** | Ground truth = **mined Comeet decision history** | Teach from hundreds–thousands of already-decided candidates, not 25 fresh clicks. |
| Learner == evaluator (same rounds) | **Frozen gold test set**, never learned from | "Prove it learned" is impossible without a held-out exam. |
| Autonomy is a switch | **Autonomy is an earned ladder** | Operationalizes "teach before deciding." |

---

## 4. The maturity ladder (system spine)

The AI climbs one rung only after clearing its gate (§7) on the frozen gold set **and** sustaining
agreement in shadow mode. It is **auto-demoted** if live metrics drift below the gate.

```
L0  TEACH       — learns from mined history; scores nothing live
L1  SHADOW      — scores every live candidate, takes NO action; we compare to recruiters
L2  ASSIST      — pre-sorts & suggests; recruiter confirms every decision
L3  AUTO-YES    — auto-advances only the clear top band; every "no" stays human-confirmed
L4  AUTONOMOUS  — decides within a confidence band; abstains & routes hard cases to humans
                  (may be a rung we deliberately never take for rejections)
```

**Hard rule:** the AI never silently rejects a CV. Auto-*advancing* a strong candidate is low-risk;
auto-*rejecting* is high-risk. Hiring is a "high-risk" use under the EU AI Act — audit logs + human
oversight are effectively required — so rejections stay human-confirmed until real adverse-impact
data says otherwise.

---

## 5. Phase-by-phase plan

### Phase 0 — Ground truth & problem definition *(the real starting point)*
**Read-only. No Claude scoring cost.**
- **Mine Comeet history** into a labeled corpus. Per past candidate → features (CV text, parsed
  structure, JD, `position_class`) + label = **recruiter screen decision (advance / reject)**.
  Capture furthest workflow step + hire flag too, as an *optional* future validation signal.
- **Measure the human ceiling.** Compute inter-recruiter agreement (Cohen's κ) on any candidate
  seen by 2+ recruiters. This sets the realistic target and reveals label noise.
- **Freeze a gold test set** (~15–20%), stratified by position class, era, and outcome. Never used
  for teaching.

**Deliverable:** `app/corpus/` mining module + a versioned labeled dataset + a one-page data report
(counts per position class, label balance, inter-recruiter κ, gold-set composition).

### Phase 1 — Eval harness *(build before any teaching)*
- Metrics (see §7). Primary is **ranking agreement**, not absolute error.
- **Promotion-gate table** wired into an `app/eval/` runner that scores the frozen gold set and
  prints pass/fail per rung.

**Deliverable:** `app/eval/` runner + baseline report card.

### Phase 2 — Rebuild the judgment engine
- **Decompose** the monolithic score into JD-derived **dimensions** (extend `dimensions.py`): score
  each dimension 1–5 **with a cited evidence span from the CV**, then combine.
- **Comparative calibration**: pairwise "A vs B for this role" instead of absolute grades.
- **Modern-model leverage:**
  - Opus 5 / Sonnet 5 with **extended thinking** → the reasoning trace is the audit log.
  - **Structured outputs / tool-forced JSON** → per-dimension scores can't come back malformed →
    **eliminates the "dirty verdict" class at the source** (retire the regex error-filter).
  - **Self-consistency** (sample N, aggregate) → counteracts discrimination collapse + single-sample
    noise.
  - **Prompt caching** → big rubric + JD + anchors become ~free per candidate.
  - **Embedding retrieval for anchors** → fetch k most-similar *already-decided* candidates as fixed
    few-shot exemplars (ground truth that can't drift) — replaces synth-rubric drift.
  - **Batch API** → cheap bulk shadow scoring, off the interactive budget.
  - *Fine-tuning:* skip for now; in-context + retrieval wins at this data scale. Revisit only past
    several-thousand labeled examples.

**Deliverable:** new judge module producing per-dimension, evidence-cited, structured scores.

### Phase 3 — Teaching loop (calibration, done right)
- **Active learning:** calibrate on the candidates where the model is most *uncertain* or most
  *disagrees* with history — maximum information per human click, far fewer clicks.
- **Pairwise sessions** build a preference/ranking model; **regularize toward spread** so feedback
  can never crush discrimination.
- **Freeze anchors, version the rubric, regression-test every change against gold.** No change ships
  if it regresses the frozen set. (This is the guardrail the old system lacked.)

**Deliverable:** pairwise-calibration UI + preference model + versioned-rubric regression test.

### Phase 4 — Shadow mode *(teach-before-decide, live)*
- AI scores every incoming candidate, **takes no action.** Recruiter decides independently. Log both.
- Weekly agreement dashboard + drift tracking. This is the real-world exam that earns L2.
- **Adverse-impact audit:** compare the advance rates the AI *would* produce across university tier,
  geography, career-gap presence, etc. Flag disparate impact before it touches a live decision.

**Deliverable:** shadow logging + agreement/drift dashboard + bias-audit report.

### Phase 5 — Staged autonomy & governance
- Promote rung-by-rung per §7 gates. Every automated action writes an **audit record** (inputs,
  reasoning trace, decision, model version). Candidate-facing transparency + appeal path. Kill-switch
  + auto-demotion on drift.

---

## 6. What "technology got better" actually buys us

| Problem in the old system | New capability | Effect |
|---|---|---|
| Discrimination collapse | Self-consistency + ranking objective + spread regularization | Structurally resists collapse |
| "Dirty verdict" API-error rows | Structured outputs / tool-forced JSON | Bad rows can't exist |
| Rubric synth drift | Embedding retrieval of fixed decided exemplars | Anchors are ground truth, can't drift |
| Opaque single score | Extended-thinking reasoning trace + evidence citations | Auditable, debuggable, defensible |
| Cost of rich context | Prompt caching + Batch API | Rich prompts + bulk shadow scoring affordable |
| Small-n calibration | Active learning over mined history | Fewer human clicks, more signal |

---

## 7. Metrics & promotion gates

**Metrics**
- **Ranking agreement** — Spearman / Kendall τ; **top-k precision** (of the k the AI ranked highest,
  how many did recruiters advance?). *Primary.*
- **Decision agreement** at threshold — Cohen's κ; FN/FP at the decision boundary.
- **Discrimination** — σ ratio (std_ai / std_recruiter). Now a first-class objective.
- **Calibration curve** — when the AI says "8," how often is that candidate actually advanced?
- **Abstention rate** — how often it correctly says "not sure → human."

**Gates** *(illustrative — tune to the measured human ceiling from Phase 0)*

| Rung | Gate on frozen gold set |
|---|---|
| → L1 Shadow | Spearman ≥ 0.55, σ-ratio ≥ 0.7, zero parse/error rows |
| → L2 Assist | Top-k precision ≥ (human ceiling − 5 pts), κ ≥ 0.45, calibration slope 0.8–1.2 |
| → L3 Auto-yes | Top band only: precision ≥ 0.90, zero adverse-impact flags, ≥ 4 weeks shadow-stable |
| → L4 | Separate decision + fairness sign-off (may never be taken for rejections) |

---

## 8. Fairness & governance (non-negotiable for CV decisions)
- **Human ceiling first** — never ask the AI to beat an agreement level humans don't reach.
- **Adverse-impact audit** every shadow cycle across non-protected proxies we can see (university
  tier, geography, career gaps) — note: inferring protected attributes (e.g. gender from names) is
  itself risky; prefer aggregate rate comparisons over per-candidate demographic inference.
- **Audit log** for every automated action (EU AI Act high-risk expectation).
- **Human-confirmed rejections** until data justifies otherwise.
- **Kill-switch + auto-demotion** on metric drift.

---

## 9. Codebase impact

**Reuse:** `dimensions.py`, `benchmark_stats.py` (σ/FN/FP already present), `calibration.py`,
`company_tiers.py` / `university_tiers.py` / `geo_overlays.py` (great fairness-audit axes),
`comeet_client.py` / `comeet_app_client.py`.

**Build:**
- `app/corpus/` — Comeet history → labeled dataset (Phase 0)
- `app/eval/` — frozen gold set + gate runner (Phase 1)
- retrieval-based anchors (embeddings) (Phase 2)
- pairwise-calibration UI + preference model (Phase 3)
- shadow-mode logging + agreement/drift dashboard + bias audit (Phase 4)

**Retire / rework:**
- online rubric-synthesis that compounds (`rubrics.py` drift path)
- absolute-score-from-live-feedback path
- regex "dirty verdict" filter (obsoleted by structured outputs)

---

## 10. Rules of engagement (carried over from HANDOFF.md)
- **Do NOT** fire Claude-scoring endpoints from the Claude side (burns personal Anthropic budget):
  no `/api/scan/now`, individual rescores, or auto-filling calibration queues.
- OK: read-only endpoints (deploy status, DB queries, logs) and non-Claude destructive endpoints
  (soft-reset, full-reset).
- Ask before any global (all-position) reset.
- **Deploy is currently suspended** (all auto-screener Render services suspended 2026-06-25). Phase 0
  is local/read-only and does not require un-suspending.

---

## 11. Suggested first three moves
1. **Phase 0 data pull** — mine Comeet decision history into a labeled corpus + measure inter-recruiter
   κ. Cheap, read-only, no scoring cost; tells us whether there's enough signal to teach from.
2. **Stand up the eval harness + frozen gold set** with the §7 metric/gate table.
3. **Baseline the gold set once** with the new decomposed + structured judge — the first honest
   measurement of how good it already is.

---

## 12. Phase 0 findings (2026-08-23)

Ran a read-only data-availability assessment against the live (un-suspended) service + DB.

**Corpus is rich and history-first is clearly viable.**
- One open position (48.358) alone has **1,285 candidates** (1,055 Rejected, 122 blank, 50 Withdrawn,
  50 In progress, 4 Hired, 4 On hold). 30 open positions + closed ones ⇒ plausibly **10k–40k labeled
  CVs**.
- **2,820 candidates already AI-scored** with full per-dimension breakdowns in `debug_scoring`
  (2,842 rows, 28 positions) — reusable at **zero scoring cost**. Their rating distribution is visibly
  collapsed (spikes at 3 = 892 and 5 = 867, valleys at 4/6, no 10s) — direct evidence of the
  discrimination pathology.
- **137 direct (AI, recruiter) rating pairs** in `feedback` (9 positions, 6 recruiters); **178
  calibration verdicts** (8 positions).

**The recruiter decision label is cleanly encoded on the Comeet candidate object:**
- `status` — Rejected / Hired / Withdrawn / In progress / On hold / '' (final outcome)
- `completed_steps[]` — each has `name`, `type`, `time_completed`, `assignees[]` (deciding recruiter
  + email). The CV-screen gate is the step **`"CV Screen / Recruiter"` (type `"Go/No-go"`)**.
- `resume` (CV), `source`, `disposition_reason`, `tags`, `linkedin_url`.
- `{"deleted": true}` tombstone rows exist → filter out. Blank-status rows exist → handle explicitly.

**Label definition (Phase 1 decision):** the training label is *"did the recruiter advance the
candidate past the CV Screen / Recruiter Go/No-go?"* — NOT `status != Rejected`, because a candidate
can pass the screen and be rejected later at interview. We teach the *screen* decision.

**Open questions still live:**
- **κ human-ceiling:** no double-labeling exists (workflow = 1 screener per candidate). Needs a small
  deliberate exercise (~30–50 candidates × 2–3 recruiters). Per-recruiter base rates ARE measurable now.
- Per-position vs. global model: start global, specialize per position class once volume allows?
- Legacy 1–5 scale still hard-coded in `/position/breakdown` + `/position/agreement-matrix` while
  scoring emits 1–10 — reconcile during the rebuild.
- Shadow-mode scoring host: reuse the now-live service (Batch API) or run locally.

**Deploy state:** web service + DB un-suspended 2026-08-23 for this assessment; the 3 cron jobs
(scan / feedback-poll / prewarm) deliberately left suspended to avoid burning Anthropic budget.

---

## 13. Rater-variance handling (from the Phase 1 finding)

**The finding:** CV-screen pass rates vary **8%→58% across screeners** (kourtney 8%, jayme 16%,
yuval 19%, jade 32%, noga 49%, gili 58%). Two of the strictest recruiters (jayme + kourtney) produced
~64% of all labels. So a naively pooled "global correct screen" model would (a) fit a muddled average
of contradictory cutoffs and (b) inherit a reject-bias from the high-volume strict screeners.

**Consequence:** "recruiter decision" is a *rater-dependent, noisy* label. We must not fit one global
binary classifier on pooled labels.

**Design decision — separate ranking from threshold (option B, + A):**
- Learn a single **quality/ranking score** `q(CV, position)` shared across recruiters — the thing
  recruiters (hypothesis) mostly agree on.
- Apply a **per-recruiter decision threshold** `τ(recruiter)` calibrated to each screener's base rate
  — the thing they demonstrably disagree on.
- Treat **recruiter identity as a modeled variable** (available on `completed_steps.assignees`), so the
  system can either condition on the assigned screener or marginalize to a "house" cutoff.
- This is *why* the whole redesign pivots to ranking: recruiters differ on where to cut, not (as much)
  on who's better. Ranking + calibrated thresholds fits that reality; absolute-score classification
  does not.

**Rejected alternatives:** naive pooled global classifier (bakes in strict-screener bias);
full Dawid-Skene/IRT latent-truth model (data-hungry, complex — revisit if needed).

**Validation overlay (later, not a training target):** downstream outcomes (advanced far / hired) as
a tie-breaker to audit *whose* screen calls were "right." Sparse today (~4 hires on a 1,285-candidate
position) but worth tracking.

**Concrete Phase 1 analysis to run on the full corpus:** test the shared-ranking hypothesis — is the
AI-rating→pass-rate curve monotonic *within each recruiter* (shared ranking) but *shifted* (different
thresholds)? If yes, option B is validated directly. Also: between-recruiter variance is our best
*upper bound* on achievable agreement until a deliberate double-labeling exercise measures κ.
