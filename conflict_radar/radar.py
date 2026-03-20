"""
Conflict Radar — core detection logic.

For each TD:
  1. Tags their declared interests with sectors
  2. Tags their committee memberships with sectors
  3. Tags the debates they voted on with sectors
  4. Flags overlaps as potential conflicts

Output is factual only — no inference of wrongdoing.
"""

import json
import re
from pathlib import Path

try:
    from unidecode import unidecode
except ImportError:
    def unidecode(s):
        return s

from conflict_radar.sector_tags import tag_interests, tag_committee, tag_vote, vote_is_restrict
from conflict_radar.oireachtas_api import (
    fetch_members,
    fetch_votes,
    build_member_vote_index,
    DAIL_LABELS,
)


# ---------------------------------------------------------------------------
# Name normalisation — join register names to API names
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """
    Normalise a TD name for fuzzy matching.
    'AIRD, William (Laois)'  →  'aird william'
    'William Aird'           →  'aird william'
    Handles Irish accented chars via unidecode.
    """
    name = re.sub(r"\(.*?\)", "", name)     # remove "(Constituency)"
    name = unidecode(name)                  # ó → o, á → a, etc.
    name = re.sub(r"[^a-zA-Z\s]", " ", name)  # remove punctuation
    name = re.sub(r"\s+", " ", name).strip().lower()
    # Sort tokens alphabetically so "AIRD William" == "William AIRD"
    return " ".join(sorted(name.split()))


def build_name_index(members: list) -> dict:
    """
    Returns { normalised_name: member_dict } for fast lookup.
    Where member_dict has memberCode, fullName, committees, party.
    """
    index = {}
    for m in members:
        key = _norm(m["fullName"])
        index[key] = m
    return index


# Hardcoded overrides for TDs where the register name doesn't fuzzy-match the API.
# Key   = _norm() of the register name
# Value = API fullName to look up instead
#
# Known mismatches (34th Dáil):
#   CLARKE, Sorcha   → API spells it "Sorca Clarke" (missing 'h')
#   FEIGHAN, Frank   → API uses nickname "Frankie Feighan"
#   MACLOCHLAINN     → API writes "Mac Lochlainn" (two words); sorted tokens differ
NAME_OVERRIDES = {
    "clarke sorcha":        "Sorca Clarke",
    "feighan frank":        "Frankie Feighan",
    "maclochlainn padraig": "Pádraig Mac Lochlainn",
}


def _match_member(register_name: str, name_index: dict) -> dict | None:
    """Find the API member record matching a register name string."""
    key = _norm(register_name)
    # Check hardcoded overrides first
    if key in NAME_OVERRIDES:
        api_name = NAME_OVERRIDES[key]
        return name_index.get(_norm(api_name))
    return name_index.get(key)


# ---------------------------------------------------------------------------
# Interest evidence helpers
# ---------------------------------------------------------------------------

def _interest_evidence_for_sector(interests: dict, sector: str) -> str:
    """
    Pull the relevant interest text that triggered a sector tag.
    Returns a short summary string.
    """
    from conflict_radar.sector_tags import SECTORS
    keywords = SECTORS[sector]["interest"]
    snippets = []
    for cat, text in interests.items():
        if not text:
            continue
        text_lower = text.lower()
        if any(kw in text_lower for kw in keywords):
            # Truncate long entries
            snippet = text[:200] + "..." if len(text) > 200 else text
            snippets.append(f"[{cat}] {snippet}")
    return " | ".join(snippets)


# ---------------------------------------------------------------------------
# Main conflict detection
# ---------------------------------------------------------------------------

def run_radar(interests_path: str, year: int = 2025) -> list:
    """
    Run the conflict radar against the interests JSON for a given year.
    Returns list of per-TD conflict reports.
    """
    # Load interests
    with open(interests_path, encoding="utf-8") as f:
        td_interests = json.load(f)
    print(f"  Loaded {len(td_interests)} TD records from {interests_path}")

    # Fetch API data — current Dáil plus 33rd Dáil for ~5 years of history
    members = fetch_members(year=year)
    votes_current = fetch_votes(year=year)

    # Also pull the 33rd Dáil (2020-02-08 → 2024-11-28) unless we're already
    # fetching it (i.e. year == 2024 means 33rd Dáil is the primary)
    if year != 2024:
        votes_33rd = fetch_votes(year=2024)
        all_votes = votes_current + votes_33rd
        print(f"  Votes: {len(votes_current)} ({DAIL_LABELS.get(year,'current')}) "
              f"+ {len(votes_33rd)} (33rd Dáil) = {len(all_votes)} total")
    else:
        all_votes = votes_current
        print(f"  Votes: {len(all_votes)} total")

    # Build lookup structures
    name_index = build_name_index(members)
    vote_index = build_member_vote_index(all_votes)

    print(f"  API: {len(members)} members, {len(all_votes)} votes")

    reports = []
    unmatched = []

    for td in td_interests:
        register_name = td["name"]
        interests = td["interests"]

        # Skip TDs with no declared interests
        if not any(v.strip() for v in interests.values()):
            continue

        # Tag interests → sectors
        interest_sector_map = tag_interests(interests)
        if not interest_sector_map:
            continue  # no recognisable sectors in their interests

        interest_sectors = set(interest_sector_map.keys())

        # Match to API member record
        member = _match_member(register_name, name_index)
        if not member:
            unmatched.append(register_name)
            # Still record interest sectors even without API data
            reports.append({
                "name": register_name,
                "memberCode": None,
                "party": None,
                "matched_to_api": False,
                "interest_sectors": sorted(interest_sectors),
                "committee_sectors": [],
                "committee_conflicts": [],
                "vote_conflicts": [],
                "interests_summary": {
                    s: _interest_evidence_for_sector(interests, s)
                    for s in interest_sectors
                },
            })
            continue

        member_code = member["memberCode"]
        party = member["party"]

        # Tag committees → sectors
        committee_sector_map = {}  # sector → [committee names]
        for c in member["committees"]:
            c_sectors = tag_committee(c["committeeName"])
            for s in c_sectors:
                committee_sector_map.setdefault(s, []).append(c["committeeName"])

        committee_sectors = set(committee_sector_map.keys())

        # Tag votes → conflicts
        member_votes = vote_index.get(member_code, [])
        vote_sector_map = {}  # sector → [vote records]
        for v in member_votes:
            v_sectors = tag_vote(v["debate_title"])
            for s in v_sectors:
                vote_sector_map.setdefault(s, []).append(v)

        vote_sectors = set(vote_sector_map.keys())

        # Find overlaps
        committee_conflict_sectors = interest_sectors & committee_sectors
        vote_conflict_sectors = interest_sectors & vote_sectors

        committee_conflicts = []
        for sector in sorted(committee_conflict_sectors):
            committee_conflicts.append({
                "sector": sector,
                "interest_evidence": _interest_evidence_for_sector(interests, sector),
                "committees": committee_sector_map[sector],
            })

        vote_conflicts = []
        for sector in sorted(vote_conflict_sectors):
            # Sort newest-first; keep up to 25 per sector across all Dáils
            all_sector_votes = sorted(
                vote_sector_map[sector],
                key=lambda v: v["datetime"],
                reverse=True,
            )
            total_votes = len(all_sector_votes)

            # Filter to votes that are ALIGNED with the TD's financial interest:
            #   restrict bill → Níl = aligned (opposing a burden on their sector)
            #   pro-sector bill → Tá = aligned (backing a benefit to their sector)
            aligned_votes = []
            for v in all_sector_votes:
                restrict = vote_is_restrict(v["debate_title"], sector)
                aligned = (v["voted"] == "Níl") if restrict else (v["voted"] == "Tá")
                if aligned:
                    aligned_votes.append(v)

            if not aligned_votes:
                continue  # no aligned votes for this sector — skip entirely

            relevant_votes = aligned_votes[:25]
            vote_conflicts.append({
                "sector": sector,
                "interest_evidence": _interest_evidence_for_sector(interests, sector),
                "total_votes": total_votes,
                "aligned_votes": len(aligned_votes),
                "votes": [
                    {
                        "date": v["datetime"][:10],
                        "title": v["debate_title"],
                        "voted": v["voted"],
                        "outcome": v["outcome"],
                        "dail": v.get("dail", ""),
                    }
                    for v in relevant_votes
                ],
            })

        reports.append({
            "name": register_name,
            "memberCode": member_code,
            "party": party,
            "matched_to_api": True,
            "interest_sectors": sorted(interest_sectors),
            "committee_sectors": sorted(committee_sectors),
            "vote_sectors": sorted(vote_sectors),
            "committee_conflicts": committee_conflicts,
            "vote_conflicts": vote_conflicts,
            "interests_summary": {
                s: _interest_evidence_for_sector(interests, s)
                for s in interest_sectors
            },
        })

    if unmatched:
        print(f"  Warning: {len(unmatched)} TDs not matched to API: {unmatched[:5]}...")

    return reports


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

def summarise_radar(reports: list) -> str:
    total = len(reports)
    matched = sum(1 for r in reports if r["matched_to_api"])
    def _overlap_sectors(r):
        c = {c["sector"] for c in r["committee_conflicts"]}
        v = {v["sector"] for v in r["vote_conflicts"]}
        return c & v

    with_committee = sum(1 for r in reports if r["committee_conflicts"])
    with_votes = sum(1 for r in reports if r["vote_conflicts"])
    # "Both" = same sector appears in committee conflict AND vote conflict
    with_both = sum(1 for r in reports if _overlap_sectors(r))

    lines = [
        "=" * 70,
        "  CONFLICT RADAR — SUMMARY",
        "=" * 70,
        f"  TDs with declared interests:    {total}",
        f"  Matched to Oireachtas API:      {matched}",
        f"  Committee conflicts:            {with_committee}",
        f"  Voting conflicts:               {with_votes}",
        f"  Both committee + vote conflict: {with_both}",
        "",
        "  TOP FLAGS (committee + vote conflict):",
        "-" * 70,
    ]

    # Only include TDs where at least one sector overlaps between committee and vote conflicts
    flagged = [r for r in reports if _overlap_sectors(r)]
    flagged.sort(
        key=lambda r: len(_overlap_sectors(r)) * 10 + len(r["committee_conflicts"]) + len(r["vote_conflicts"]),
        reverse=True,
    )

    for r in flagged[:20]:
        c_sectors = [c["sector"] for c in r["committee_conflicts"]]
        v_sectors = [v["sector"] for v in r["vote_conflicts"]]
        overlap = sorted(set(c_sectors) & set(v_sectors))
        name_short = r["name"][:45]
        party = f"({r['party']})" if r["party"] else ""
        lines.append(f"  {name_short:<45} {party:<20} sectors: {', '.join(overlap)}")
        for conflict in r["committee_conflicts"]:
            s = conflict["sector"]
            if s in overlap:
                for committee in conflict["committees"]:
                    lines.append(f"    Committee: {committee}")
        for conflict in r["vote_conflicts"]:
            s = conflict["sector"]
            if s in overlap:
                lines.append(f"    Votes on {s}: {len(conflict['votes'])} divisions")
                for v in conflict["votes"][:3]:
                    lines.append(f"      {v['date']}  {v['voted']}  {v['title'][:70]}")
        lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)
