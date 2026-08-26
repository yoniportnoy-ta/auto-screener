# Teaching-Loop Output v1 — Dimension Spec + Position Briefs

**Status:** Draft for review · **Date:** 2026-08-25
**Source:** 20 tagged disagreement cases (high-fit-REJECTED) — Engineering Director (6) + BDR Canada (8) + 6 mixed. Recruiter: Yoni (per-position framing; see attribution note).
**Companion:** `docs/REDESIGN.md`

> **Why this exists:** the current judge scores 5 abstract "how impressive is this CV" dimensions
> (profession, company_pedigree, career, location, education). They **saturate at the top** (rejected
> candidates score 5/5/5/5/4), so the judge is a good "no-filter" but a poor "yes-selector." The
> tagging session revealed the **fit-specific and disqualifier signals recruiters actually reject on**,
> which the judge doesn't model. All are CV-observable, so the judge can learn them.

---

## 0. Fairness — DO-NOT-LEARN filter (hard rule)

The screener must **never** key on these, and its rationale must never cite them:
- accent / spoken-English inference
- national origin
- **country of education**, years-in-country
- name-, photo-, or country-based nationality / gender / ethnicity / age inference

**Rationale:** these are protected-origin proxies. In an automated screener applied at scale they
produce national-origin **adverse impact** (Canadian Human Rights Act and equivalents) — the exact
risk the redesign's fairness track exists to prevent. During tagging, "language / country of
education" reasons (3 BDR cases) were logged as data but **excluded** from learnable criteria by this
rule. Genuine job-relevant needs are captured via evidence-based dimensions below, not origin proxies.

---

## 1. New / recalibrated dimensions

Derived from the tags; every one is observable in the CV itself.

| # | dimension | what it captures | replaces / adds |
|---|---|---|---|
| 1 | **seniority_fit** | over/under-qualified vs the role's target level; penalize too-senior/expensive | NEW — biggest gap (5 cases, both roles) |
| 2 | **company_type_fit** | fast-moving software **startup/scaleup** DNA; downweight enterprise/heavy, hardware, cyber, big-corp(-acquired) | **replaces brand-based `company_pedigree`** |
| 3 | **industry_fit** | role-specific domain (BDR→SaaS sales; Eng→software product) | NEW (ties to existing `industries_up/down`) |
| 4 | **core_function_present** | does the CV actually show the role's core function performed (e.g. real sales experience), not just adjacent-impressive background | NEW |
| 5 | **employment_recency** | significant current gap / not currently employed (context-dependent, not auto-disqualifying) | NEW |
| 6 | **tenure_pattern** | penalize over-long tenures (slow-moving signal); likely also job-hopping | NEW |
| — | **education (recalibrate)** | apply real university-tier calibration | FIX — currently over-rates / saturates |

`company_pedigree` (brand size) is **deprecated** — it maxed both famous-enterprise (Cisco, Mobileye)
and weak-local (Nana10) companies, which is the opposite of what recruiters want. Superseded by
`company_type_fit` + `industry_fit`.

---

## 2. Position brief — Engineering Director, Editor Platform

**Target:** engineering leaders from fast-moving **software startups/scaleups**.

- **Company type (dominant — 6/6 cases):** strong downweight of
  - enterprise / heavy / slow: Cisco, HP, AT&T, Salesforce
  - hardware / semi-hardware: Intel, Mobileye
  - cyber: Checkpoint
  - weak local portals: Nana10
  - big-corp-acquired: Cynamedia (→Cisco)
- **Tenure:** penalize very long tenures (e.g. 8 yrs at HP).
- **Seniority:** reject overqualified for the level.
- **Education:** apply real university-tier calibration (don't inflate 2nd-tier).

*Evidence: 6 of 6 tagged Eng-Director rejections were unanimous on company-type; long-tenure and
overqualified recurred.*

---

## 3. Position brief — BDR Canada (position 41.264)

**Target:** right-sized junior **SaaS** sales / outbound BDRs, currently active.

- **Core function (must-have):** actual **sales experience** — reject if absent (e.g. Sueva Falcone).
- **Industry:** SaaS sales background preferred; downweight non-SaaS backgrounds (e.g. Enrique Aguilar).
- **Seniority (dominant — 3 cases):** reject **overqualified** (Manuel, Swayam, Darshan).
- **Employment recency:** flag significant current gaps (Manuel — not working since Oct '25).
- **DO NOT use:** language / accent / country-of-education (do-not-learn §0).

---

## 3b. Position brief — Senior Product Designer (position 0A.568)

**Target:** designers from **design-led companies** in Riverside's domain.

- **Design-led company (dominant — 6/10):** does the company genuinely invest in product design, in the
  **creator / marketing / video / audio / media** domain. Downweight weak-design / non-design-centric
  companies (e.g. Yotpo, ZoomInfo). **Nuance:** assess design-led-ness at the *company* level, NOT by
  industry label — most cyber companies fail, but design-forward ones (**Wiz, Cyera**) pass. Do not
  blanket-reject "cyber."
- **Company scale:** downweight too-small / no-scale companies.
- **Seniority:** reject overqualified.
- **Employment recency:** flag current gaps; also flag when the *relevant/strong* experience is dated.

## 3c. New signal + training category (from the Designer pass)

- **NEW dimension — `relevant_experience_recency`**: is the candidate's *strong, role-relevant*
  experience recent, or dated? (distinct from employment gaps).
- **`company_type_fit` is role-parameterized**: "good company" means startup/scaleup software for Eng,
  SaaS-sales for BDR, **design-led/creator-media for Designer**. Same dimension, per-position target.
- **THIRD data category — EXCLUDE-FROM-TRAINING** (distinct from §0 fairness do-not-learn):
  rejections that are **not CV-explainable** — inside info, external context, or borderline (~85 vs
  judge 88) calls. Tag these and **exclude from training / down-weight**, rather than forcing the judge
  to learn a signal absent from the CV. Also: distinguish **clear reject** vs **borderline** in labels.

## 4. How these plug into the judge

- **Dimensions:** add 1–6 to the scoring tool schema + system prompt; drop/reframe `company_pedigree`.
- **Position briefs:** inject as position-level context — same mechanism as the existing
  `recruiter_notes` / `industries_up/down` on `PositionClass`.
- **Do-not-learn:** explicit prompt instruction **plus** a post-check that the model's rationale never
  cites origin/accent/country; if it does, discard/flag.

---

## 5. Validation plan (next; costs metered $ — batch, ≤$15/day)

1. Encode the updated judge (new dims + the two position briefs).
2. **Re-score the held-out disagreement cases** for Eng Director + BDR Canada.
3. **Success = the previously high-fit-REJECTED candidates now score LOWER** (blind spot closes), with
   agreement lift on held-out — without any drop attributable to do-not-learn signals.
4. If it lifts, expand the loop to more positions; if not, inspect which dimension underperformed.

---

## Attribution note (carried from REDESIGN §findings)
`cv_screen_assignee` is unreliable (44% of CV-screen steps have 2–3 assignees; the public API never
records who actually decided). Hence **per-position** framing here, not per-recruiter. Some Eng-Director
cases shown under Yoni were likely decided by Mor Eyal (co-assigned) — fine, since these are role-level
criteria the team shares.
