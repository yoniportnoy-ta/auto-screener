"""Curated lists of well-known product companies, organised by tier.

These feed into the scoring prompt so Claude has concrete positive-signal
benchmarks when evaluating a candidate's career arc. The lists are
intentionally focused on PRODUCT companies (companies shipping their own
software product to real customers).

Why is this a Python module instead of a JSON / DB table?
  - It's effectively prompt content, version-controlled with the prompt.
  - Reading a JSON file at every score call is silly when the list changes
    a few times a year.
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


def format_company_tiers_block() -> str:
    """Render the company-tier reference as a single prompt block.

    Designed to be embedded in the pre-rating checklist. Two positive-signal
    lists (global + Israeli). No explicit service/agency callouts — Claude
    is left to judge unknown companies based on what the CV describes.
    """
    lines = ["\n--- COMPANY TIER REFERENCE (use this when grading employer signal) ---"]

    lines.append("\nTIER-1 GLOBAL (FAANG, recognised unicorns — strong positive signal):")
    lines.append(_csv_wrap(TIER_1_GLOBAL))

    lines.append("\nTIER-1 ISRAELI (top scale-ups, public, unicorns — strong positive signal):")
    lines.append(_csv_wrap(TIER_1_ISRAELI))

    lines.append(
        "\nIf an employer isn't on either list, judge by what's described in the CV: "
        "if they ship their own product/SaaS to real customers, treat as Tier-2 product. "
        "If the CV makes the company sound generic / no clear product / per-client work, "
        "that's a weaker signal."
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
    "format_company_tiers_block",
]
