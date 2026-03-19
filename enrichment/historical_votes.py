"""
enrichment/historical_votes.py
================================
Extends vote conflict data with 33rd Dáil (2020–2024) voting records.

For each TD record in the merged dataset:
  1. Stamps existing 34th Dáil vote entries with dail="34th Dáil"
  2. Fetches 33rd Dáil votes (cached) for the TD's memberCode
  3. Tags those votes with sectors using tag_vote()
  4. Merges any overlapping sectors into the vote_conflicts list
  5. Updates total_votes counts to reflect both Dáils

Operating on in-memory records — does not modify radar_output JSON on disk.
"""

import sys
from pathlib import Path

# Allow imports from parent directory when run standalone
sys.path.insert(0, str(Path(__file__).parent.parent))

from conflict_radar.oireachtas_api import fetch_votes, build_member_vote_index
from conflict_radar.sector_tags import tag_vote


def enrich_historical_votes(records: list) -> None:
    """
    Enrich TD records in-place with 33rd Dáil vote conflicts.
    Modifies records[*]["vote_conflicts"] in-place.
    Also stamps existing (34th Dáil) vote entries with dail="34th Dáil".
    """
    print("  Loading 33rd Dáil votes (2020–2024)...")
    votes_33rd = fetch_votes(year=2024)
    print(f"  {len(votes_33rd)} 33rd Dáil votes loaded")

    vote_index = build_member_vote_index(votes_33rd)

    enriched_count = 0
    new_sectors_count = 0

    for td in records:
        member_code = td.get("memberCode")
        if not member_code:
            continue

        interest_sectors = set(td.get("interest_sectors", []))
        if not interest_sectors:
            continue

        # ── Step 1: stamp existing 34th Dáil vote entries ──────────────────
        for vc in td.get("vote_conflicts", []):
            for v in vc.get("votes", []):
                if not v.get("dail"):
                    v["dail"] = "34th Dáil"
            # Also ensure total_votes is set (may be missing from pre-enrichment output)
            if "total_votes" not in vc:
                vc["total_votes"] = len(vc.get("votes", []))

        # ── Step 2: find 33rd Dáil votes for this member ───────────────────
        member_votes_33rd = vote_index.get(member_code, [])
        if not member_votes_33rd:
            continue

        # ── Step 3: tag 33rd Dáil votes with sectors ───────────────────────
        vote_sector_map: dict = {}  # sector → [vote records]
        for v in member_votes_33rd:
            v_sectors = tag_vote(v["debate_title"])
            for s in v_sectors:
                vote_sector_map.setdefault(s, []).append(v)

        # ── Step 4: find overlaps with declared interest sectors ────────────
        overlap_sectors = interest_sectors & set(vote_sector_map.keys())
        if not overlap_sectors:
            continue

        # Build a lookup for existing vote_conflicts by sector
        existing_by_sector = {vc["sector"]: vc for vc in td.get("vote_conflicts", [])}
        # Ensure vote_conflicts list exists
        if "vote_conflicts" not in td:
            td["vote_conflicts"] = []

        added = False
        for sector in sorted(overlap_sectors):
            new_votes_for_sector = vote_sector_map[sector]

            if sector in existing_by_sector:
                # ── Merge: add 33rd Dáil votes to existing sector entry ────
                vc = existing_by_sector[sector]
                existing_votes = list(vc.get("votes", []))
                existing_total = vc.get("total_votes", len(existing_votes))

                new_formatted = [
                    {
                        "date":    v["datetime"][:10],
                        "title":   v["debate_title"],
                        "voted":   v["voted"],
                        "outcome": v["outcome"],
                        "dail":    v.get("dail", "33rd Dáil"),
                    }
                    for v in sorted(
                        new_votes_for_sector,
                        key=lambda v: v["datetime"],
                        reverse=True,
                    )
                ]

                # Combine and sort newest-first; keep up to 25 (balanced across Dáils)
                combined = existing_votes + new_formatted
                combined.sort(key=lambda v: v.get("date", ""), reverse=True)
                vc["votes"] = combined[:25]
                vc["total_votes"] = existing_total + len(new_votes_for_sector)
                added = True

            else:
                # ── New sector: only from 33rd Dáil data ──────────────────
                # Get interest_evidence from interests_summary
                interest_evidence = td.get("interests_summary", {}).get(sector, "")

                all_sector_votes = sorted(
                    new_votes_for_sector,
                    key=lambda v: v["datetime"],
                    reverse=True,
                )
                relevant = all_sector_votes[:25]

                new_vc = {
                    "sector":           sector,
                    "interest_evidence": interest_evidence,
                    "total_votes":      len(all_sector_votes),
                    "votes": [
                        {
                            "date":    v["datetime"][:10],
                            "title":   v["debate_title"],
                            "voted":   v["voted"],
                            "outcome": v["outcome"],
                            "dail":    v.get("dail", "33rd Dáil"),
                        }
                        for v in relevant
                    ],
                }
                td["vote_conflicts"].append(new_vc)
                new_sectors_count += 1
                added = True

        if added:
            # Keep vote_conflicts sorted by sector name for consistency
            td["vote_conflicts"].sort(key=lambda vc: vc["sector"])
            enriched_count += 1

    print(
        f"  Historical votes: {enriched_count} TDs enriched "
        f"(+{new_sectors_count} new sector entries from 33rd Dáil)"
    )
