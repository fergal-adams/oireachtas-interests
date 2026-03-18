"""
CLI entry point for the Conflict Radar.

Usage:
    python -m conflict_radar.run_radar --interests /tmp/register_2025_clean.json --year 2025
    python -m conflict_radar.run_radar --interests /tmp/register_2025_clean.json --year 2025 --td "AIRD, William"
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from conflict_radar.radar import run_radar, summarise_radar


def main():
    parser = argparse.ArgumentParser(description="Oireachtas Conflict Radar")
    parser.add_argument(
        "--interests",
        required=True,
        help="Path to extracted interests JSON (from extract_interests.py)",
    )
    parser.add_argument("--year", type=int, default=2025, help="Register year (default: 2025)")
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default: radar_output_{year}.json in project dir)",
    )
    parser.add_argument(
        "--td",
        default=None,
        help="Show detailed report for one TD (partial name match, e.g. 'AIRD')",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-fetch from API even if cached",
    )
    parser.add_argument(
        "--cro-check",
        action="store_true",
        help="Run CRO directorship cross-check (parse-only without credentials)",
    )
    parser.add_argument(
        "--cro-email",
        default=None,
        help="CRO API email (or set CRO_EMAIL env var)",
    )
    parser.add_argument(
        "--cro-key",
        default=None,
        help="CRO API key (or set CRO_API_KEY env var)",
    )
    args = parser.parse_args()

    # Optionally force refresh
    if args.refresh:
        from conflict_radar import oireachtas_api
        oireachtas_api._force_refresh = True

    print(f"\nRunning conflict radar for {args.year}...\n")
    reports = run_radar(args.interests, year=args.year)

    # Output path
    output_path = args.output or str(
        Path(__file__).parent.parent / f"radar_output_{args.year}.json"
    )
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(reports, f, indent=2, ensure_ascii=False)
    print(f"\n  Saved {len(reports)} reports → {output_path}\n")

    # Print summary
    print(summarise_radar(reports))

    # CRO directorship cross-check
    if args.cro_check:
        from conflict_radar.cro_check import run_cro_check, summarise_cro
        print("\nRunning CRO directorship cross-check...\n")
        cro_email = args.cro_email or os.environ.get("CRO_EMAIL")
        cro_key = args.cro_key or os.environ.get("CRO_API_KEY")
        cro_reports = run_cro_check(
            args.interests,
            api_email=cro_email,
            api_key=cro_key,
            year=args.year,
            force_refresh=args.refresh,
        )
        cro_output = str(Path(__file__).parent.parent / f"cro_output_{args.year}.json")
        with open(cro_output, "w", encoding="utf-8") as f:
            json.dump(cro_reports, f, indent=2, ensure_ascii=False)
        print(f"  Saved {len(cro_reports)} CRO reports → {cro_output}\n")
        print(summarise_cro(cro_reports))

    # Detailed TD view
    if args.td:
        query = args.td.lower()
        matches = [r for r in reports if query in r["name"].lower()]
        if not matches:
            print(f"\nNo TD found matching '{args.td}'")
        else:
            for r in matches:
                print(f"\n{'=' * 70}")
                print(f"  DETAILED REPORT: {r['name']}")
                print(f"  Party: {r['party']}  |  memberCode: {r['memberCode']}")
                print(f"  Interest sectors: {', '.join(r['interest_sectors'])}")
                print(f"{'=' * 70}\n")

                print("  DECLARED INTERESTS (by sector):")
                for sector, evidence in r["interests_summary"].items():
                    print(f"    [{sector}]")
                    print(f"      {evidence[:300]}")

                print()
                if r["committee_conflicts"]:
                    print("  COMMITTEE CONFLICTS:")
                    for c in r["committee_conflicts"]:
                        print(f"    Sector: {c['sector']}")
                        for committee in c["committees"]:
                            print(f"      Member of: {committee}")
                else:
                    print("  No committee conflicts.")

                print()
                if r["vote_conflicts"]:
                    print("  VOTE CONFLICTS:")
                    for vc in r["vote_conflicts"]:
                        print(f"    Sector: {vc['sector']}  ({len(vc['votes'])} relevant votes)")
                        for v in vc["votes"]:
                            print(f"      {v['date']}  [{v['voted']}]  {v['title'][:80]}")
                else:
                    print("  No vote conflicts.")


if __name__ == "__main__":
    main()
