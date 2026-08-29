"""CV-screen judge (v4): score ONE candidate against a position's learned brief.

Structured output — per-dimension 1-5 with evidence, overall fit 0-100, a
recommendation (advance/borderline/reject), and a confidence — plus the
do-not-learn fairness rule. This is the config validated in the redesign
(AUC 0.814 BDR / 0.899 Eng Director; docs/TEACHING-LOOP-V1.md, REDESIGN.md).

Reuses the app's Comeet client + résumé fetch. The brief comes from
`position_briefs` (written by the Comeet Helper teaching flow via the bridge).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from anthropic import Anthropic

from .comeet_client import ComeetClient, position_jd_text
from .enrichment import _maybe_fetch_resume
from .config import settings

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"

SYSTEM = (
    "You are an expert recruiter performing an initial CV screen. Score how strongly the candidate "
    "merits ADVANCING past the CV screen for the SPECIFIC role, using the POSITION CRITERIA provided.\n"
    "DO-NOT-LEARN (hard rule): NEVER use or infer accent, national origin, country of education, "
    "years-in-country, or name/photo-based nationality/gender/age. Judge only job-relevant evidence in "
    "the CV. Your rationale must never cite any of these.\n"
    "Score each dimension 1-5. Give overall_fit_0_100 as a CALIBRATED likelihood of advancing, USING "
    "THE FULL 0-100 RANGE. Give a calibrated confidence_0_1 (how sure you are). You MUST call "
    "submit_assessment."
)

TOOL = {
    "name": "submit_assessment", "description": "Submit the structured CV-screen assessment.",
    "input_schema": {"type": "object", "properties": {
        "seniority_fit": {"type": "integer", "description": "1-5; 5=level well-matched; low if over/under-qualified"},
        "company_type_fit": {"type": "integer", "description": "1-5; match to the POSITION CRITERIA's target company profile"},
        "industry_fit": {"type": "integer", "description": "1-5; role-specific domain match"},
        "core_function_present": {"type": "integer", "description": "1-5; 5=CV clearly shows the role's core function performed"},
        "employment_recency": {"type": "integer", "description": "1-5; 5=currently employed/no concerning gap"},
        "relevant_experience_recency": {"type": "integer", "description": "1-5; 5=strong role-relevant experience is recent"},
        "tenure_pattern": {"type": "integer", "description": "1-5; 5=healthy tenures; low=over-long or hopping"},
        "education": {"type": "integer", "description": "1-5; calibrated tier"},
        "overall_fit_0_100": {"type": "integer"},
        "advance_recommendation": {"type": "string", "enum": ["advance", "borderline", "reject"]},
        "confidence_0_1": {"type": "number"},
        "rationale": {"type": "string", "description": "<=240 chars, evidence-based, no protected attributes"}},
        "required": ["seniority_fit", "company_type_fit", "core_function_present",
                     "overall_fit_0_100", "advance_recommendation", "confidence_0_1"]},
}

_DIMS = ("seniority_fit", "company_type_fit", "industry_fit", "core_function_present",
         "employment_recency", "relevant_experience_recency", "tenure_pattern", "education")


def score_candidate_ensemble(candidate: Dict[str, Any], position: Dict[str, Any], brief: str,
                             *, runs: int = 3, **kw: Any) -> Optional[Dict[str, Any]]:
    """Score a candidate `runs` times and average — the 2026-08-29 benchmark
    showed 3-run mean lifts AUC +0.03-0.04 and κ 0.41→0.53 (clears the gate)
    while per-run std is only ~2.2. fit = mean, recommendation = majority
    (ties → borderline), confidence = mean, dims = per-key mean, tokens summed."""
    results = []
    for _ in range(max(1, runs)):
        r = score_candidate(candidate, position, brief, **kw)
        if r:
            results.append(r)
    if not results:
        return None
    if len(results) == 1:
        return results[0]
    n = len(results)
    recs = [r["recommendation"] for r in results]
    rec = max(set(recs), key=recs.count)
    if recs.count(rec) * 2 <= n:  # no strict majority
        rec = "borderline"
    dims = {}
    for k in _DIMS:
        vals = [r["dims"].get(k) for r in results if r["dims"].get(k) is not None]
        dims[k] = round(sum(vals) / len(vals), 1) if vals else None
    confs = [r["confidence"] for r in results if r["confidence"] is not None]
    return {
        "fit": round(sum(r["fit"] for r in results) / n),
        "recommendation": rec,
        "confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "dims": dims,
        "rationale": results[0]["rationale"],
        "model": results[0]["model"],
        "in_tokens": sum(r["in_tokens"] for r in results),
        "out_tokens": sum(r["out_tokens"] for r in results),
        "runs": n,
        "fit_run_std": round((sum((r["fit"] - sum(x["fit"] for x in results) / n) ** 2
                                  for r in results) / n) ** 0.5, 1),
    }


def score_candidate(candidate: Dict[str, Any], position: Dict[str, Any], brief: str,
                    *, client: Optional[Anthropic] = None,
                    system: Optional[str] = None, model: Optional[str] = None,
                    effort: str = "low", use_thinking: bool = True) -> Optional[Dict[str, Any]]:
    """Score one Comeet candidate dict against `brief`. Returns a result dict or
    None (no fetchable CV / model returned nothing).

    system/model/effort/use_thinking exist for benchmark variants (app.bench_screen);
    production callers use the defaults. Any system override MUST keep the
    DO-NOT-LEARN fairness rule — enforced here, not trusted."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    if system is not None:
        # Enforce the actual fairness rule, not just its heading — an override
        # must carry the operative prohibitions verbatim-enough to bind.
        required = ("DO-NOT-LEARN", "national origin", "country of education",
                    "years-in-country", "nationality/gender/age")
        missing = [k for k in required if k not in system]
        if missing:
            raise ValueError(f"system override is missing fairness-rule elements: {missing}")
    resume = candidate.get("resume") or {}
    url = resume.get("url") if isinstance(resume, dict) else None
    pdf_b64, docx_text, _ = _maybe_fetch_resume(url)
    if not pdf_b64 and not docx_text:
        return None
    jd = position_jd_text(position or {}) or ""
    if pdf_b64:
        content = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}}]
    else:
        content = [{"type": "text", "text": "CANDIDATE CV:\n\n" + docx_text}]
    content.append({"type": "text", "text":
        f"{brief}\n\nROLE JD (reference):\n{jd[:1500]}\n\nAssess this candidate for advancing past the CV screen for THIS role."})

    client = client or Anthropic(api_key=settings.anthropic_api_key)
    use_model = model or MODEL
    kwargs: Dict[str, Any] = dict(model=use_model, max_tokens=1500, system=system or SYSTEM,
                                  tools=[TOOL], messages=[{"role": "user", "content": content}])
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort}
    msg = client.messages.create(**kwargs)
    a = next((b.input for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if not a or a.get("overall_fit_0_100") is None:
        return None
    return {
        "fit": a["overall_fit_0_100"],
        "recommendation": a.get("advance_recommendation"),
        "confidence": a.get("confidence_0_1"),
        "dims": {k: a.get(k) for k in _DIMS},
        "rationale": (a.get("rationale") or "")[:400],
        "model": use_model,
        "in_tokens": msg.usage.input_tokens,
        "out_tokens": msg.usage.output_tokens,
    }
