"""Claude-based candidate scoring.

Port of ScoringV2.gs. Runs a single pass with:
  - JD criteria extracted once per position (cached in CacheService → simple in-process LRU here)
  - Learned class rubric (from rubrics.py) injected as the highest-priority calibration signal
  - Per-candidate anchors (from anchors.py) injected even higher up in the prompt
  - Arithmetic calibration delta (avg recruiter - avg AI) — only fires when neither
    rubric nor anchors are present (prevents stacking)

Returns a `ScoreResult` dataclass; the caller wires it to the scan flow + tagging.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from anthropic import Anthropic
from anthropic.types import TextBlock

from .anchors import format_anchors_for_prompt, get_anchors_for_candidate
from .config import settings
from .debug_log import append_debug_log
from .feedback import list_feedback_for_class
from .rubrics import get_learned_rubric_for_class

log = logging.getLogger(__name__)


# ─── Result dataclass ────────────────────────────────────────────────────────
@dataclass
class ScoreResult:
    rating: int                                 # overall 1-10 (weighted sum of sub-scores)
    confidence: float
    summary: str
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    comeet_comment_html: str = ""
    linkedin_url: str | None = None
    # Per-dimension sub-scores (1-10 each). All optional — legacy callers
    # without dimension support can still construct a ScoreResult with
    # just `rating`. Current scoring pipeline populates the five active
    # sliders + location_match. dim_domain_match and dim_achievements
    # are deprecated — kept on the dataclass for back-compat with stored
    # rows.
    dim_company_domain: int | None = None
    dim_profession_domain: int | None = None
    dim_company_tier: int | None = None
    dim_career_progression: int | None = None
    dim_location_match: int | None = None
    dim_university_tier: int | None = None
    dim_domain_match: int | None = None      # DEPRECATED — split into above two
    dim_achievements: int | None = None      # DEPRECATED — no longer scored
    # v2 extras
    pre_calibration_rating: int = 0
    calibration_delta: float | None = None
    calibration_samples: int = 0
    learned_rubric_used: bool = False
    arithmetic_calibration_skipped: bool = False
    anchors_used: int = 0
    anchors_critical: int = 0


@dataclass
class ScoreInputs:
    """Everything the scorer needs about one candidate."""
    candidate: dict[str, Any]              # full candidate dict from public Comeet API
    position_uid: str
    position_name: str
    position_jd: str                       # prose JD text (from comeet_client.position_jd_text)
    class_id: str
    class_name: str
    process_context: str                   # extra text describing the candidate's history
    resume_pdf_b64: str | None = None      # base64 of the candidate's resume PDF, if available
    resume_url_existed_but_failed: bool = False


# ─── JD criteria extraction (cached) ─────────────────────────────────────────
@lru_cache(maxsize=256)
def _extract_jd_criteria_cached(api_key_marker: str, position_uid: str, jd_hash: str, position_jd: str) -> str:
    """Wrapper that lets us cache by (api key + position uid + JD content hash).

    The cache key includes a hash of the JD text so a JD edit busts the entry.
    """
    return _extract_jd_criteria(position_jd)


def _extract_jd_criteria(position_jd: str) -> str:
    """Pull structured criteria from the JD as JSON. Returns "" on failure."""
    if not settings.anthropic_api_key:
        return ""
    prompt = (
        "Extract the structured screening criteria from this job description. Return ONLY a "
        "JSON object with keys: must_haves (string[]), nice_to_haves (string[]), deal_breakers "
        "(string[]), seniority (one of: junior|mid|senior|staff|principal|director|vp), "
        "primary_skills (string[] of 3-7 items).\n\n"
        "Be concrete and concise. Each item should be a specific capability not a vague phrase.\n\n"
        "JOB DESCRIPTION:\n" + position_jd
    )
    try:
        client = Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.claude_model,
            max_tokens=800,
            temperature=0.0,
            system="Return only valid JSON, no markdown fences.",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        )
        text = "".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        json.loads(text)  # validate
        return text
    except Exception as exc:  # noqa: BLE001
        log.warning("extract_jd_criteria failed: %s", exc)
        return ""


def _criteria_block(criteria_json: str) -> str:
    """Build the soft-criteria prompt block from the extracted JSON."""
    if not criteria_json:
        return ""
    try:
        data = json.loads(criteria_json)
    except json.JSONDecodeError:
        return ""
    must = (data.get("must_haves") or [])[:8]
    nice = (data.get("nice_to_haves") or [])[:6]
    deal = (data.get("deal_breakers") or [])[:4]
    if not (must or nice or deal):
        return ""
    block = "\n--- KEY REQUIREMENTS (from the JD, for your reference) ---\n"
    if must:
        block += "Required: " + "; ".join(must) + "\n"
    if nice:
        block += "Preferred: " + "; ".join(nice) + "\n"
    if deal:
        block += "Hard blockers (only if explicitly stated in JD): " + "; ".join(deal) + "\n"
    if data.get("seniority"):
        block += f"Seniority indicator: {data['seniority']}\n"
    block += "(Use these as a soft reference; weigh them with judgment, do not mechanically check off.)\n"
    return block


# ─── Calibration delta ───────────────────────────────────────────────────────
def _calibration_delta_for_class(class_id: str) -> tuple[float | None, int]:
    """Returns (delta, sample_count). delta is None when sample is too small."""
    rows = list_feedback_for_class(class_id)
    valid = [r for r in rows if r.ai_rating and r.recruiter_rating]
    if len(valid) < settings.calibration_min_samples:
        return None, len(valid)
    avg_ai = sum(r.ai_rating for r in valid) / len(valid)
    avg_rec = sum(r.recruiter_rating for r in valid) / len(valid)
    delta = avg_rec - avg_ai
    cap = settings.calibration_max_delta
    delta = max(-cap, min(cap, delta))
    return delta, len(valid)


def _apply_calibration(raw: int, delta: float | None) -> int:
    if delta is None:
        return raw
    return max(1, min(5, round(raw + delta)))


# ─── Main scoring entry ──────────────────────────────────────────────────────
def score_candidate(inputs: ScoreInputs) -> ScoreResult:
    """Score one candidate against the position. Mirrors ScoringV2.gs's lite mode."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    candidate = inputs.candidate
    candidate_uid = str(candidate.get("uid") or "")

    # 1) JD criteria (cached per JD content)
    jd_hash = str(hash(inputs.position_jd))
    criteria_json = _extract_jd_criteria_cached(
        settings.anthropic_api_key[:8], inputs.position_uid, jd_hash, inputs.position_jd,
    ) if inputs.position_uid else ""
    criteria_block = _criteria_block(criteria_json)

    # 2) Learned rubric for the class (cached, count-keyed — auto-busts on new feedback)
    learned_rubric = (
        get_learned_rubric_for_class(inputs.class_id, inputs.class_name)
        if inputs.class_id and inputs.class_name else ""
    )

    # 3) Per-candidate anchors
    anchors = get_anchors_for_candidate(
        class_id=inputs.class_id,
        position_uid=inputs.position_uid,
        candidate_uid=candidate_uid,
    ) if inputs.class_id else []
    anchors_block = format_anchors_for_prompt(anchors)
    anchors_critical = sum(1 for a in anchors if a.is_critical)

    # 4) Calibration delta (only used when neither rubric nor anchors are present)
    delta, sample_count = (
        _calibration_delta_for_class(inputs.class_id) if inputs.class_id else (None, 0)
    )

    # 5) Build & call the LLM
    pass_result = _single_pass(
        inputs=inputs,
        criteria_block=criteria_block,
        learned_rubric=learned_rubric,
        anchors_block=anchors_block,
    )

    raw_rating = pass_result.rating
    final_rating = raw_rating
    arithmetic_applied = False
    if (
        not learned_rubric and not anchors
        and delta is not None
        and abs(delta) >= settings.calibration_min_abs_delta
    ):
        final_rating = _apply_calibration(raw_rating, delta)
        arithmetic_applied = True

    # 6) Debug log (no-op when disabled)
    # candidate.URL is Comeet's canonical web URL in the form
    # https://app.comeet.co/app/req/<numericPos>/can/<numericCand> — the
    # only format that actually navigates inside the app. We store it so
    # the calibration UI can link straight to the recruiter's view.
    append_debug_log(
        candidate_uid=candidate_uid,
        candidate_name=_full_name(candidate),
        position_uid=inputs.position_uid,
        position_name=inputs.position_name,
        class_id=inputs.class_id,
        anchors_count=len(anchors),
        anchors_critical=anchors_critical,
        anchors_block=anchors_block,
        rubric_used=bool(learned_rubric),
        rubric_snippet=learned_rubric,
        raw_rating=raw_rating,
        final_rating=final_rating,
        calibration_delta=delta if arithmetic_applied else None,
        arithmetic_applied=arithmetic_applied,
        confidence=pass_result.confidence,
        summary=pass_result.summary,
        strengths=pass_result.strengths,
        gaps=pass_result.gaps,
        profile_url=(candidate.get("URL") or None),
        dim_domain_match=pass_result.dim_domain_match,          # legacy avg
        dim_company_domain=pass_result.dim_company_domain,
        dim_profession_domain=pass_result.dim_profession_domain,
        dim_company_tier=pass_result.dim_company_tier,
        dim_career_progression=pass_result.dim_career_progression,
        dim_location_match=pass_result.dim_location_match,
        dim_university_tier=pass_result.dim_university_tier,
        dim_achievements=pass_result.dim_achievements,           # deprecated, null
    )

    return ScoreResult(
        rating=final_rating,
        confidence=pass_result.confidence,
        summary=pass_result.summary,
        strengths=pass_result.strengths,
        gaps=pass_result.gaps,
        comeet_comment_html=pass_result.comeet_comment_html,
        linkedin_url=pass_result.linkedin_url,
        pre_calibration_rating=raw_rating,
        calibration_delta=delta if arithmetic_applied else None,
        calibration_samples=sample_count,
        learned_rubric_used=bool(learned_rubric),
        arithmetic_calibration_skipped=bool(learned_rubric or anchors),
        anchors_used=len(anchors),
        anchors_critical=anchors_critical,
    )


# ─── Single-pass call ────────────────────────────────────────────────────────
def _single_pass(
    *,
    inputs: ScoreInputs,
    criteria_block: str,
    learned_rubric: str,
    anchors_block: str,
) -> ScoreResult:
    candidate = inputs.candidate
    name = _full_name(candidate)

    rubric_block = ""
    if learned_rubric.strip():
        rubric_block = (
            "\n══════════════════════════════════════════════════════════\n"
            "LEARNED RUBRIC (from this recruiter's past ratings for this class)\n"
            "══════════════════════════════════════════════════════════\n"
            + learned_rubric.strip()
            + "\n══════════════════════════════════════════════════════════\n"
            "CALIBRATION MANDATE: The rubric above is derived from this recruiter's actual past "
            "overrides of AI ratings. It is more authoritative than your own initial intuition. "
            "When the current candidate matches a STRONG SIGNAL pattern, rate 4–5. When they match "
            "a WEAK SIGNAL pattern, rate 1–2. When unclear, rate 3 with appropriate confidence. "
            "Pay special attention to the AI BIAS CORRECTIONS section — those are the calibration "
            "errors you have been making historically.\n\n"
        )

    # Pre-rating checklist — forces the model to actually evaluate the
    # structured signals it tends to skip (location, company tier,
    # university tier, product-vs-agency career arc, career progression).
    # Without this it over-rates candidates from mismatched locations,
    # service/staffing-agency career arcs, unknown employers, and
    # candidates whose career has been flat for years.
    #
    # The company-tier reference includes both POSITIVE (tier-1 product)
    # and NEGATIVE (service / outsourcing / HR staffing) lists, ~280 names,
    # so Claude has concrete benchmarks both directions.
    from .company_tiers import format_company_tiers_block as _tiers_block
    from .university_tiers import format_university_tiers_block as _uni_block

    pre_rating_checklist = (
        "\n=== PRE-RATING CHECKLIST (work through these BEFORE picking a rating) ===\n"
        "For each of these axes, write a one-line internal assessment, then let "
        "the combined picture inform your rating. Do not just consider technical depth.\n\n"

        "1) LOCATION MATCH — Compare the role's expected location to the candidate's "
        "current location (city / country from CV, LinkedIn, or employer locations). "
        "If they're in a different country and the CV does NOT explicitly say they're "
        "willing to relocate, this is a strong negative signal. Do not assume relocation.\n\n"

        "2) COMPANY TIER (for each of their last 3-5 roles) — Categorise each employer "
        "using the COMPANY TIER REFERENCE below:\n"
        "   - TIER-1 (global FAANG/unicorns OR top Israeli scale-up): STRONG POSITIVE\n"
        "   - TIER-2 PRODUCT (smaller-but-known shipping their own product): neutral-to-positive\n"
        "   - SERVICE / OUTSOURCING / CONSULTING (Tata, Wipro, EPAM, Synamedia, Matrix IT, "
        "etc.): STRONG NEGATIVE even if titles look senior — work is per-client, not own product\n"
        "   - HR / STAFFING / RECRUITMENT AGENCIES (Manpower, Adecco, Atid, Milam HR, "
        "Allstars, etc.): STRONG NEGATIVE for senior recruiter / TA / HR roles — recruiting "
        "AT an agency is much weaker signal than recruiting in-house at a tier-1 product co\n"
        "   - UNKNOWN LOCAL: weaker signal unless concrete scale evidence (real product/DAUs/revenue)\n\n"

        "3) UNIVERSITY TIER — Categorise the highest-degree institution using the "
        "UNIVERSITY TIER REFERENCE below. Geographic focus is Israel / Canada / Poland "
        "(where most of our positions are) plus a global tier-1 anchor set:\n"
        "   - TIER-1 (global elite OR top IL/CA/PL research university): STRONG POSITIVE, "
        "grade 8-10.\n"
        "   - TIER-2 (respected national / regional research uni or strong academic college): "
        "POSITIVE, grade 6-7.\n"
        "   - LOW-TIER (smaller teaching colleges / polytechnics / regional schools): WEAK "
        "signal, grade 3-5. Not disqualifying on its own but stacks negatively.\n"
        "   - For Israeli candidates, ELITE MILITARY / NATIONAL PROGRAMS (Talpiot, 8200, "
        "Mamram, etc.) often matter MORE than the university itself — bump toward 9-10 "
        "when the CV explicitly mentions one.\n\n"

        "4) PRODUCT vs AGENCY CAREER ARC — Is the *recent* career trajectory at product "
        "companies or service / staffing shops? Weight the last 3-5 years more than older "
        "roles. A career that started in product and drifted into agency = warning. A career "
        "entirely at service shops or staffing/HR agencies = strong negative regardless of title.\n\n"

        "5) CAREER PROGRESSION — Look at title + scope across the timeline:\n"
        "   HEALTHY: Junior → Mid → Senior → Lead → Manager / Staff over 6-10 years, with "
        "scope or team-size growing alongside the titles.\n"
        "   RED FLAGS (strong negative):\n"
        "   - Same title 5+ years with no scope growth (flat trajectory).\n"
        "   - Title regression (e.g. Senior → Mid at a new company without a clear reason).\n"
        "   - Lateral company-hops every 12-18 months with no level escalation.\n"
        "   - Very slow progression (8+ years to reach Senior at non-elite shops).\n"
        "   A flat or slow arc at service / staffing companies stacks negatively with axis 4.\n\n"

        "6) BAND IMPACT — Combine the above with the role-specific evidence. Strong "
        "tier-1 product + tier-1 university + location match + product career arc + "
        "healthy progression = candidate for 7-10. Service/staffing/agency career + "
        "unknown employers + mismatched location + flat progression = should be 1-3 "
        "unless the candidate has truly exceptional individual achievements that "
        "outweigh the tier signal. The 1-10 internal scale gives you room to "
        "differentiate — use 5-6 when the signals are mixed, not as a polite default.\n"
        + _tiers_block()
        + _uni_block()
        + "\n\nThen proceed to the rating.\n\n"
    )

    base_prompt = (
        "You are an expert recruiter. Compare this applicant to the open position. "
        "Be concise, evidence-based, and selective. Your job is to filter for the team — "
        "default toward 2-3 unless there is concrete positive evidence to justify 4-5. "
        "Most candidates in a typical pool are NOT a strong fit; your ratings should "
        "reflect that. Sparse evidence is itself a negative signal, not a reason to "
        "park the rating at 3 — if you cannot find concrete positive evidence the "
        "candidate clears the bar, rate 2 with low confidence rather than 3.\n\n"
        + (anchors_block or "")
        + rubric_block
        + (criteria_block or "")
        + pre_rating_checklist
        + "\nPOSITION CONTEXT:\n"
        + inputs.position_jd
        + "\n\nAPPLICANT METADATA:\n"
        + f"Name: {name}\n"
        + f"Email: {candidate.get('email') or ''}\n"
        + inputs.process_context
        + "\n\n"
    )

    tail = (
        "Respond with ONLY a single JSON object (no markdown fences, no prose before or after). Keys:\n"
        "- profession_domain: integer 1-10 — How closely the candidate's actual ROLE / PROFESSION "
        "matches the role we're hiring for. Senior PM applying for Senior PM = 9-10. PM-adjacent "
        "(Product Marketing, Product Ops, BizOps with product surface) applying for PM = 5-7. "
        "Different profession entirely (QA → PM, Designer → PM, Dev → PM) = 1-3 unless the CV "
        "shows clear PM-style scope already. Read role descriptions and accomplishments — don't "
        "just title-match.\n"
        "- company_domain: integer 1-10 — Have the candidate's recent EMPLOYERS done something "
        "similar to us (creator-tools, podcasting, video, B2C SaaS, content creation, media tech, "
        "EdTech with consumer surface)? Use the DOMAIN ADJACENCY RULE below. PM at Descript / Loom / "
        "Canva / Spotify = 9-10. PM at general B2B SaaS / dev-tools = 5-7. PM at a bank / pharma / "
        "hardware / enterprise = 1-3 regardless of how well-known the company is.\n"
        "- company_tier: integer 1-10 — quality of recent employers (tier-1 product vs agency/unknown)\n"
        "- career_progression: integer 1-10 — title+scope growth over time (10=healthy, 1=flat/regression)\n"
        "- location_match: integer 1-10 — HARD GATE: scoring below 4 auto-rejects the candidate. "
        "Score 1-3 ONLY when the candidate is clearly in a different country AND the CV does NOT mention "
        "willingness to relocate. Score 8-10 when location clearly matches the role's country. "
        "Score 5-7 when uncertain (no explicit location info but no obvious mismatch either).\n"
        "- university_tier: integer 1-10 — use the UNIVERSITY TIER REFERENCE above. "
        "Tier-1 global / IL / CA / PL = 8-10; tier-2 respected = 6-7; low-tier / unknown = 3-5. "
        "Israeli elite programs (Talpiot, 8200, Mamram) bump toward 9-10. "
        "**NO-DEGREE RULE**: if the CV shows NO formal degree (no Bachelor's / Master's / "
        "PhD / equivalent — only bootcamps, certifications, courses, or 'self-taught'), "
        "university_tier MUST be ≤ 3. Bootcamp only = 3. No formal education visible = 1-2. "
        "Don't be generous here — many otherwise-strong candidates have no degree, and "
        "that's the signal the recruiter wants to see.\n"
        "- confidence: number 0 to 1\n"
        "- summary: string, 1-2 sentences max\n"
        "- strengths: array of up to 4 short strings\n"
        "- gaps: array of EXACTLY 2-3 short strings. ALWAYS provide AT LEAST 2 cons — even a "
        "5/5 candidate has something to flag for the recruiter (e.g., 'no public-product "
        "experience visible', 'unclear domain match on JD specifics'). Never return fewer than 2.\n"
        "- comeet_comment_html: short extra HTML/plain for the note (server allows only b, i, u)\n"
        "- linkedin_url: full linkedin.com/in/ URL if visible in the resume; otherwise null\n\n"

        "The OVERALL rating is computed server-side with three layers:\n"
        "  - If location_match < 4 (location gate): overall = 1, regardless of everything else.\n"
        "  - If avg(profession_domain, company_domain) < 5 (domain cap): overall capped at 5 even "
        "if other sliders are 10s. Rationale: wrong domain on BOTH facets together (wrong job + "
        "wrong industry) is disqualifying no matter how impressive the rest of the CV is. A strong "
        "single facet (e.g. great profession match at a wrong-industry company, or vice versa) "
        "can pull the average above 5 and avoid the cap.\n"
        "  - Otherwise: weighted sum of the FIVE slider dimensions (profession_domain, "
        "company_domain, company_tier, career_progression, university_tier) using recruiter-set "
        "per-position weights summing to 100. Default weights are profession 23 / company-domain 13 / "
        "company-tier 27 / progression 20 / university 17 but the recruiter can tune them per position.\n"
        "Score each axis HONESTLY and INDEPENDENTLY. Don't try to pre-balance toward a target — "
        "that's our math to do. Note: 'achievements' is no longer scored as a separate axis; concrete "
        "achievements are still valuable evidence but they feed your judgement on the five sliders "
        "(especially profession_domain and career_progression) rather than getting their own number.\n\n"

        "Rating scale (1-10) — calibrated for a REAL CV pool, which is power-law NOT "
        "bell-curved. Most applicants are weak fits and the top is rare. Typical pool:\n"
        "  10: ~1%   |  9: ~2%   |  8: ~2%   |  7: ~5%   |  6: ~10%\n"
        "   5: ~13%  |  4: ~17%  |  3: ~15%  |  2: ~15%  |  1: ~20%\n"
        "Cumulative from the top:\n"
        "  10        → top 1% of applicants\n"
        "  9-10      → top 3%\n"
        "  8-10      → top 5%\n"
        "  7-10      → top 10%\n"
        "  6-10      → top 20%\n"
        "  5-10      → top 33%\n"
        "  4-10      → top 50%   (so the median candidate lands at a 4)\n"
        "  1-3       → bottom 50% (location mismatches, wrong domain, agency-only, etc.)\n\n"

        "=== DOMAIN ADJACENCY RULE (READ THIS — affects ~30% of candidates) ===\n"
        "We hire for a CREATOR-TOOLS / B2C SaaS company (Riverside.fm — podcasting, "
        "video, content creation, SaaS for creators). For ANY role at this company, "
        "candidates from these industries are domain-RELEVANT (good fit):\n"
        "    Creator-tools • Podcasting / audio / video • B2C SaaS for prosumers • "
        "Social platforms • Content creation tools • Media tech • EdTech with B2C "
        "consumer-facing surface • Streaming • Music tech\n"
        "Candidates from these industries are domain-IRRELEVANT (bad fit, even if "
        "their company is tier-1):\n"
        "    Banking • Insurance (general L&P/P&C, NOT insurtech-SaaS) • Pharma / "
        "biotech • Hardware / semiconductor / chip design • Generic enterprise IT • "
        "Telecom / ISP infrastructure • Defense / aerospace • Industrial automation • "
        "Heavy manufacturing • Government / public sector • Logistics infra\n"
        "**HARD RULE**: A candidate whose entire recent career (3-5 years) is in the "
        "IRRELEVANT bucket CANNOT score 7+ overall regardless of company tier or "
        "education. Their domain_match should be 1-4. Their overall lands in 1-4. "
        "A senior PM at Goldman Sachs is a 2-3 for Riverside, not a 7.\n"
        "Adjacent-but-not-direct industries (general B2B SaaS, fintech-with-consumer-"
        "surface, dev-tools, marketing tech) → domain_match 5-7 depending on how "
        "close the work actually is.\n\n"

        "Bands (anchor points — use the 10-point scale, this is rough mapping):\n"
        "- 10 (Superstar — top ~1%): EXCEPTIONAL. Tier-1 employer + tier-1 university + "
        "creator-tools or B2C-SaaS adjacency + clear scale/scope leader achievements + "
        "healthy progression. Hire on paper. If you're thinking 'maybe 10', they're a 9.\n"
        "- 8-9 (Strong — top ~5%): Candidate we would FAST-TRACK TO INTERVIEW TODAY. "
        "Tier-1 product employer in a RELEVANT domain (see Domain Adjacency Rule above), "
        "clear progression, no major flags. 9 = nearly superstar with one small caveat. "
        "8 = solidly strong.\n"
        "- 7 (Above bar — top ~10%): Solid candidate worth a screen call. **REQUIRES** "
        "domain adjacency (creator-tools / B2C SaaS / media tech / EdTech-consumer / "
        "music or video adjacent). Without that adjacency the ceiling is 6, period.\n"
        "- 5-6 (Maybe — top ~33%): Reasonable signals but notable gaps OR all-mid "
        "signals (known-but-not-top-tier employer, normal progression, partial domain "
        "match). 6 = lean yes. 5 = lean no.\n"
        "- 4 (The DEFAULT for typical applicants — top ~50%): The median candidate. "
        "Resume looks fine but nothing concrete differentiates them. Includes most "
        "applicants who have relevant work but no tier-1 / scale / leader signals.\n"
        "- 1-3 (Misfit — bottom 50%, ~50% of pool): Hard or substantial blockers. Use "
        "this band when ANY of these are true:\n"
        "    • Candidate is in a different country and the CV does NOT mention relocating.\n"
        "    • Entire (or near-entire) career at service / staffing / consulting / "
        "outsourcing shops.\n"
        "    • Completely wrong skill set or domain (banking / hardware / pharma / "
        "generic enterprise / defense, etc. per the Domain Adjacency Rule).\n"
        "    • No formal degree on CV (use the No-Degree Rule above as one of the inputs).\n"
        "    • Obvious level mismatch (senior role + sub-junior candidate, or vice versa "
        "with no path forward).\n"
        "  These cases are NOT 4+ with low confidence — they're 1-3.\n\n"

        "IMPORTANT: Sparse evidence is itself a negative signal. 'I can't tell from this CV' "
        "= 3-4, not 5-6. Only land at 5-6 when there ARE real signals but they're middling.\n\n"

        "TIEBREAKER RULE: When hesitating between two adjacent values, pick the LOWER one. "
        "'I think this is a 7, maybe a 6' → 6. 'Could be a 4 or 3' → 3. The team can "
        "always thumbs-up a borderline candidate and teach you to be less strict; they "
        "cannot easily un-tag a candidate you over-rated.\n\n"

        "=== REFLECTION STEP (do this BEFORE finalising the rating) ===\n"
        "  (a) If you're about to rate 7 or higher, mentally list THREE concrete "
        "positive signals — specific tier-1 employer names, specific scale/scope numbers, "
        "specific tier-1 university, or specific recent achievements that map directly "
        "to THIS role (not generic 'has experience'). If you cannot list three SPECIFIC "
        "items, drop by one band (i.e. to 5-6). If you cannot list any, drop to 3-4.\n"
        "  (b) Count the CONS (red flags from the checklist: location mismatch, "
        "service/agency career, flat progression, unknown employers, irrelevant domain). "
        "If you have 2 or more cons, do NOT just inflate the rating based on the "
        "positives — explicitly weigh the negatives in your final number. The rating "
        "should reflect a balanced view of both sides. Strong positives can still "
        "justify a 7-8 if the cons are minor or non-blocking; but if the cons are "
        "substantive (location mismatch, agency-only career, etc.) the balanced answer "
        "is usually one band lower than the positives alone would suggest.\n"
        "  (c) 7-vs-6 wedge: if your reasoning for picking 7+ would also fit a 6 "
        "candidate ('strong tech depth', 'good company experience', 'relevant skills'), "
        "the answer is 6. 7+ requires something SPECIFICALLY differentiating — a tier-1 "
        "employer name, a clear scale/scope leader achievement, a domain match the "
        "lower-rated peers don't have. If you can't name that differentiator in one "
        "sentence, drop to 6.\n"
        "  (d) Final sanity-check: if you would describe this candidate as 'good but "
        "not exceptional', that's a 5-6, not a 7+. 7+ means 'fast-track this person today'.\n"
    )

    if inputs.resume_pdf_b64:
        intro = "The candidate CV/resume is attached as a PDF (previous content block).\n\n"
    elif inputs.resume_url_existed_but_failed:
        intro = (
            "The candidate has a CV on file but the download link expired. Score from metadata + "
            "LinkedIn only; lower confidence accordingly.\n\n"
        )
    else:
        # No-CV path: explicitly default to NEUTRAL with low confidence rather
        # than letting the AI drift toward 3 (which it does because "sparse
        # evidence = 3-4" elsewhere in the prompt). The Itay Simon / Waxman
        # benchmark cases were AI=3, you=5 — fixing that asymmetry here.
        intro = (
            "NO RESUME AND NO LINKEDIN AVAILABLE for this candidate. You have only "
            "the candidate name + role + position metadata to work from.\n\n"
            "**NO-CV DEFAULT RULE**: When you genuinely have no CV and no LinkedIn "
            "(this case), DO NOT pull the rating down for 'sparse evidence'. Default "
            "to overall ~5 (neutral) with confidence ≤ 0.2. Score each sub-dimension "
            "at 5 unless the candidate name alone gives you a clear signal "
            "(e.g. it's an obviously non-Latin name in a Latin-only-required role, "
            "or the metadata says they applied from a country with a hard mismatch — "
            "in which case the location gate still fires normally). The 'sparse "
            "evidence = 3-4' rule applies to candidates whose CV exists but is light; "
            "it does NOT apply when the CV is entirely absent. The recruiter will "
            "follow up to get the CV; your job is not to penalise them in the "
            "meantime.\n\n"
        )

    text_body = base_prompt + intro + tail

    user_content: list[dict[str, Any]] = []
    if inputs.resume_pdf_b64:
        user_content.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": inputs.resume_pdf_b64,
            },
        })
    user_content.append({"type": "text", "text": text_body})

    client = Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.claude_model,
        max_tokens=1500,
        temperature=0.2,
        system=(
            "You return only valid JSON for recruiting screening. No markdown code fences. "
            "No extra keys beyond those requested."
        ),
        messages=[{"role": "user", "content": user_content}],
    )
    raw_text = "".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
    raw_text = raw_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(raw_text)

    # Pull the six sub-scores Claude returned (5 sliders + location gate).
    # `achievements` and `domain_match` are deprecated and no longer
    # requested; if a model response still includes either we ignore them.
    # Each clamps to 1-10 internal scale.
    from .rating_scale import clamp_internal
    from .dimensions import ALL_SCORED_AXES, compute_overall, get_weights

    sub_scores: dict[str, int | None] = {
        k: clamp_internal(parsed.get(k)) for k in ALL_SCORED_AXES
    }

    # Back-compat: if Claude returns the legacy `domain_match` key but
    # not the new split keys (can happen during prompt-cache warmup or
    # if the model trims output), fan it out to both new axes so the
    # weighted sum still computes correctly.
    legacy_dm = clamp_internal(parsed.get("domain_match"))
    if legacy_dm is not None:
        if sub_scores.get("profession_domain") is None:
            sub_scores["profession_domain"] = legacy_dm
        if sub_scores.get("company_domain") is None:
            sub_scores["company_domain"] = legacy_dm

    # Compute the weighted overall:
    #  - location_match < threshold → auto 1 (hard gate)
    #  - else: 5 sliders at per-position weights summing to 100
    #  - then: avg(profession_domain, company_domain) < 5 → cap at 5
    weights = get_weights(inputs.position_uid)  # 5 slider weights summing to 100
    rating = compute_overall(sub_scores, weights)
    if rating is None:
        # Total parse failure (Claude returned no sub-scores). Fall back
        # to legacy single-"rating" field if present, else neutral 5.
        rating = clamp_internal(parsed.get("rating")) or 5
    # For dim_domain_match (legacy column we keep populated for back-compat
    # with anything that still reads it — e.g. older calibration views),
    # store the average of the two new domain facets, rounded to int.
    prof = sub_scores.get("profession_domain")
    comp = sub_scores.get("company_domain")
    legacy_dim_dm: int | None
    if prof is not None and comp is not None:
        legacy_dim_dm = round((prof + comp) / 2)
    elif prof is not None:
        legacy_dim_dm = prof
    elif comp is not None:
        legacy_dim_dm = comp
    else:
        legacy_dim_dm = None

    return ScoreResult(
        rating=rating,
        confidence=float(parsed.get("confidence") or 0.0),
        summary=str(parsed.get("summary") or ""),
        strengths=list(parsed.get("strengths") or [])[:4],
        gaps=list(parsed.get("gaps") or [])[:2],
        comeet_comment_html=str(parsed.get("comeet_comment_html") or ""),
        linkedin_url=(parsed.get("linkedin_url") or None) or None,
        dim_company_domain=sub_scores.get("company_domain"),
        dim_profession_domain=sub_scores.get("profession_domain"),
        dim_company_tier=sub_scores.get("company_tier"),
        dim_career_progression=sub_scores.get("career_progression"),
        dim_location_match=sub_scores.get("location_match"),
        dim_university_tier=sub_scores.get("university_tier"),
        dim_domain_match=legacy_dim_dm,   # back-compat (averaged)
        dim_achievements=None,             # deprecated
    )


def _full_name(candidate: dict[str, Any]) -> str:
    parts = [(candidate.get("first_name") or "").strip(), (candidate.get("last_name") or "").strip()]
    return " ".join(p for p in parts if p)


def encode_pdf_bytes(pdf_bytes: bytes) -> str:
    return base64.b64encode(pdf_bytes).decode("ascii")


__all__ = ["ScoreInputs", "ScoreResult", "score_candidate", "encode_pdf_bytes"]
