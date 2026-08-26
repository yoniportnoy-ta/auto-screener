# Operating Protocol — Per-Position Graduated Autonomy

**Status:** Design, agreed 2026-08-26 · **Companions:** `REDESIGN.md`, `TEACHING-LOOP-V1.md`

Supersedes the generic maturity ladder with a **per-position** version (validated: the lift lives in
each position's own tagged criteria — generic dimensions alone don't transfer; §validation below).
**No position-class briefs** — each position earns its own autonomy, or doesn't.

---

## The lifecycle (every new position runs through this)

```
NEW POSITION
   │
   ▼
① TEACH  — recruiter rates + reasons on ~10 actively-selected candidates   (AI takes NO action)
   │        → builds this position's brief (criteria + do-not-learn)
   ▼
② SHADOW + CORRECT — AI scores incoming; recruiter corrects                (AI still doesn't decide)
   │        → each correction refines the brief; track rolling agreement
   ▼
③ SELF — AI rates on its own, behind two gates:
          • CONFIDENCE gate    — acts only when sure; unsure → human
          • LEARNING-CURVE gate — stays in ③ only while agreement holds; drop → demote to ②
```

Two invariants across ALL phases:
- **Do-not-learn fairness filter** (accent / national origin / country-of-education / name-photo-age).
- **Rejections stay human-confirmable** (legal / ethical).

---

## Phase mechanics

### ① TEACH — 10-round rate + reason (per position cold-start)
- **Active selection, not random:** surface the most *informative* candidates — highest model
  uncertainty + a spread of clear-strong / clear-weak / borderline, and **both passes and rejects**
  (so it learns the boundary, not just the "no" side).
- Recruiter gives: decision (advance / borderline / reject) + **reason tag(s)** + optional note.
- Output: the position brief (criteria mapped to dimensions; role-specific + cross-cutting).
- ~10 rounds is the default; if agreement hasn't stabilized, run a few more. If a position can't
  reach the bar after N rounds → flag **"not AI-suitable, stays human"** (see Designer, below).

### ② SHADOW + CORRECT
- AI scores every incoming candidate with the brief; **takes no action**.
- Recruiter reviews; corrections refine the brief. Track **rolling agreement** on a held-out slice.
- Graduate to ③ only when agreement clears the bar (tune to the human ceiling per `REDESIGN §7`).

### ③ SELF — gated autonomy
- **Confidence gate** — the linchpin of "insecurity → human." AI emits advance / **borderline** /
  reject + a **calibrated** confidence; the borderline band is *defined* so it maps to real
  uncertainty (e.g. only auto-act where historical agreement in that score band > 90%). Everything
  else routes to a human.
- **Learning-curve gate** — autonomy is *kept* only while rolling agreement stays above the bar.
  Drift → auto-demote to ②. A position that never reaches the bar never enters ③.

---

## Why the gates matter (they auto-handle the failure modes we found)
- **Designer** — portfolio signal isn't in the CV, so agreement never reaches the bar → the position
  **never graduates to ③, stays human forever.** The system self-selects the roles it can help with.
- **Drift** on a role that was working → learning-curve gate demotes it back to shadow.
- **Uncertain individual cases** in a working role → confidence gate routes them to a human.

Teaching-loop applicability (from `TEACHING-LOOP-V1`): a role lifts **iff** the signal is (a) in the
input we feed the model, (b) CV-extractable + model-known or KB-backed, and (c) the position is tagged.

---

## UI / capture — NON-Render, meet recruiters where they work

No standalone web app. Three surfaces:

1. **Comeet-native reasons (steady-state, zero UI):** align Comeet's disposition/rejection-reason
   taxonomy to our criteria; recruiters pick a reason during their normal reject flow; we read it via
   the Comeet API we already mine. The "reasoning" is a free byproduct — infinitely scalable.
2. **Chrome extension (rich ① onboarding, inline in Comeet):** extend the existing `auto-screener`
   extension to show the AI score + rate+reason panel on the candidate page. Client-side; recruiters
   never leave Comeet.
3. **Slack (batch push + routing):** new-position "here are 10 cases to rate" cold-start, and
   confidence-gate routing ("below the bar — your call"), as interactive Block Kit messages.

Backend can be serverless (Vercel / Cloudflare Workers / Lambda) — the recruiter-facing surface is
Comeet + Slack + the extension, never a Render web app.

---

## Validation backing this design (2026-08-25/26)
- Per-position briefs lift: Eng Director AUC 0.899, BDR 0.814 (vs old 0.66–0.75).
- Generic dimensions w/o brief on an UNTAUGHT role (Agency AE): 0.739 ≈ old 0.748 → **no free
  transfer**; hence per-position ① is mandatory.
- Designer: tagging + KB both flat → **not AI-suitable via CV**; the learning-curve gate keeps it human.

---

## Build order (next sessions)
1. Phase ① flow — active-case selection + the rate+reason capture (Comeet reasons first; extension for rich onboarding).
2. Brief-builder — turn tags → position brief + dimension config.
3. Agreement tracker + the two gates (confidence, learning-curve) with auto-demote.
4. Slack routing for cold-start + borderline cases.
