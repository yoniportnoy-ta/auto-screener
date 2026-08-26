# Auto-Screener — session handoff (2026-05-23)

Continuing from the 76.85A trial calibration. This file exists so a fresh
Claude Code session can pick up where the desktop app session left off.

## Where we are

- 76.85A ("Engineering Director, Editor Platform", engineering_leadership,
  Israel) trial calibration is **complete**: 25 verdicts across 5 rounds.
- Benchmark showed AI over-rating +1.8 → 0.0 across rounds, but AI collapsed
  to a uniform "3" in R4-R5 (σ ratio dropped to 0.0). One false negative
  (C3.AA63E), one dirty verdict (stale enrichment cache from before the
  candidate_enrichment cleanup deploy).
- Slack skill files still work; production is deployed to
  `https://auto-screener-2va5.onrender.com` (Render service
  `srv-d803r87aqgkc739p4mrg`).

## What shipped this session (M-E milestone, live in prod)

Two commits went live earlier: **682d96d** (M-E improvements) and **56d1d40**
(soft-reset endpoint). Both `status: live`.

1. **Shared benchmark helper** — `app/benchmark_stats.py`. Single source of
   truth for bias, MAE, RMSE, σ ratio (discrimination), FN/FP counts, dirty
   verdict filter, per-axis tallies. Called from batch-complete + finalize
   + CLI benchmark.
2. **Bucket boundary tightened** — `app/calibration.py::rating_to_verdict`
   is now (1-4 → down, 5-6 → question, 7-10 → up). Was 1-3 → down.
3. **Discrimination metric** — batch-complete + finalize + CLI benchmark
   surface `std_ai`, `std_recruiter`, `discrimination_ratio`. Round-summary
   UI shows red "AI collapsing" tag when ratio < 0.5.
4. **False-negative + false-positive tracking** — `false_negatives`
   (AI≤3 AND Rec≥6) and `false_positives` (AI≥7 AND Rec≤3) surface as
   counts on every batch payload + meta-benchmark headline. CLI benchmark
   prints them with the threshold labels inline.
5. **Cache-error verdict filter** — verdicts whose `feedback_text` matches
   `/Error code|extraction failed|not_found_error|model:\s*claude-|invalid_request_error|api[_\s-]?error/i`
   are moved to a `dirty` bucket and excluded from every numeric metric.
   Count surfaces as `dirtyCount` on both endpoints; CLI prints a "dirty
   verdicts excluded" table when any are present.
6. **Position brief: industry preferences** — new columns
   `position_classes.industries_up` and `.industries_down` (Alembic
   migration `0015_industries`). Brief screen UI adds two optional
   textareas ("industries to weight up / down") with per-textarea hints.
   Locked view shows them as inline chips beneath the saved brief. Scoring
   prompt gets a labelled `[INDUSTRY PREFERENCES on this position]` block
   with directive language ("weight up / weight down") — same injection
   pattern as `recruiter_notes`. Both batched scan flow and the extension's
   one-shot flow inject the block.
7. **Soft-reset endpoint** — `POST /api/position/soft-reset` wipes
   `debug_scoring` + position-scoped `learned_rubric` + `candidate_enrichment`
   + `score_done` locks. **Keeps** `calibration_verdicts`, `feedback`,
   `recruiter_thresholds`. Sibling of the existing `/position/full-reset`
   which wipes everything.

## Immediate next step (blocked on user)

Yoni asked me to fire the soft-reset on 76.85A but my sandbox is blocked
from the production host by outbound-allowlist. He needs to run it from
his machine or DevTools. Two ways:

```bash
curl -X POST https://auto-screener-2va5.onrender.com/api/position/soft-reset \
  -H "Content-Type: application/json" \
  -d '{"position_uid":"76.85A"}'
```

Or in the app's DevTools console:

```js
fetch("/api/position/soft-reset", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({position_uid: "76.85A"})
}).then(r => r.json()).then(console.log)
```

After that fires, he'll start a fresh calibration session on 76.85A from
the UI. The queue will lazily rescore candidates with the new code path
(4→down boundary, dirty-cache guard, industry-block prompt section IF he
filled the new brief fields).

## What to watch for on the fresh calibration

- **σ ratio should stay above 0.5** across all 5 rounds. If it collapses
  again in R4-R5 the rubric-synth step is over-applying feedback. Task
  #1 in that case is inspect the position-specific rubric right after R3.
- **FN count should stay at 0 or 1**. FN=2+ means the AI is being too
  cautious at the bottom of the range.
- **dirtyCount should be 0** now that candidate_enrichment was wiped for
  the position.

## Positions rated and not yet re-verified

Only 76.85A. Yoni was going to fire soft-reset then re-run calibration
under the new code. Other open positions still have scores from the pre-M-E
prompt — no rush to rescore them until we've validated the trial position
runs clean.

## Key files touched this session

- `app/benchmark_stats.py` (new)
- `app/calibration.py` — `rating_to_verdict` boundary
- `app/routes/api.py` — batch-complete + finalize + `/onboarding/brief`
  + new `/position/soft-reset`
- `app/cli.py` — `cmd_benchmark` uses shared helper, prints new stats
- `app/position_classes.py` — `set_/get_industry_preferences`,
  `format_industry_block`
- `app/scan.py` + `app/scan_session.py` — inject industry block into
  process_ctx for both scan paths
- `app/models.py` — `industries_up`, `industries_down` on `PositionClass`
- `alembic/versions/20260523_0015_industries.py` (new)
- `app/templates/index.html` — brief screen industry fields, mini-chart
  diagnostics (σ ratio + FN/FP + dirty banner), meta-benchmark headline

## Rules of engagement Yoni set

- **DO NOT** fire scoring endpoints from Claude side — it burns his
  personal Anthropic API budget. That includes `/api/scan/now`, individual
  rescores, or /api/calibration/queue when the queue would auto-fill.
- OK to hit read-only endpoints (deploy status, DB queries, logs) and
  destructive endpoints that don't call Claude (soft-reset, full-reset).
- Ask before global (all-position) resets. He specifically approved
  76.85A-only for this session.

## Tasks

Task list in the desktop app has #171 (M-E umbrella) + #172-176 (all five
sub-tasks) marked **completed**. #170 (76.85A trial) completed. Everything
else was pre-existing state.
