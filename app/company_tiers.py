"""Curated company lists, organised by signal direction.

These feed into the scoring prompt so Claude has concrete benchmarks
when evaluating a candidate's career arc.

POSITIVE lists (strong signal that the candidate has real product DNA):
  - TIER_1_GLOBAL       FAANG + globally recognised unicorns
  - TIER_1_ISRAELI      top Israeli scale-ups / public companies

NEGATIVE lists (strong signal that the candidate's tenure is at a
per-client / service / staffing shop — title inflation is common,
"senior" at one of these is not equivalent to senior at a product co):
  - SERVICE_AGENCY_GLOBAL    Tata/Wipro/EPAM/Accenture/Capgemini/etc
  - SERVICE_AGENCY_ISRAELI   Matrix IT/Ness/Magic Software/Synamedia/etc
  - HR_STAFFING_AGENCIES     Manpower/Adecco/Atid/Milam HR/Allstars/etc

Plus SERVICE_NAME_PATTERNS — substrings ("Consulting", "Solutions",
"Studio", "Lab", "Recruitment Agency"…) that flag a name as service-shop
by default unless explicitly whitelisted on a TIER-1 list.

Why is this a Python module instead of a JSON / DB table?
  - It's effectively prompt content, version-controlled with the prompt.
  - Reading a JSON file at every score call is silly when the list
    changes a few times a year.
  - Reviewers can tweak categories in code review without spinning up DB.
"""
from __future__ import annotations


# ─── TIER-1 PRODUCT: globally recognised, top-of-mind ───────────────────
# FAANG-equivalent + globally famous unicorns. Working at one of these
# is itself a meaningful resume signal.
TIER_1_GLOBAL = [
    # Big Tech
    "Google", "Alphabet", "Meta", "Facebook", "Apple", "Amazon",
    "Microsoft", "Netflix", "Nvidia", "Tesla", "Oracle", "Salesforce",
    "Adobe", "Intel", "IBM",
    # Globally-known consumer / SaaS
    "Stripe", "Notion", "Figma", "Linear", "Discord", "Slack",
    "Dropbox", "Airbnb", "Uber", "Lyft", "Spotify", "TikTok",
    "ByteDance", "Snap", "Snapchat", "Pinterest", "Reddit",
    "Atlassian", "Shopify", "Twilio", "Square", "Block", "PayPal",
    "Coinbase", "Robinhood", "Plaid", "Brex", "Ramp", "Mercury",
    "Databricks", "Snowflake", "MongoDB", "Confluent", "Elastic",
    "HashiCorp", "GitLab", "GitHub", "Cloudflare", "Datadog",
    "PagerDuty", "ServiceNow", "Workday", "Zoom",
    # AI / ML labs
    "OpenAI", "Anthropic", "DeepMind", "Hugging Face", "Mistral",
    "Cohere", "Perplexity", "Scale AI", "xAI",
]


# ─── TIER-1 ISRAELI: top scale-ups / public companies (tight bar) ───────
# Curated list of household-name Israeli product/tech companies. The bar
# is intentionally STRICT after benchmark feedback: globally-recognised
# brand OR clear category-leader, not just "respected unicorn." Borderline
# names live in TIER_2_PRODUCT_ISRAELI below.
TIER_1_ISRAELI = [
    # Public / mega-cap consumer + SaaS — household names
    "Wix", "monday.com", "monday", "Fiverr", "ironSource", "Unity",
    "JFrog", "Riskified", "Global-e", "WalkMe",
    "Taboola", "Outbrain", "Cellebrite", "Nayax",
    "Lightricks",
    # Public/mega-cap fintech — only the truly large
    "Lemonade", "Plus500", "Pagaya", "Nuvei", "eToro",
    # Public/mega-cap cyber — top of category
    "Check Point", "CyberArk", "Imperva", "Radware",
    "SentinelOne", "Cybereason",
    # Mature enterprise software — household
    "NICE", "Amdocs", "Verint", "Sapiens", "Mellanox",
    "Tower Semiconductor",
    # Mobility / auto — top-of-mind
    "Mobileye", "Gett", "Via", "Moovit",
    # Late-stage unicorns — cyber (top-of-category only)
    "Snyk", "Wiz", "Aqua Security", "Orca Security", "Cato Networks",
    "Claroty",
    # Late-stage unicorns — fintech / insurtech (top-of-category)
    "Tipalti", "Melio", "Rapyd", "BlueVine", "HoneyBook",
    "At-Bay", "Forter", "Hippo", "Next Insurance",
    # AI / ML — top-of-category Israeli AI
    "AI21 Labs", "AI21", "Pinecone", "Run:ai", "Run.ai",
    # Marketing / sales / data — top-of-category
    "Gong",
    # Productivity / SaaS — top-of-category
    "HiBob",
    # Mobility / auto (unicorn-tier)
    "Innoviz", "Optibus",
    # Health — top-of-category
    "Tytocare", "Aidoc",
    # Gaming / media — household
    "Playtika", "Plarium", "Moon Active",
    # Anchor: us
    "Riverside", "Riverside.fm",
]


# ─── TIER-2 PRODUCT ISRAELI: respected unicorns, not household names ────
# These are real product companies with valid scale — meaningfully better
# than "unknown employer" — but BELOW the user's tier-1 bar (Wix / Monday /
# Mobileye / Check Point). Moved here from TIER_1_ISRAELI after benchmark
# feedback explicitly flagged these as not-tier-1 (e.g. "appsflyer isnt",
# "Optimove and Payoneer are not tier 1", "personetics is not tier 1",
# "armis is not top tier"). Grade COMPANY_TIER 5-6 for these, not 8-10.
TIER_2_PRODUCT_ISRAELI = [
    # Mid-cap consumer + SaaS
    "AppsFlyer", "Innovid", "Duda", "Stamply",
    # Mid-cap fintech / insurtech
    "Payoneer", "Personetics", "Earnix", "Mesh Payments",
    "Shift4", "Shift4 Payments",
    # Mid-cap cyber
    "Armis", "Allot", "Salt Security", "Pentera", "Apiiro", "Akeyless",
    "Cycode", "Sygnia", "Mitiga", "Adaptive Shield", "Coro", "Perimeter 81",
    "Deep Instinct", "Cyolo", "Talon Cyber Security", "BigID",
    "Mend", "WhiteSource", "Argus Cyber Security", "Cymulate",
    "Lightspin", "Permiso",
    # Mid-cap AI / ML
    "Granulate", "Tabnine", "D-ID", "Hour One", "Aporia", "Anyword",
    "DataLoop", "Lightrun", "Coralogix",
    # Mid-cap marketing / sales / data
    "Lusha", "Yotpo", "Optimove",
    # Mid-cap productivity / SaaS
    "Atera", "DataRails", "Verbit", "BigPanda", "Bringg",
    # Mid-cap mobility / auto
    "Otonomo", "Cybellum", "Foretellix", "Trigo Vision", "Trigo",
    # Mid-cap health
    "Healthy.io", "Zebra Medical Vision", "K Health", "Sweetch",
    # Mid-cap gaming / media
    "Crazy Labs", "Papaya Gaming", "Eko",
    # Mid-cap other
    "Trax",
]


# ─── SERVICE / AGENCY / CONSULTING / OUTSOURCING / STAFFING ─────────────
# Companies that build / hire FOR clients, not their own product. Strong
# NEGATIVE signal — title seniority here is much less meaningful than at
# product companies because the work is per-engagement, and titles are
# often inflated to give clients a sense of seniority.
#
# Split into sub-categories so we can be explicit in the prompt about
# *why* each one is a negative signal.
SERVICE_AGENCY_GLOBAL = [
    # Global IT outsourcing giants
    "Tata Consultancy Services", "TCS", "Wipro", "Infosys",
    "Cognizant", "HCL Technologies", "HCL", "Tech Mahindra",
    "Larsen & Toubro Infotech", "L&T Infotech", "LTI", "Mindtree",
    "Mphasis", "Hexaware", "NIIT Technologies", "Coforge",
    "Persistent Systems",
    # Eastern European / global agencies
    "EPAM", "EPAM Systems", "Luxoft", "GlobalLogic", "Globant",
    "Endava", "DXC Technology", "Capgemini", "Accenture",
    "Deloitte Digital", "PwC Digital", "KPMG Digital", "Booz Allen Hamilton",
    # SE Asia
    "FPT Software", "FPT Telecom", "TMA Solutions", "Rikkeisoft",
]

SERVICE_AGENCY_ISRAELI = [
    # Israeli IT service shops / development houses
    "Synamedia", "Zemingo", "ValueLabs", "Asseco", "Matrix IT",
    "Ness Technologies", "Magic Software", "Aman Group",
    "John Bryce", "TripleSI", "One Technologies", "Malam Team",
    "Aman", "Taldor", "Mihshuv", "Sela Group",
]

# ─── TIER-1 CANADA: top Canadian tech / SaaS companies ─────────────────
# Household-name Canadian product companies. Bar: publicly traded with
# real scale, globally recognised brand, or category-leading SaaS for
# a real customer base. Includes common alt-spellings + abbreviations
# so name-matching in CVs is robust.
TIER_1_CANADA = [
    # Mega-cap consumer + SaaS
    "Shopify", "Shopify Inc",
    "Lightspeed", "Lightspeed Commerce", "Lightspeed POS",
    "OpenText", "Open Text",
    "Constellation Software", "Constellation Software Inc",
    "Slack", "Slack Technologies",  # originally Canadian, now Salesforce
    "BlackBerry", "BlackBerry Limited", "RIM", "Research In Motion",
    "Hootsuite",
    "Wattpad",
    # Fintech / payments
    "Wealthsimple",
    "Nuvei", "Nuvei Corporation",
    "Hopper",
    "Ceridian", "Dayforce", "Ceridian HCM",
    "Borrowell",
    # AI / ML
    "Cohere", "Cohere AI", "Cohere Inc",
    "Element AI",
    "Layer 6 AI", "Layer 6",
    # Cyber / security / privacy
    "1Password", "AgileBits",
    "TrulyAo", "Trulioo",
    "Magnet Forensics",
    "Absolute Software",
    # E-commerce / marketplaces
    "Faire", "Faire Wholesale",
    "Tucows",
    "Ritual",
    "ApplyBoard",
    # SaaS — verticals
    "Clio", "Clio Cloud",
    "Coveo",
    "Top Hat",
    "D2L", "Desire2Learn",
    "Kinaxis",
    "Vena Solutions", "Vena",
    "ZE PowerGroup",
    # Health tech
    "League",
    "MaRS",  # innovation hub, not company, but candidates list it
    # Media / gaming
    "Ubisoft Montreal", "Ubisoft Toronto", "Ubisoft Quebec",
    "EA Vancouver", "Electronic Arts Vancouver",
    "BioWare",
    "Behaviour Interactive",
    "Eidos-Montréal",
    # Telecom / large cap (recognised but not pure-product)
    "Telus", "Bell Canada", "Rogers Communications",
    # AI labs / research
    "Vector Institute",
    "Mila", "Mila Quebec AI Institute",
]


# ─── TIER-1 POLAND: top Polish tech / SaaS / gaming companies ──────────
# Household-name Polish product companies + leading game studios. Bar:
# publicly traded, broadly recognised, or category-leading. Includes
# Polish-language and English variants for name-matching robustness.
TIER_1_POLAND = [
    # E-commerce / marketplaces
    "Allegro", "Allegro.pl", "Allegro Group",
    # IT / enterprise software (large cap)
    "Asseco", "Asseco Poland", "Asseco SA",
    "Comarch", "Comarch SA",
    # SaaS / unicorns
    "LiveChat", "LiveChat Software", "LiveChat Inc",
    "DocPlanner", "Docplanner", "ZnanyLekarz",
    "Booksy",
    "Brainly", "Brainly.com",
    "Brand24",
    "GetResponse",
    "Estimote",
    "DataWalk",
    "Nethone",
    "Sumo Logic Poland",  # major engineering hub
    "Codility",
    "Spacelift",
    "Vercel Poland",  # major remote PL talent
    "Snowflake Poland",
    # Logistics / fintech
    "InPost", "InPost SA",
    "Pekao", "PKO Bank Polski",  # banking — large cap (less tech)
    # Gaming — Polish gaming is globally famous
    "CD Projekt", "CD Projekt Red", "CDPR", "CD PROJEKT",
    "Techland", "Techland S.A.",
    "11 bit studios", "11 Bit Studios",
    "People Can Fly",
    "Bloober Team",
    "Ten Square Games", "TSG",
    "Huuuge Games", "Huuuge Inc",
    "Reality Pump",
    # Satellite / hardware / deeptech
    "Iceye", "ICEYE",  # Finnish-Polish radar satellites
    "SatRevolution",
    # Biotech / pharma scale-ups
    "Selvita",
    # Media / audio
    "Audioteka",
    "Wirtualna Polska", "WP", "Wirtualna Polska Holding",
    "Onet",
    # Travel / SaaS
    "Tidio", "Tidio Live Chat",
    "Survicate",
    "Edrone",
]


HR_STAFFING_AGENCIES = [
    # Global staffing / HR agencies — recruiting roles AT these are a
    # negative signal for a senior-recruiter hire (they recruited *into*
    # client rosters, not for product teams)
    "Manpower", "ManpowerGroup", "Adecco", "Randstad", "Kelly Services",
    "Hays", "Robert Half", "Korn Ferry", "Allegis Group",
    # Israeli HR / recruiting agencies
    "Atid", "Atid Recruitment Agency", "Milam HR", "Milam",
    "Allstars", "Ethosia", "Talanton", "Manpower Israel",
    "Adam Milo", "AdamMilo", "Niloosoft", "Yael Group", "Yael",
    "Danel Group", "Danel",
]


# Convenience: all service-shop names combined, for the prompt block.
SERVICE_AGENCY_ALL = (
    SERVICE_AGENCY_GLOBAL + SERVICE_AGENCY_ISRAELI + HR_STAFFING_AGENCIES
)


# String literals for "this employer looks like a service company" detection.
# Claude is told to treat any company whose name contains one of these
# substrings as service-shop by default unless it's also on the TIER-1 list.
SERVICE_NAME_PATTERNS = [
    "Consulting", "Consultancy", "Solutions", "Services",
    "Studio", "Lab", "Labs", "Software House",
    "Systems Integration", "IT Services", "Outsourcing",
    "Staffing", "Recruitment Agency", "HR Solutions",
]


def format_company_tiers_block() -> str:
    """Render the full company-tier reference as a single prompt block.

    Two positive-signal lists (global + Israeli tier-1) plus three
    negative-signal lists (global outsourcing, Israeli service shops,
    HR/staffing agencies) plus suspicious-name patterns. Embedded in the
    pre-rating checklist as concrete benchmarks.
    """
    lines = ["\n--- COMPANY TIER REFERENCE (use this when grading employer signal) ---"]

    lines.append("\nTIER-1 GLOBAL (FAANG, household-name unicorns — STRONG POSITIVE signal, grade 8-10):")
    lines.append(_csv_wrap(TIER_1_GLOBAL))

    lines.append(
        "\nTIER-1 ISRAELI (household-name Israeli scale-ups, category leaders — "
        "STRONG POSITIVE signal, grade 8-10):"
    )
    lines.append(_csv_wrap(TIER_1_ISRAELI))

    lines.append(
        "\nTIER-2 PRODUCT ISRAELI (respected Israeli unicorns, NOT household names — "
        "MODERATE POSITIVE, grade 5-6. Real product companies but the bar for tier-1 "
        "is brand recognition / category-leadership which these don't quite meet):"
    )
    lines.append(_csv_wrap(TIER_2_PRODUCT_ISRAELI))

    lines.append(
        "\nTIER-1 CANADA (household-name Canadian product cos: Shopify, OpenText, "
        "Cohere, Wealthsimple, Lightspeed, Constellation, 1Password, Clio, etc. — "
        "STRONG POSITIVE signal, grade 8-10):"
    )
    lines.append(_csv_wrap(TIER_1_CANADA))

    lines.append(
        "\nTIER-1 POLAND (household-name Polish product cos + globally-recognised "
        "game studios: Allegro, CD Projekt Red, LiveChat, Booksy, DocPlanner, "
        "Brainly, InPost, Techland, 11 bit studios, etc. — STRONG POSITIVE signal, "
        "grade 8-10):"
    )
    lines.append(_csv_wrap(TIER_1_POLAND))

    lines.append(
        "\nNAME-MATCHING NOTES: candidates write company names many ways — accept "
        "all these as equivalent:\n"
        "  - Capitalisation variants: AppsFlyer / Appsflyer / appsflyer / APPSFLYER\n"
        "  - Punctuation variants: monday.com / Monday.com / Monday / monday\n"
        "  - With/without 'Inc' / 'Ltd' / 'SA' / 'AG' suffixes\n"
        "  - Hebrew transliterations: Wix (ויקס), Riverside (ריברסייד), etc.\n"
        "  - Polish diacritics: CD Projekt (CD PROJEKT), Łódź / Lodz, etc.\n"
        "  - Acronyms: CDPR (CD Projekt Red), TCS (Tata Consultancy Services), "
        "TAU (Tel Aviv University), UofT (University of Toronto), AGH (AGH Krakow), "
        "BGU (Ben-Gurion University), HUJI (Hebrew University), UJ (Jagiellonian), "
        "UW (University of Warsaw), PW (Politechnika Warszawska / Warsaw UoT).\n"
        "If you see ANY of these variants, match to the canonical entry. Do NOT "
        "treat a known company as 'unknown' just because the candidate's spelling "
        "differs from the reference list.\n"
    )

    lines.append(
        "\nSERVICE / OUTSOURCING — global (STRONG NEGATIVE — they build for "
        "clients, not their own product; titles here are often inflated):"
    )
    lines.append(_csv_wrap(SERVICE_AGENCY_GLOBAL))

    lines.append(
        "\nSERVICE / DEVELOPMENT HOUSES — Israeli (STRONG NEGATIVE — same as above, "
        "per-client engagement work):"
    )
    lines.append(_csv_wrap(SERVICE_AGENCY_ISRAELI))

    lines.append(
        "\nHR / STAFFING / RECRUITMENT AGENCIES (STRONG NEGATIVE for senior "
        "recruiter / TA / HR roles — recruiting AT an agency is a weaker signal "
        "than recruiting in-house for a tier-1 product company):"
    )
    lines.append(_csv_wrap(HR_STAFFING_AGENCIES))

    lines.append(
        "\nSUSPICIOUS NAME PATTERNS (treat a company whose name contains any of "
        "these as service-shop by default unless it's explicitly on the TIER-1 "
        "list above): " + ", ".join(SERVICE_NAME_PATTERNS)
    )

    lines.append(
        "\nIf an employer isn't on either list and the name doesn't match a "
        "suspicious pattern, judge by what the CV describes: if they ship their "
        "own product/SaaS to real customers, treat as Tier-2 product. If the work "
        "is per-client engagements or the company sounds generic, treat as "
        "service-shop and apply the negative signal."
    )
    return "\n".join(lines)


def _csv_wrap(names: list[str], width: int = 88) -> str:
    """Wrap a long list of names into roughly-width-bounded comma-separated lines.

    Just cosmetic — the model doesn't care about line breaks but humans
    reading the prompt in logs do.
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
    "TIER_1_ISRAELI",
    "TIER_2_PRODUCT_ISRAELI",
    "TIER_1_CANADA",
    "TIER_1_POLAND",
    "SERVICE_AGENCY_GLOBAL",
    "SERVICE_AGENCY_ISRAELI",
    "HR_STAFFING_AGENCIES",
    "SERVICE_AGENCY_ALL",
    "SERVICE_NAME_PATTERNS",
    "format_company_tiers_block",
]
