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


# ─── TIER-1 ISRAELI: top scale-ups / public companies ───────────────────
# Curated list of well-known Israeli product/tech companies. The bar:
# either publicly traded, valued >$1B, broadly recognised in the
# Israeli tech ecosystem, or category-leading in their niche.
TIER_1_ISRAELI = [
    # Public / mega-cap consumer + SaaS
    "Wix", "monday.com", "monday", "Fiverr", "ironSource", "Unity",
    "AppsFlyer", "JFrog", "Riskified", "Global-e", "WalkMe",
    "Innovid", "Taboola", "Outbrain", "Cellebrite", "Nayax",
    "Lightricks",
    # Public/mega-cap fintech + insurtech
    "Lemonade", "Hippo", "Plus500", "Pagaya", "Payoneer", "Nuvei",
    "eToro", "Next Insurance",
    # Public/mega-cap cyber
    "Check Point", "CyberArk", "Imperva", "Radware", "Allot",
    "SentinelOne", "Cybereason",
    # Mature enterprise software
    "NICE", "Amdocs", "Verint", "Sapiens", "Mellanox",
    "Tower Semiconductor",
    # Mobility / auto
    "Mobileye", "Gett", "Via", "Moovit",
    # Late-stage unicorns — cyber
    "Snyk", "Wiz", "Aqua Security", "Orca Security", "Cato Networks",
    "Salt Security", "Pentera", "Apiiro", "Akeyless", "Cycode",
    "Sygnia", "Mitiga", "Adaptive Shield", "Coro", "Perimeter 81",
    "Deep Instinct", "Claroty", "Armis", "Cyolo",
    "Talon Cyber Security", "BigID", "Mend", "WhiteSource",
    "Argus Cyber Security", "Cymulate", "Lightspin", "Permiso",
    # Late-stage unicorns — fintech / insurtech
    "Tipalti", "Melio", "Rapyd", "BlueVine", "Mesh Payments",
    "HoneyBook", "Personetics", "At-Bay", "Forter", "Earnix",
    # AI / ML
    "AI21 Labs", "AI21", "Pinecone", "Run:ai", "Run.ai", "Granulate",
    "Tabnine", "D-ID", "Hour One", "Aporia", "Anyword", "DataLoop",
    "Lightrun", "Coralogix",
    # Marketing / sales / data
    "Gong", "Lusha", "Yotpo", "Optimove", "Innovid",
    # Productivity / SaaS
    "HiBob", "Atera", "DataRails", "Verbit", "BigPanda", "Bringg",
    # Mobility / auto (unicorn-tier)
    "Innoviz", "Otonomo", "Cybellum", "Optibus", "Foretellix",
    "Trigo Vision", "Trigo",
    # Health
    "Tytocare", "Healthy.io", "Aidoc", "Zebra Medical Vision",
    "K Health", "Sweetch",
    # Gaming / media
    "Playtika", "Plarium", "Crazy Labs", "Moon Active",
    "Papaya Gaming", "Eko",
    # Other product
    "Trax", "Riverside", "Riverside.fm",
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

    lines.append("\nTIER-1 GLOBAL (FAANG, recognised unicorns — STRONG POSITIVE signal):")
    lines.append(_csv_wrap(TIER_1_GLOBAL))

    lines.append(
        "\nTIER-1 ISRAELI (top scale-ups, public, unicorns — STRONG POSITIVE signal):"
    )
    lines.append(_csv_wrap(TIER_1_ISRAELI))

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
    "SERVICE_AGENCY_GLOBAL",
    "SERVICE_AGENCY_ISRAELI",
    "HR_STAFFING_AGENCIES",
    "SERVICE_AGENCY_ALL",
    "SERVICE_NAME_PATTERNS",
    "format_company_tiers_block",
]
