"""Geo-specific scoring overlays.

Layer 2 of the three-layer scoring system. Encodes geo-wide patterns
that are true regardless of the role-shape — fraud signatures, naming
conventions, career-cadence norms specific to a candidate pool from a
given country/region.

Stacks like this in the system prompt:
  [class_overlay if no position rubric yet]   (cold-start, role-shape)
  [geo_overlay for this position's location]  ← this module
  [position_rubric or class_rubric]            (learned from feedback)
  [position_brief]                              (recruiter free-text)

The geo overlay is curated manually from observed patterns during
calibration sessions. When recruiters tag persistent fraud patterns
on geo-X candidates that the AI keeps missing, the pattern goes into
that geo's overlay so subsequent positions (across all classes) in
the same geo inherit the correction.

Location strings are normalised against the same lookup used in the
positions endpoint (see app/locations.py): "PL" / "Poland" both map
to the Poland overlay; "IL" / "Israel" / "Tel-Aviv" → Israel; etc.
"""
from __future__ import annotations


# Normalised location keys → overlay text. The key is matched against
# the position's location field AFTER passing through
# `normalise_location()` below. Empty / unknown location → no overlay.
_GEO_OVERLAY: dict[str, str] = {
    "Poland": (
        "=== GEO OVERLAY (Poland) ===\n"
        "This position recruits candidates in or from Poland. The Polish CV "
        "market has a recurring FAKE-PROFILE PATTERN that the AI consistently "
        "over-rates. Apply these checks BEFORE scoring company_tier, "
        "profession_domain, or university_tier:\n\n"
        "FAKE-PROFILE SIGNATURE (any 2+ of these → treat as likely fabricated; "
        "cap company_tier ≤ 3, university_tier ≤ 3, and final rating ≤ 4 "
        "regardless of stated credentials):\n"
        "  • Polish name + education claimed at a non-EU university with no "
        "verifiable LinkedIn (especially: Singapore, Malaysia, Dubai, India, "
        "Philippines) — this is the strongest fraud signature observed.\n"
        "  • No LinkedIn URL on the CV, OR a LinkedIn URL that 404s. Polish "
        "tech candidates almost always have LinkedIn. Absence is itself a "
        "negative signal.\n"
        "  • Future-dated experience (e.g. CV submitted in 2026 showing "
        "'Senior Engineer at X, Mar 2026 – present' when 'present' should "
        "be a past date) — clear fabrication.\n"
        "  • Career progression that's impossibly fast (Junior → Senior → "
        "Lead in <3 years across multiple unverifiable companies).\n"
        "  • Employer names that are generic or unverifiable, especially "
        "if combined with 'foreign studies' above.\n\n"
        "Do NOT auto-rate well-presented CVs from Poland without confirming "
        "LinkedIn-presence and verifiable employer history. The AI's "
        "default is to take career claims at face value, which is wrong for "
        "this geo.\n\n"
        "If the CV is CLEAN of the fake signature (real LinkedIn, verifiable "
        "Polish/EU university, traceable employer history), score normally. "
        "Polish tech has real talent — Allegro, CD Projekt Red, LiveChat, "
        "Booksy, DocPlanner are tier-1 employers.\n\n"
    ),
    # Other geos: empty for now, to be seeded as we calibrate them.
    # Add Israel after the next IL backend calibration, etc.
    "Israel": "",
    "Canada": "",
    "Brazil": "",
    "United States": "",
    "United Kingdom": "",
}


def normalise_location(raw: str | None) -> str:
    """Map a free-form location string from Comeet to a geo overlay key.

    Handles country codes (PL → Poland), city names (Tel-Aviv → Israel),
    and case variations. Returns "" when no overlay matches.

    Kept in sync with the location normaliser in the positions endpoint
    so the same string buckets to the same overlay everywhere.
    """
    if not raw:
        return ""
    s = (raw or "").strip()
    if not s:
        return ""
    lower = s.lower()

    # Country-code shortcuts (Comeet often uses ISO-style codes).
    code_map = {
        "pl": "Poland",
        "il": "Israel",
        "ca": "Canada",
        "br": "Brazil",
        "us": "United States",
        "usa": "United States",
        "gb": "United Kingdom",
        "uk": "United Kingdom",
    }
    if lower in code_map:
        return code_map[lower]

    # City / region heuristics.
    city_to_country = {
        "tel-aviv": "Israel",
        "tel aviv": "Israel",
        "jerusalem": "Israel",
        "haifa": "Israel",
        "warsaw": "Poland",
        "krakow": "Poland",
        "kraków": "Poland",
        "wroclaw": "Poland",
        "wrocław": "Poland",
        "toronto": "Canada",
        "vancouver": "Canada",
        "montreal": "Canada",
        "sao paulo": "Brazil",
        "são paulo": "Brazil",
        "rio de janeiro": "Brazil",
        "london": "United Kingdom",
        "new york": "United States",
        "san francisco": "United States",
    }
    for city, country in city_to_country.items():
        if city in lower:
            return country

    # Otherwise try matching the exact key (title-case).
    for key in _GEO_OVERLAY.keys():
        if key.lower() == lower:
            return key

    return ""


def get_geo_overlay(location: str | None) -> str:
    """Return the geo-overlay prompt block for this location, or '' if none."""
    key = normalise_location(location)
    if not key:
        return ""
    return _GEO_OVERLAY.get(key, "")


__all__ = ["get_geo_overlay", "normalise_location"]
