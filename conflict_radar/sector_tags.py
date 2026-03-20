"""
Keyword-based sector tagging for interests, committee names, and vote/debate titles.

Each sector has three keyword lists:
  - interest:   matched against a TD's free-text interest declarations
  - committee:  matched against committee names from the API
  - vote:       matched against debate/bill titles from the votes API

All keyword matching uses whole-word boundaries (\\b) to prevent false positives
like "pub" matching "public", or "fund" matching "Longford".
"""

import re

SECTORS = {
    "agriculture": {
        "interest": [
            "farm", "farmer", "tillage", "livestock", "co-op", "milk", "dairy",
            "glanbia", "tirlan", "tirlán", "drinagh", "agricultural", "mart",
            "creamery", "silage", "beef", "poultry", "fishing", "aquaculture",
        ],
        "committee": ["agriculture", "food", "marine", "fisheries", "agri"],
        "vote": [
            "agriculture", "farm", "food safety", "fisheries", "rural",
            "agri", "tillage", "livestock", "animal health",
        ],
    },
    "property": {
        "interest": [
            "rental", "letting", "rent", "lease", "property", "land",
            "apartment", "hap payment", "hap", "commercial property",
            "residential letting", "landlord",
        ],
        "committee": ["housing", "planning", "local government"],
        "vote": [
            "housing", "planning", "rent", "landlord", "tenancy",
            "residential tenancy", "lease", "land development", "affordable housing",
            "private rented", "buy to let",
        ],
    },
    "finance": {
        "interest": [
            "shares", "davy", "zurich", "aib", "bank of ireland", "permanent tsb",
            "insurance", "fund", "investment", "ryanair", "crh", "aviva",
            "pension", "prsa", "prize bond", "stockbroker",
        ],
        "committee": ["finance", "public accounts", "banking", "public expenditure"],
        "vote": [
            # Deliberately excludes "finance bill", "tax", "budget" — these are voted
            # on by every TD and don't constitute meaningful sector-specific conflicts.
            # Only flag votes on bills that specifically regulate the financial sector.
            "banking", "investment fund", "financial services regulation",
            "credit union", "insurance regulation", "central bank",
            "credit institution", "moneylending",
        ],
    },
    "legal": {
        "interest": [
            "solicitor", "barrister", "law firm", "legal services", "notary",
            "solicitors", "barristers", "legal practice",
        ],
        "committee": ["justice", "law reform", "legal affairs"],
        "vote": [
            "criminal justice", "legal aid", "courts", "solicitors act",
            "legal services", "defamation", "family law",
        ],
    },
    "tourism_hospitality": {
        "interest": [
            "hotel", "tourism", "hospitality", "guesthouse", "b&b",
            "restaurant", "pub", "bar", "accommodation",
        ],
        "committee": ["tourism", "culture", "media", "sport"],
        "vote": ["tourism", "hospitality", "visitor attraction", "fáilte"],
    },
    "transport": {
        "interest": [
            "bus", "bus eireann", "haulage", "freight", "transport company",
            "taxi", "coach", "truck",
        ],
        "committee": ["transport", "infrastructure"],
        "vote": [
            "road transport", "bus services", "rail", "aviation",
            "transport bill", "road traffic", "public transport",
        ],
    },
    "construction": {
        "interest": [
            "construction", "building contractor", "civil engineering",
            "developer", "development company", "building company",
        ],
        "committee": ["infrastructure", "built environment"],
        "vote": [
            "planning permission", "construction", "building regulations",
            "compulsory purchase", "planning and development",
        ],
    },
    "energy": {
        "interest": [
            "energy", "wind farm", "wind turbine", "solar", "oil", "gas",
            "electricity", "renewables",
        ],
        "committee": ["energy", "climate", "environment"],
        "vote": [
            "energy", "climate", "renewable", "electricity", "offshore wind",
            "grid", "fossil fuel", "carbon",
        ],
    },
    "health": {
        "interest": [
            "medical", "pharmaceutical", "pharmacy", "nursing home",
            "gp practice", "hse", "doctor", "dentist", "optician",
        ],
        "committee": ["health", "children"],
        "vote": [
            "health bill", "pharmaceutical", "mental health", "hse",
            "nursing home", "medical card", "drugs payment",
        ],
    },
    "media": {
        "interest": [
            "newspaper", "printing", "publishing", "radio", "television",
            "media company", "broadcast",
        ],
        "committee": ["media", "tourism", "culture"],
        "vote": ["broadcasting", "media", "press", "online safety"],
    },
}

# ---------------------------------------------------------------------------
# Vote alignment: keywords that mark a bill as RESTRICTING / BURDENING the
# sector's interests.  If a bill title matches these, a Níl vote is
# "aligned" with the TD's financial interest (they opposed the burden).
# For all other bills the default is Tá = aligned (they backed the sector).
# ---------------------------------------------------------------------------

RESTRICT_VOTE_KEYWORDS = {
    "property": [
        # Bills/motions that impose burdens on landlords or favour tenants/social housing
        "building energy rating", "ber standards",
        "emergency action on housing", "housing emergency", "emergency measures",
        "social housing", "tenant in situ", "vacant council",
        "eviction ban", "rent freeze", "rent cap",
        "homeless",
    ],
    "agriculture": [
        # Bills restricting farming/rural activities
        "ban on fox hunting", "fox hunting",
        "pesticide restriction", "nitrates directive",
    ],
    "finance": [
        # Motions/bills that restrict financial interests or mandate divestment
        "ending the central bank", "divestment", "disinvestment",
    ],
    "energy": [
        # Motions demanding lower energy costs cut into energy-sector profits
        "energy costs: motion",
    ],
    "media": [
        # Regulation of media platforms and algorithm recommendations
        "online safety", "recommender algorithm",
    ],
    # health, legal, construction, transport: no clear restrict signals in
    # current data — default (Tá = aligned) applies for all their vote titles.
}


def vote_is_restrict(debate_title: str, sector: str) -> bool:
    """
    Return True if the bill title suggests it RESTRICTS or BURDENS the sector's
    declared interests (so a Níl vote would be aligned with those interests).
    """
    kws = RESTRICT_VOTE_KEYWORDS.get(sector, [])
    if not kws:
        return False
    t = debate_title.lower()
    for kw in kws:
        if kw in t:
            return True
    return False


# Debate titles that are purely procedural — exclude from conflict matching
PROCEDURAL_TITLES = {
    "adjournment",
    "order of business",
    "ceisteanna",
    "questions",
    "ráitis",
    "statements",
    "oral answers",
    "private notice",
    "topical issue",
    "ceisteanna - questions",
    "commencement",
    "taoiseach",
    "leaders'",
}


def _normalise(text: str) -> str:
    return text.lower()


def _keyword_match(text: str, keywords: list) -> set:
    """
    Return the subset of keywords that appear as whole words in text.
    Uses \\b word boundaries to prevent e.g. 'pub' matching 'public'.
    Multi-word phrases (e.g. 'bank of ireland') are matched as substrings
    since word boundaries apply at the phrase edges.
    """
    t = _normalise(text)
    matched = set()
    for kw in keywords:
        # Build a pattern: word boundary at start and end of the keyword phrase
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, t):
            matched.add(kw)
    return matched


def tag_interests(interests: dict) -> dict:
    """
    Given a TD's interests dict (category → text), return:
      { sector: [matching keyword strings] }
    Only returns sectors that matched at least one keyword.
    """
    all_text = " ".join(v for v in interests.values() if v)
    result = {}
    for sector, cfg in SECTORS.items():
        hits = _keyword_match(all_text, cfg["interest"])
        if hits:
            result[sector] = sorted(hits)
    return result


def tag_committee(committee_name: str) -> set:
    """Return set of sector labels matching a committee name."""
    matched = set()
    for sector, cfg in SECTORS.items():
        if _keyword_match(committee_name, cfg["committee"]):
            matched.add(sector)
    return matched


def tag_vote(debate_title: str) -> set:
    """
    Return set of sector labels matching a vote/debate title.
    Returns empty set for procedural titles.
    """
    lower = _normalise(debate_title)
    if any(proc in lower for proc in PROCEDURAL_TITLES):
        return set()
    matched = set()
    for sector, cfg in SECTORS.items():
        if _keyword_match(debate_title, cfg["vote"]):
            matched.add(sector)
    return matched
