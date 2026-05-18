"""Curated university lists, organised by signal direction and geography.

Same pattern as `company_tiers.py`: these feed into the scoring prompt so
Claude has concrete benchmarks when grading the `university_tier` axis.

GEOGRAPHIC FOCUS — Israel, Canada, Poland are the three countries where
most of our open positions live, so their lists are deeper. A global
TIER-1 anchor list is included so candidates with degrees from MIT /
Oxford / Tsinghua / etc. are graded correctly regardless of country.

POSITIVE lists (real academic signal):
  - TIER_1_GLOBAL        Top global research universities (anchor set)
  - TIER_1_ISRAEL        Top Israeli research universities + elite programs
  - TIER_2_ISRAEL        Respected Israeli colleges / specialised institutions
  - TIER_1_CANADA        Canadian U15 / globally recognised research unis
  - TIER_2_CANADA        Respected Canadian regional / mid-tier universities
  - TIER_1_POLAND        Top Polish research + technical universities
  - TIER_2_POLAND        Solid Polish regional / specialised institutions

NEGATIVE-LEANING lists (weaker academic signal — not disqualifying on
its own, but contributes negatively when stacked with other weak signals):
  - LOW_TIER_ISRAEL      Smaller Israeli teaching colleges
  - LOW_TIER_CANADA      Smaller Canadian colleges / polytechnics
  - LOW_TIER_POLAND      Smaller Polish private / regional schools

Plus ELITE_PROGRAMS — Israeli military / national programs that often
matter MORE than the university itself (Talpiot, 8200, Mamram, etc).

Why a Python module and not a JSON / DB table?
  - It's prompt content, version-controlled with the prompt.
  - Reading JSON at every score call is wasted I/O; the list changes a
    few times a year, not per-request.
  - Code review is the natural place to debate inclusions / exclusions.
"""
from __future__ import annotations


# ─── TIER-1 GLOBAL: anchor set of globally recognised research universities ─
# Used so Claude grades a Stanford or Tsinghua degree consistently regardless
# of where the candidate is applying from. Not exhaustive — just the names
# that should obviously be 9-10.
TIER_1_GLOBAL = [
    # US elite — Ivy + STEM
    "MIT", "Massachusetts Institute of Technology",
    "Stanford University", "Stanford",
    "Harvard University", "Harvard",
    "Princeton University", "Princeton",
    "Yale University", "Yale",
    "Caltech", "California Institute of Technology",
    "Carnegie Mellon University", "CMU",
    "University of California Berkeley", "UC Berkeley", "Berkeley",
    "University of California Los Angeles", "UCLA",
    "University of Pennsylvania", "UPenn", "Penn",
    "Columbia University", "Columbia",
    "Cornell University", "Cornell",
    "Brown University", "Brown",
    "Dartmouth College", "Dartmouth",
    "University of Chicago", "UChicago",
    "Northwestern University", "Northwestern",
    "Duke University", "Duke",
    "Johns Hopkins University", "Johns Hopkins",
    "University of Michigan", "Michigan",
    "Georgia Institute of Technology", "Georgia Tech",
    "University of Illinois Urbana-Champaign", "UIUC",
    "University of Texas at Austin", "UT Austin",
    "University of Wisconsin-Madison",
    "University of Washington",
    "New York University", "NYU",
    "University of Southern California", "USC",
    # UK
    "University of Cambridge", "Cambridge",
    "University of Oxford", "Oxford",
    "Imperial College London", "Imperial College",
    "University College London", "UCL",
    "London School of Economics", "LSE",
    "University of Edinburgh", "Edinburgh",
    "King's College London", "KCL",
    "University of Manchester",
    "University of Warwick",
    # Continental Europe
    "ETH Zürich", "ETH Zurich", "Swiss Federal Institute of Technology",
    "EPFL", "École Polytechnique Fédérale de Lausanne",
    "Technical University of Munich", "TU München", "TUM",
    "Ludwig Maximilian University of Munich", "LMU Munich",
    "Heidelberg University",
    "KU Leuven",
    "Sorbonne University",
    "Paris-Saclay University", "Université Paris-Saclay",
    "École Polytechnique", "Polytechnique Paris",
    "TU Delft", "Delft University of Technology",
    "University of Amsterdam",
    "Bocconi University",
    "Karolinska Institute",
    # Asia
    "Tsinghua University", "Tsinghua",
    "Peking University",
    "National University of Singapore", "NUS",
    "Nanyang Technological University", "NTU Singapore",
    "Hong Kong University of Science and Technology", "HKUST",
    "University of Hong Kong", "HKU",
    "KAIST", "Korea Advanced Institute of Science and Technology",
    "Seoul National University",
    "University of Tokyo",
    "Kyoto University",
    "IIT Bombay", "IIT Delhi", "IIT Madras", "IIT Kanpur",
    "IIT Kharagpur", "IIT Roorkee", "IIT Hyderabad",
    "Indian Institute of Science", "IISc",
    # Australia
    "University of Melbourne",
    "University of Sydney",
    "Australian National University", "ANU",
]


# ─── TIER-1 ISRAEL: top research universities + elite institutions ──────────
# The bar: globally recognised research output, strong undergrad selectivity,
# faculty publishing in top venues, and pipeline into Israeli/global tech.
TIER_1_ISRAEL = [
    "Technion", "Technion – Israel Institute of Technology",
    "Technion - Israel Institute of Technology",
    "Tel Aviv University", "TAU",
    "Hebrew University of Jerusalem", "Hebrew University", "HUJI",
    "Weizmann Institute of Science", "Weizmann Institute",
    "Bar-Ilan University", "Bar Ilan University",
    "Ben-Gurion University of the Negev", "Ben Gurion University", "BGU",
    "University of Haifa", "Haifa University",
    "Reichman University", "IDC Herzliya",
    "Interdisciplinary Center Herzliya",
]


# ─── TIER-2 ISRAEL: respected colleges / specialised institutions ───────────
# Lower than the research universities but still serious academic signal.
# Most are accredited "academic colleges" (Mihlala Akademit) — solid
# engineering / business / design programs, but not research-led.
TIER_2_ISRAEL = [
    "Open University of Israel", "Open University",
    "Afeka College of Engineering", "Afeka Tel Aviv Academic College of Engineering",
    "Holon Institute of Technology", "HIT",
    "Shenkar College", "Shenkar College of Engineering and Design",
    "Sami Shamoon College of Engineering", "SCE",
    "Azrieli College of Engineering", "Azrieli College of Engineering Jerusalem",
    "Bezalel Academy of Arts and Design", "Bezalel",
    "College of Management Academic Studies", "COMAS", "Rishon LeZion College",
    "Ono Academic College",
    "Ariel University",
    "Jerusalem College of Technology", "Machon Lev", "JCT",
    "Hadassah Academic College",
    "Peres Academic Center",
    "Netanya Academic College",
    "Tel Aviv-Yaffo Academic College",
    "Tel-Hai Academic College", "Tel Hai College",
]


# ─── LOW-TIER ISRAEL: smaller / regional / mostly-teaching colleges ─────────
# Not disqualifying. Stacks negatively only when combined with other weak
# signals (no real product experience, agency career, etc.).
LOW_TIER_ISRAEL = [
    "Kinneret College", "Kinneret Academic College",
    "Sapir Academic College", "Sapir College",
    "Western Galilee College", "Western Galilee Academic College",
    "Achva Academic College",
    "Emek Yezreel College", "Yezreel Valley College",
    "Ruppin Academic Center",
    "Zefat Academic College", "Safed Academic College",
    "Ashkelon Academic College",
    "Kibbutzim College of Education",
    "Levinsky College of Education",
    "Talpiot Academic College of Education",
    "Beit Berl College",
    "Oranim College",
    "David Yellin College",
]


# ─── ISRAELI ELITE PROGRAMS: military / national pipelines ──────────────────
# These often matter MORE than the university for Israeli tech candidates.
# Treat membership as a STRONG POSITIVE signal — graduates are pre-vetted
# for high IQ, technical depth, and leadership. Bump university_tier toward
# 9-10 when the CV explicitly mentions one of these.
ISRAEL_ELITE_PROGRAMS = [
    "Talpiot",
    "8200", "Unit 8200", "Yehida 8200",
    "Mamram",
    "9900", "Unit 9900",
    "81", "Unit 81",
    "Atuda", "Atuda Akademait",
    "Havatzalot", "Havatzalot Program",
    "Psagot",
    "Tzameret",  # IDF medical reserve track for top students
    "Brakim", "Brakim Program",  # IDF lawyers track
]


# ─── TIER-1 CANADA: U15-aligned, globally recognised research universities ──
# The U15 is Canada's research-intensive university group. Adding Waterloo
# explicitly (already U15) because of its outsized tech / engineering rep.
TIER_1_CANADA = [
    "University of Toronto", "UofT", "U of T",
    "University of British Columbia", "UBC",
    "McGill University", "McGill",
    "University of Waterloo", "Waterloo",
    "Université de Montréal", "University of Montreal", "UdeM",
    "McMaster University", "McMaster",
    "University of Alberta", "UofA",
    "Queen's University", "Queens University",
    "Western University", "University of Western Ontario", "UWO",
    "Université Laval", "Laval University",
    "University of Ottawa", "uOttawa",
    "University of Calgary", "UCalgary",
    "Dalhousie University", "Dal",
    "University of Manitoba", "UManitoba",
    "University of Saskatchewan", "USask",
]


# ─── TIER-2 CANADA: respected mid-tier research + comprehensive universities ─
TIER_2_CANADA = [
    "Simon Fraser University", "SFU",
    "York University", "York U",
    "Concordia University", "Concordia",
    "Toronto Metropolitan University", "Ryerson University", "TMU",
    "Carleton University", "Carleton",
    "University of Victoria", "UVic",
    "University of Guelph", "Guelph",
    "Memorial University of Newfoundland", "Memorial University", "MUN",
    "Brock University",
    "Wilfrid Laurier University", "Laurier",
    "University of Windsor",
    "University of New Brunswick", "UNB",
    "Université du Québec à Montréal", "UQAM",
    "Université de Sherbrooke",
    "Polytechnique Montréal",
    "École de technologie supérieure", "ÉTS",
    "HEC Montréal",
    "Ivey Business School",
    "Rotman School of Management",
    "Schulich School of Business",
    "Smith School of Business",
]


# ─── LOW-TIER CANADA: smaller universities + polytechnics + colleges ────────
# These award legitimate degrees but the brand recognition / academic
# selectivity is lower. Bootcamp-equivalents and applied-only colleges
# belong here too.
LOW_TIER_CANADA = [
    "Athabasca University",
    "Royal Roads University",
    "Trinity Western University",
    "Mount Royal University",
    "MacEwan University",
    "Thompson Rivers University",
    "Vancouver Island University",
    "Capilano University",
    "Kwantlen Polytechnic University",
    "British Columbia Institute of Technology", "BCIT",
    "Northern Alberta Institute of Technology", "NAIT",
    "Southern Alberta Institute of Technology", "SAIT",
    "Seneca College", "Seneca Polytechnic",
    "Humber College",
    "George Brown College",
    "Centennial College",
    "Sheridan College",
    "Algonquin College",
    "Conestoga College",
    "Fanshawe College",
    "Mohawk College",
    "Lambton College",
    "Cape Breton University",
    "Lakehead University",
    "Nipissing University",
    "Brandon University",
    "University of Lethbridge",
    "University of Regina",
    "University of Winnipeg",
    "University of Prince Edward Island", "UPEI",
    "St. Francis Xavier University", "StFX",
    "Acadia University",
    "Mount Saint Vincent University",
    "Saint Mary's University", "SMU Halifax",
]


# ─── TIER-1 POLAND: top research + flagship technical universities ──────────
TIER_1_POLAND = [
    "University of Warsaw", "Uniwersytet Warszawski", "UW",
    "Jagiellonian University", "Uniwersytet Jagielloński", "UJ",
    "Warsaw University of Technology", "Politechnika Warszawska", "PW",
    "AGH University of Science and Technology",
    "AGH University of Krakow", "AGH",
    "Akademia Górniczo-Hutnicza",
    "Adam Mickiewicz University", "Uniwersytet im. Adama Mickiewicza", "UAM",
    "Wrocław University of Science and Technology",
    "Wrocław University of Technology", "Politechnika Wrocławska", "PWr",
    "SGH Warsaw School of Economics", "Szkoła Główna Handlowa", "SGH",
]


# ─── TIER-2 POLAND: solid regional research + technical universities ────────
TIER_2_POLAND = [
    "University of Wrocław", "Uniwersytet Wrocławski",
    "Gdańsk University of Technology", "Politechnika Gdańska",
    "Lodz University of Technology", "Politechnika Łódzka",
    "Silesian University of Technology", "Politechnika Śląska",
    "Cracow University of Technology", "Politechnika Krakowska",
    "Poznań University of Technology", "Politechnika Poznańska",
    "University of Łódź", "Uniwersytet Łódzki",
    "University of Gdańsk", "Uniwersytet Gdański",
    "University of Silesia in Katowice", "Uniwersytet Śląski",
    "Nicolaus Copernicus University", "Uniwersytet Mikołaja Kopernika",
    "Maria Curie-Skłodowska University", "UMCS",
    "University of Białystok",
    "Warsaw University of Life Sciences", "SGGW",
    "Kozminski University", "Akademia Leona Koźmińskiego",
    "Poznań University of Economics and Business",
    "Cracow University of Economics", "Uniwersytet Ekonomiczny w Krakowie",
    "Medical University of Warsaw",
    "Lublin University of Technology",
    "West Pomeranian University of Technology",
    "Bialystok University of Technology",
    "Rzeszów University of Technology",
    "Częstochowa University of Technology",
]


# ─── LOW-TIER POLAND: smaller private / regional / vocational ──────────────
LOW_TIER_POLAND = [
    "Polish-Japanese Academy of Information Technology", "PJATK", "PJAIT",
    "Collegium Civitas",
    "Lazarski University",
    "University of Information Technology and Management in Rzeszów",
    "WSB University", "WSB Universities",
    "Warsaw School of Computer Science",
    "WSEI", "University of Economics and Innovation in Lublin",
    "SWPS University", "Uniwersytet SWPS",
    "Vistula University",
    "Akademia Finansów i Biznesu Vistula",
    "WSIiZ", "University of Information Technology and Management",
    "Akademia Ekonomiczno-Humanistyczna",
    "WSPA", "University College of Enterprise and Administration",
    "WSH", "Wyższa Szkoła Humanitas",
]


# Convenience aggregates for prompt rendering.
ALL_TIER_1 = (
    TIER_1_GLOBAL + TIER_1_ISRAEL + TIER_1_CANADA + TIER_1_POLAND
)
ALL_TIER_2 = TIER_2_ISRAEL + TIER_2_CANADA + TIER_2_POLAND
ALL_LOW_TIER = LOW_TIER_ISRAEL + LOW_TIER_CANADA + LOW_TIER_POLAND


def format_university_tiers_block() -> str:
    """Render the curated university lists as a single prompt block.

    Mirrors `company_tiers.format_company_tiers_block()`. Embedded in the
    pre-rating checklist so Claude has concrete benchmarks for grading
    the `university_tier` axis. Geographic focus: Israel, Canada, Poland
    (the three countries where most of our positions live).
    """
    lines = ["\n--- UNIVERSITY TIER REFERENCE (use this when grading academic signal) ---"]

    lines.append(
        "\nTIER-1 GLOBAL (top research universities — STRONG POSITIVE, grade 9-10):"
    )
    lines.append(_csv_wrap(TIER_1_GLOBAL))

    lines.append(
        "\nTIER-1 ISRAEL (top Israeli research universities — STRONG POSITIVE, grade 8-10):"
    )
    lines.append(_csv_wrap(TIER_1_ISRAEL))

    lines.append(
        "\nTIER-2 ISRAEL (respected Israeli academic colleges — POSITIVE, grade 6-7):"
    )
    lines.append(_csv_wrap(TIER_2_ISRAEL))

    lines.append(
        "\nLOW-TIER ISRAEL (smaller Israeli teaching colleges — WEAK signal, grade 3-5):"
    )
    lines.append(_csv_wrap(LOW_TIER_ISRAEL))

    lines.append(
        "\nISRAEL ELITE PROGRAMS (military / national tracks — STRONG POSITIVE; "
        "presence of one of these on the CV often matters MORE than the university "
        "and should bump university_tier toward 9-10):"
    )
    lines.append(_csv_wrap(ISRAEL_ELITE_PROGRAMS))

    lines.append(
        "\nTIER-1 CANADA (U15 / globally recognised Canadian universities — "
        "STRONG POSITIVE, grade 8-10):"
    )
    lines.append(_csv_wrap(TIER_1_CANADA))

    lines.append(
        "\nTIER-2 CANADA (respected mid-tier Canadian universities and business "
        "schools — POSITIVE, grade 6-7):"
    )
    lines.append(_csv_wrap(TIER_2_CANADA))

    lines.append(
        "\nLOW-TIER CANADA (smaller Canadian universities + polytechnics + colleges "
        "— WEAK signal, grade 3-5):"
    )
    lines.append(_csv_wrap(LOW_TIER_CANADA))

    lines.append(
        "\nTIER-1 POLAND (top Polish research + flagship technical universities — "
        "STRONG POSITIVE, grade 8-10):"
    )
    lines.append(_csv_wrap(TIER_1_POLAND))

    lines.append(
        "\nTIER-2 POLAND (solid Polish regional + technical + economics universities "
        "— POSITIVE, grade 6-7):"
    )
    lines.append(_csv_wrap(TIER_2_POLAND))

    lines.append(
        "\nLOW-TIER POLAND (smaller private / regional Polish schools — WEAK signal, "
        "grade 3-5):"
    )
    lines.append(_csv_wrap(LOW_TIER_POLAND))

    lines.append(
        "\nIf the institution is NOT on any list above, judge by what the CV "
        "describes: well-known research output / globally-ranked / produces "
        "real engineers → grade 6-7. Unknown regional school or unclear "
        "accreditation → grade 3-5. Bootcamp or non-accredited online → grade 2-3. "
        "When in doubt, lean lower — the bias here is over-rating unknown "
        "universities, not under-rating them."
    )
    return "\n".join(lines)


def _csv_wrap(names: list[str], width: int = 88) -> str:
    """Wrap a long list of names into roughly-width-bounded comma-separated lines.

    Cosmetic — model doesn't care about line breaks but humans reading
    prompt logs do.
    """
    out_lines: list[str] = []
    current = ""
    for n in names:
        piece = (", " if current else "") + n
        if current and len(current) + len(piece) > width:
            out_lines.append(current)
            current = n
        else:
            current = current + piece
    if current:
        out_lines.append(current)
    return "  " + "\n  ".join(out_lines)


__all__ = [
    "TIER_1_GLOBAL",
    "TIER_1_ISRAEL",
    "TIER_2_ISRAEL",
    "LOW_TIER_ISRAEL",
    "ISRAEL_ELITE_PROGRAMS",
    "TIER_1_CANADA",
    "TIER_2_CANADA",
    "LOW_TIER_CANADA",
    "TIER_1_POLAND",
    "TIER_2_POLAND",
    "LOW_TIER_POLAND",
    "ALL_TIER_1",
    "ALL_TIER_2",
    "ALL_LOW_TIER",
    "format_university_tiers_block",
]
