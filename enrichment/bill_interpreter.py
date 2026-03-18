"""
enrichment/bill_interpreter.py
===============================
Parses Oireachtas bill/vote titles and infers plain-English context for
the conflict analysis section of TD profiles.

Two public functions:

  parse_bill_title(title)
    Splits a raw Oireachtas debate title into its components.
    e.g. "Commission on the Future of the Family Farm Bill 2024:
          Second Stage (Resumed) [Private Members]"
    → { clean_name, year, stage, stage_label, bill_type, bill_type_label }

  interpret_vote(sector, bill_title, voted_ta_nil)
    Given the sector (e.g. "property"), raw title, and "Tá"/"Níl",
    returns a plain-English alignment note and confidence.
    → { direction_label, alignment, alignment_note, confidence }

Important: alignment notes are prefaced with "may" / "could" and are
clearly framed as observations, not conclusions. They must not allege
wrongdoing.
"""

import re

# ---------------------------------------------------------------------------
# Stage parsing
# ---------------------------------------------------------------------------

_STAGE_MAP: dict[str, str] = {
    "first stage":            "Bill formally introduced — no debate yet",
    "second stage":           "General debate on the bill's principles",
    "committee stage":        "Detailed line-by-line scrutiny of the text",
    "report stage":           "Further amendments after committee review",
    "report and final stage": "Final amendments and last vote before Seanad",
    "final stage":            "Last vote in the Dáil before sending to the Seanad",
    "from the seanad":        "Returned from the Seanad with amendments",
    "all stages":             "Passed through all stages in one sitting",
    "committee and remaining stages": "Committee stage plus all remaining stages",
}


def _match_stage(stage_raw: str) -> tuple[str, str]:
    """Return (normalised_stage, plain_English_label)."""
    s = stage_raw.lower().strip()
    # Remove "(Resumed)" annotation
    s = re.sub(r"\(resumed\)", "", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    for key, label in _STAGE_MAP.items():
        if key in s:
            return key.title(), label
    return stage_raw.strip(), ""


def parse_bill_title(title: str) -> dict:
    """
    Parse a raw Oireachtas debate title.

    Parameters
    ----------
    title : e.g. "Commission on the Future of the Family Farm Bill 2024:
                  Second Stage (Resumed) [Private Members]"

    Returns
    -------
    {
      clean_name   : "Commission on the Future of the Family Farm Bill 2024",
      year         : "2024"  (or "" if not found),
      stage        : "Second Stage",
      stage_label  : "General debate on the bill's principles",
      bill_type    : "private_members" | "government" | "motion" | "unknown",
      bill_type_label : "Private Members' Bill" | "Government Bill" | "Motion" | "",
      is_resumed   : bool,
    }
    """
    raw = title.strip()

    # ── Bill type ────────────────────────────────────────────────────────────
    is_private = bool(re.search(r"\[private members?\]", raw, re.I))
    is_motion  = bool(re.search(r"\bmotion\b", raw, re.I))

    if is_private and is_motion:
        bill_type       = "private_motion"
        bill_type_label = "Private Members' Motion"
    elif is_private:
        bill_type       = "private_members"
        bill_type_label = "Private Members' Bill"
    elif is_motion:
        bill_type       = "motion"
        bill_type_label = "Motion"
    else:
        bill_type       = "government"
        bill_type_label = "Government Bill"

    is_resumed = bool(re.search(r"\(resumed\)", raw, re.I))

    # ── Strip tags / annotations ─────────────────────────────────────────────
    cleaned = re.sub(r"\[.*?\]", "", raw).strip()   # remove [Private Members] etc.

    # ── Split at colon to get name vs stage ──────────────────────────────────
    if ":" in cleaned:
        name_part, stage_part = cleaned.split(":", 1)
    else:
        name_part  = cleaned
        stage_part = ""

    name_part  = name_part.strip()
    stage_part = stage_part.strip()
    stage_part = re.sub(r"\(resumed\)", "", stage_part, flags=re.I).strip()
    stage_part = re.sub(r"\s+", " ", stage_part).strip()

    # For motions, the stage might be in the name part itself
    if is_motion and not stage_part:
        # e.g. "Emergency Action on Housing: Motion (Resumed)"
        # name_part is already "Emergency Action on Housing: Motion"
        name_part  = re.sub(r":\s*motion.*$", "", name_part, flags=re.I).strip()
        stage_part = "Motion"

    stage, stage_label = _match_stage(stage_part) if stage_part else ("", "")

    # ── Extract year ─────────────────────────────────────────────────────────
    m = re.search(r"\b(20\d{2})\b", name_part)
    year = m.group(1) if m else ""

    return {
        "clean_name":      name_part,
        "year":            year,
        "stage":           stage,
        "stage_label":     stage_label,
        "bill_type":       bill_type,
        "bill_type_label": bill_type_label,
        "is_resumed":      is_resumed,
    }


# ---------------------------------------------------------------------------
# Vote alignment inference
# ---------------------------------------------------------------------------

# For each sector, define keyword sets that characterise:
#   pro_interest  — bills that tend to benefit TDs with this sector interest
#   counter_interest — bills that tend to impose costs/obligations on them
#
# Then:
#   voted Tá on pro_interest → "may align with declared interest"
#   voted Níl on counter_interest → "may protect declared interest"
#   voted Níl on pro_interest → "voted against bill that could benefit their sector"
#   voted Tá on counter_interest → "voted for bill that may affect their sector interests"

_ALIGNMENT_RULES: dict[str, dict] = {
    "property": {
        "interest_label": "property/landlord interests",
        # Bills that increase costs or obligations for landlords/developers
        "counter_interest": [
            "building energy rating", "ber standards", "private rented",
            "tenant in situ", "social housing tenant",
            "emergency action on housing", "homeless",
            "vacant council housing",
        ],
        # Bills that benefit property owners, increase development, ease planning
        "pro_interest": [
            "planning and development", "accelerate housing delivery",
            "housing and critical infrastructure",
            "legislative and structural reforms to accelerate",
            "land development", "planning permission",
            "national planning framework",
        ],
    },
    "agriculture": {
        "interest_label": "farming/agricultural interests",
        "counter_interest": [
            "ban on fox hunting", "fox hunting",
            "animal health and welfare",
        ],
        "pro_interest": [
            "family farm", "commission on the future of the family farm",
            "agriculture bill", "rural",
        ],
    },
    "finance": {
        "interest_label": "financial/investment interests",
        "counter_interest": [
            "ending the central bank", "israel bonds",
            "central bank facilitation",
        ],
        "pro_interest": [
            "moneylending", "credit union",
        ],
    },
    "legal": {
        "interest_label": "legal professional interests",
        "counter_interest": [],
        "pro_interest": [
            "legal services", "solicitors",
        ],
    },
    "construction": {
        "interest_label": "construction/development interests",
        "counter_interest": [],
        "pro_interest": [
            "planning and development",
            "accelerate housing delivery",
            "planning permission",
        ],
    },
    "energy": {
        "interest_label": "energy sector interests",
        "counter_interest": [
            "energy costs",
        ],
        "pro_interest": [
            "electricity supply", "offshore wind", "renewable",
        ],
    },
    "health": {
        "interest_label": "healthcare sector interests",
        "counter_interest": [],
        "pro_interest": [
            "mental health", "pharmaceutical", "nursing home", "medical card",
        ],
    },
    "media": {
        "interest_label": "media sector interests",
        "counter_interest": [
            "online safety", "recommender algorithms",
            "broadcasting oversight", "rte accounts",
        ],
        "pro_interest": [],
    },
    "tourism_hospitality": {
        "interest_label": "tourism/hospitality interests",
        "counter_interest": [],
        "pro_interest": [
            "tourism", "hospitality",
        ],
    },
    "transport": {
        "interest_label": "transport sector interests",
        "counter_interest": [],
        "pro_interest": [
            "road transport", "bus services",
        ],
    },
}


def _bill_stance(sector: str, title_lower: str) -> str:
    """
    Returns 'pro_interest', 'counter_interest', or 'neutral'.
    """
    rules = _ALIGNMENT_RULES.get(sector, {})
    for kw in rules.get("counter_interest", []):
        if kw in title_lower:
            return "counter_interest"
    for kw in rules.get("pro_interest", []):
        if kw in title_lower:
            return "pro_interest"
    return "neutral"


def interpret_vote(sector: str, bill_title: str, voted: str) -> dict:
    """
    Interpret a single vote in the context of the TD's sector interest.

    Parameters
    ----------
    sector     : e.g. "property"
    bill_title : raw debate title (cleaned name is also fine)
    voted      : "Tá" or "Níl"

    Returns
    -------
    {
      direction_label  : "Voted in favour" | "Voted against",
      alignment        : "aligned" | "counter" | "unclear",
      alignment_note   : plain-English sentence,
      confidence       : "medium" | "low",
    }
    """
    rules = _ALIGNMENT_RULES.get(sector, {})
    interest_label = rules.get("interest_label", f"{sector} interests")
    title_lower    = bill_title.lower()
    stance         = _bill_stance(sector, title_lower)

    voted_for = voted.strip().lower() in ("tá", "for", "yes", "aye")
    direction = "Voted in favour" if voted_for else "Voted against"

    # ── Determine alignment ──────────────────────────────────────────────────
    if stance == "neutral":
        alignment = "unclear"
        note = (
            f"This bill relates to the {sector} sector, where this TD has declared interests. "
            f"Whether voting {direction.lower()} aligned with or against those interests "
            f"depends on the bill's specific provisions."
        )
        confidence = "low"

    elif stance == "pro_interest":
        if voted_for:
            alignment = "aligned"
            note = (
                f"This bill could benefit those with {interest_label}. "
                f"Voting in favour may be consistent with those declared interests."
            )
        else:
            alignment = "counter"
            note = (
                f"This bill could benefit those with {interest_label}. "
                f"Voting against it appears contrary to that financial interest."
            )
        confidence = "medium"

    else:  # counter_interest
        if not voted_for:
            alignment = "aligned"
            note = (
                f"This bill would impose new obligations or costs on those with {interest_label}. "
                f"Voting against it may protect that interest."
            )
        else:
            alignment = "counter"
            note = (
                f"This bill would impose new obligations or costs on those with {interest_label}. "
                f"Voting in favour appears contrary to that financial interest."
            )
        confidence = "medium"

    return {
        "direction_label": direction,
        "alignment":       alignment,
        "alignment_note":  note,
        "confidence":      confidence,
    }


# ---------------------------------------------------------------------------
# Batch enrichment: apply to all votes in a records list
# ---------------------------------------------------------------------------

def enrich_vote_conflicts(records: list[dict]) -> list[dict]:
    """
    Add parsed bill data and vote interpretation to all vote_conflicts entries.
    Mutates records in-place. Returns them.
    """
    for td in records:
        for conflict in td.get("vote_conflicts", []):
            sector = conflict.get("sector", "")
            enriched_votes = []
            for v in conflict.get("votes", []):
                raw_title = v.get("title", "")
                parsed    = parse_bill_title(raw_title)
                interp    = interpret_vote(sector, raw_title, v.get("voted", ""))
                enriched_votes.append({
                    **v,
                    "bill": parsed,
                    "interp": interp,
                })
            conflict["votes"] = enriched_votes
    return records


# ---------------------------------------------------------------------------
# CLI self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_titles = [
        "Commission on the Future of the Family Farm Bill 2024: Second Stage (Resumed) [Private Members]",
        "Animal Health and Welfare (Ban on Fox Hunting) Bill 2025: Second Stage (Resumed) [Private Members]",
        "Building Energy Rating (BER) Standards for Private Rented Accommodation Bill 2025: Second Stage (Resumed) [Private Members]",
        "Planning and Development (Amendment) Bill 2025: Committee and Remaining Stages",
        "Planning and Development (Amendment) Bill 2025: From the Seanad",
        "Ending the Central Bank's Facilitation of the Sale of Israel Bonds: Motion (Resumed) [Private Members]",
        "Emergency Action on Housing and Homelessness: Motion (Resumed) [Private Members]",
        "Social Housing Tenant In Situ Scheme: Motion (Resumed) [Private Members]",
        "Defamation (Amendment) Bill 2024: Report Stage",
        "Mental Health Bill 2024: Committee Stage (Resumed)",
        "Electricity (Supply) (Amendment) Bill 2025: Committee and Remaining Stages",
        "Online Safety (Recommender Algorithms) Bill 2026: Second Stage (Resumed) [Private Members]",
    ]

    print(f"{'Clean name':<65} {'Stage':<25} {'Type':<25}")
    print("-" * 120)
    for t in test_titles:
        p = parse_bill_title(t)
        print(f"{p['clean_name'][:65]:<65} {p['stage']:<25} {p['bill_type_label']:<25}")

    print()
    print("── Vote alignment tests ──────────────────────────────────────────")
    tests = [
        ("property", "Building Energy Rating (BER) Standards for Private Rented Accommodation Bill 2025", "Tá"),
        ("property", "Building Energy Rating (BER) Standards for Private Rented Accommodation Bill 2025", "Níl"),
        ("agriculture", "Commission on the Future of the Family Farm Bill 2024", "Tá"),
        ("agriculture", "Animal Health and Welfare (Ban on Fox Hunting) Bill 2025", "Níl"),
        ("property", "Planning and Development (Amendment) Bill 2025", "Tá"),
        ("finance", "Ending the Central Bank's Facilitation of the Sale of Israel Bonds: Motion", "Tá"),
    ]
    for sector, title, voted in tests:
        r = interpret_vote(sector, title, voted)
        print(f"  [{voted}] {sector}: {r['direction_label']} — {r['alignment'].upper()} ({r['confidence']})")
        print(f"     → {r['alignment_note'][:100]}...")
        print()
