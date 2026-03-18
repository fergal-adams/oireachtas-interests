"""
build.py — Oireachtas Interests Tracker static site generator
=============================================================

Reads:
  radar_output_{year}.json                    — conflict data (interests, committees, votes)
  cro_output_{year}.json                      — CRO directorship data
  conflict_radar/cache/pension_{year}.json    — pension estimates (from pension_calculator.py)
  conflict_radar/cache/dail_34_pids.json      — pId ↔ fullName mapping for current Dáil

Generates:
  site/                      — static HTML, deploy this directory

Usage:
    python build.py [--year 2025] [--output site]
"""

import argparse
import json
import os
import re
import shutil
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    from unidecode import unidecode
except ImportError:
    def unidecode(s):
        return s

try:
    from enrichment.property_valuations import value_property_interests, summarise_valuation
    _PROPERTY_ENRICHMENT = True
except ImportError:
    _PROPERTY_ENRICHMENT = False

try:
    from enrichment.cro_enrichment import enrich_cro_records
    _CRO_ENRICHMENT = True
except ImportError:
    _CRO_ENRICHMENT = False

try:
    from enrichment.bill_interpreter import enrich_vote_conflicts
    _BILL_ENRICHMENT = True
except ImportError:
    _BILL_ENRICHMENT = False


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def slugify(register_name: str) -> str:
    """'AIRD, William (Laois)' → 'william-aird'"""
    # Remove constituency in parens
    name = re.sub(r"\s*\([^)]+\)\s*$", "", register_name).strip()
    # Split at comma: "AIRD, William" → firstname-first order
    parts = name.split(",", 1)
    if len(parts) == 2:
        surname = parts[0].strip()
        firstname = parts[1].strip()
        ordered = f"{firstname} {surname}"
    else:
        ordered = name
    # Transliterate accents, lowercase, replace non-alnum with hyphen
    ordered = unidecode(ordered)
    slug = re.sub(r"[^a-z0-9]+", "-", ordered.lower()).strip("-")
    return slug


def display_name(register_name: str) -> str:
    """'AIRD, William (Laois)' → 'William Aird'"""
    name = re.sub(r"\s*\([^)]+\)\s*$", "", register_name).strip()
    parts = name.split(",", 1)
    if len(parts) == 2:
        surname = parts[0].strip().title()
        firstname = parts[1].strip()
        return f"{firstname} {surname}"
    return name.title()


def constituency(register_name: str) -> str:
    """'AIRD, William (Laois)' → 'Laois'"""
    m = re.search(r"\(([^)]+)\)", register_name)
    return m.group(1) if m else ""


def norm_name(register_name: str) -> str:
    """Normalise for matching — strips constituency, lowercases, strips punctuation."""
    name = re.sub(r"\s*\([^)]+\)\s*$", "", register_name).strip()
    name = unidecode(name.lower())
    name = re.sub(r"[^a-z\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


# ---------------------------------------------------------------------------
# Interest helpers
# ---------------------------------------------------------------------------

CATEGORY_LABELS = {
    "occupations":           "Occupations",
    "shares":                "Shares",
    "directorships":         "Directorships",
    "land_property":         "Land and Property",
    "gifts":                 "Gifts",
    "property_supplied":     "Property or Services Supplied",
    "travel":                "Travel Facilities",
    "remunerated_positions": "Remunerated Positions",
    "contracts":             "Contracts",
    "other_information":     "Other Information",
}


def extract_categories(interests_summary: dict) -> dict:
    """
    Reconstruct per-category interests from the sector-grouped interests_summary.
    interests_summary format: {"sector": "[cat] text | [cat] text | ..."}
    Returns: {"occupations": "text...", "shares": "text...", ...}
    Note: only shows interests that are sector-tagged (i.e. matched a keyword).
    """
    cats: dict[str, set] = {}
    for _sector, evidence in (interests_summary or {}).items():
        for segment in evidence.split(" | "):
            segment = segment.strip()
            m = re.match(r"^\[([^\]]+)\]\s+(.*)", segment)
            if m:
                cat = m.group(1).strip()
                text = m.group(2).strip()
                if text:
                    cats.setdefault(cat, set()).add(text)
    # Deduplicate and join; preserve category order
    result = {}
    for cat in CATEGORY_LABELS:
        if cat in cats:
            result[cat] = "; ".join(sorted(cats[cat]))
    # Any unexpected category keys
    for cat, texts in cats.items():
        if cat not in result:
            result[cat] = "; ".join(sorted(texts))
    return result


# ---------------------------------------------------------------------------
# Pension data helpers
# ---------------------------------------------------------------------------

def _norm_for_name_lookup(s: str) -> str:
    """Normalise a full name for fuzzy matching: unidecode, lowercase, alpha only."""
    return re.sub(r"[^a-z]", "", unidecode(s.lower()))


def load_pension_data(year: int, base_dir: Path) -> tuple[dict, dict]:
    """
    Load pension estimates and return:
      pension_by_pid   : {pId: pension_record}
      name_to_pid      : {norm_fullname: pId}  — built from dail_34_pids cache
    """
    cache_dir = base_dir / "conflict_radar" / "cache"
    pension_path = cache_dir / f"pension_{year}.json"
    dail34_path  = cache_dir / "dail_34_pids.json"

    pension_by_pid: dict = {}
    if pension_path.exists():
        pension_by_pid = json.loads(pension_path.read_text(encoding="utf-8"))

    name_to_pid: dict = {}
    if dail34_path.exists():
        dail34 = json.loads(dail34_path.read_text(encoding="utf-8"))
        for pid, rec in dail34.items():
            key = _norm_for_name_lookup(rec.get("fullName", ""))
            if key:
                name_to_pid[key] = pid

    return pension_by_pid, name_to_pid


def _pid_from_member_code(member_code: str) -> str:
    """Derive pId from memberCode: 'Ciarán-Ahern.D.2024-11-29' → 'CiaranAhern'."""
    name_part = member_code.split(".")[0]
    name_part = unidecode(name_part)
    return re.sub(r"[^a-zA-Z0-9]", "", name_part)


def find_pension(td_record: dict, pension_by_pid: dict, name_to_pid: dict) -> dict | None:
    """
    Match a TD record to its pension data.
    Two-stage: (1) derive pId from memberCode; (2) fallback to full-name lookup.
    Returns the pension dict or None.
    """
    # Stage 1: derive from memberCode
    mc = td_record.get("memberCode", "")
    if mc:
        pid = _pid_from_member_code(mc)
        if pid in pension_by_pid:
            return pension_by_pid[pid]

    # Stage 2: normalised full-name lookup via dail_34 cache
    dname = td_record.get("name", "")
    name_no_const = re.sub(r"\s*\([^)]+\)", "", dname).strip()
    parts = name_no_const.split(",", 1)
    if len(parts) == 2:
        full = (parts[1].strip() + " " + parts[0].strip()).strip()
    else:
        full = name_no_const
    key = _norm_for_name_lookup(full)
    pid = name_to_pid.get(key)
    if pid and pid in pension_by_pid:
        return pension_by_pid[pid]

    return None


# ---------------------------------------------------------------------------
# Data loading and merging
# ---------------------------------------------------------------------------

def load_data(
    year: int,
    base_dir: Path,
    pension_by_pid: dict | None = None,
    name_to_pid: dict | None = None,
) -> list[dict]:
    """Load and merge radar + CRO + pension outputs into one list of enriched TD records."""
    radar_path = base_dir / f"radar_output_{year}.json"
    cro_path = base_dir / f"cro_output_{year}.json"

    with open(radar_path, encoding="utf-8") as f:
        radar_records = json.load(f)

    cro_by_name: dict[str, dict] = {}
    if cro_path.exists():
        with open(cro_path, encoding="utf-8") as f:
            for r in json.load(f):
                cro_by_name[norm_name(r["name"])] = r

    merged = []
    for td in radar_records:
        key = norm_name(td["name"])
        cro = cro_by_name.get(key, {
            "declared_directorships": [],
            "undeclared_cro_directorships": [],
            "flags": [],
        })

        slug = slugify(td["name"])
        dname = display_name(td["name"])
        const = constituency(td["name"])
        categories = extract_categories(td.get("interests_summary", {}))

        # Conflict counts
        n_committee = len(td.get("committee_conflicts", []))
        n_vote = len(td.get("vote_conflicts", []))
        has_conflicts = n_committee > 0 or n_vote > 0

        # Pension data (if available)
        pension = None
        if pension_by_pid is not None and name_to_pid is not None:
            pension = find_pension(td, pension_by_pid, name_to_pid)

        # Property valuations (if available)
        property_valuation = None
        if _PROPERTY_ENRICHMENT:
            land_texts = []
            for _sector, evidence in (td.get("interests_summary") or {}).items():
                for segment in evidence.split(" | "):
                    if "[land_property]" in segment:
                        import re as _re
                        text = _re.sub(r"^\[land_property\]\s*", "", segment.strip())
                        if text:
                            land_texts.append(text)
            if land_texts:
                all_props = []
                for t in land_texts:
                    all_props.extend(value_property_interests(t))
                if all_props:
                    property_valuation = summarise_valuation(all_props)

        merged.append({
            # Identity
            "name":            td["name"],
            "display_name":    dname,
            "slug":            slug,
            "party":           td.get("party", ""),
            "constituency":    const,
            "memberCode":      td.get("memberCode", ""),
            "matched_to_api":  td.get("matched_to_api", False),
            # Interests (sector-tagged)
            "interest_sectors":    td.get("interest_sectors", []),
            "interests_by_cat":    categories,      # reconstructed per-category
            "interests_summary":   td.get("interests_summary", {}),
            # Conflicts
            "committee_conflicts": td.get("committee_conflicts", []),
            "vote_conflicts":      td.get("vote_conflicts", []),
            "has_conflicts":       has_conflicts,
            "conflict_count":      n_committee + n_vote,
            # CRO
            "cro": cro,
            # Pension
            "pension": pension,
            # Property valuation
            "property_valuation": property_valuation,
        })

    # Enrich vote conflict entries with bill parsing + alignment interpretation
    if _BILL_ENRICHMENT:
        enrich_vote_conflicts(merged)

    return merged


def slim_records(records: list[dict]) -> list[dict]:
    """Trim to fields needed for client-side JS (data.json)."""
    out = []
    for td in records:
        out.append({
            "name":             td["name"],
            "display_name":     td["display_name"],
            "slug":             td["slug"],
            "party":            td["party"],
            "constituency":     td["constituency"],
            "interest_sectors": td["interest_sectors"],
            "has_conflicts":    td["has_conflicts"],
            "conflict_count":   td["conflict_count"],
            "cro_flags":        td["cro"].get("flags", []),
            # Short interests text for search
            "interests_text":   " | ".join(
                f"[{cat}] {text}"
                for cat, text in td["interests_by_cat"].items()
            ),
        })
    return out


# ---------------------------------------------------------------------------
# Aggregate stats for home page / charts
# ---------------------------------------------------------------------------

def compute_stats(records: list[dict]) -> dict:
    from collections import Counter

    total = len(records)
    flagged = sum(1 for r in records if r["has_conflicts"])
    cro_total = sum(
        len(r["cro"].get("declared_directorships", []))
        for r in records
    )

    # Sectors with interests
    sector_counts: Counter = Counter()
    conflict_sector_counts: Counter = Counter()
    for r in records:
        for s in r["interest_sectors"]:
            sector_counts[s] += 1
        for c in r["committee_conflicts"]:
            conflict_sector_counts[c["sector"]] += 1
        for c in r["vote_conflicts"]:
            conflict_sector_counts[c["sector"]] += 1

    return {
        "total_tds":     total,
        "flagged_tds":   flagged,
        "cro_companies": cro_total,
        "data_year":     2025,
        "data_date":     "February 2026",
        "sector_counts":          dict(sector_counts.most_common()),
        "conflict_sector_counts": dict(conflict_sector_counts.most_common()),
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def load_cro_records(year: int, base_dir: Path, party_map: dict | None = None) -> list[dict]:
    """Load the full CRO output and enrich with slug / display_name for templates.
    party_map: optional dict {norm_name → party} from radar data to fill in party info.
    """
    cro_path = base_dir / f"cro_output_{year}.json"
    if not cro_path.exists():
        return []
    with open(cro_path, encoding="utf-8") as f:
        raw = json.load(f)
    enriched = []
    for r in raw:
        if not r.get("declared_directorships"):
            continue
        key = norm_name(r["name"])
        party = r.get("party") or (party_map.get(key, "") if party_map else "")
        enriched.append({
            **r,
            "slug":         slugify(r["name"]),
            "display_name": display_name(r["name"]),
            "party":        party,
            "constituency": constituency(r["name"]),
        })
    enriched.sort(key=lambda r: r["name"])
    return enriched


def build(year: int = 2025, output_dir: str = "site"):
    base = HERE
    out = base / output_dir

    # Load data
    print(f"Loading data for year {year}...")
    pension_by_pid, name_to_pid = load_pension_data(year, base)
    print(f"  {len(pension_by_pid)} pension records loaded")
    records = load_data(year, base, pension_by_pid=pension_by_pid, name_to_pid=name_to_pid)
    pension_found = sum(1 for r in records if r.get("pension"))
    print(f"  {len(records)} TD records loaded ({pension_found} with pension data)")

    # Load full CRO records (all 58 TDs with directorships, not just those in radar)
    # Build party map from radar records so CRO TDs get party labels
    party_map = {norm_name(r["name"]): r["party"] for r in records}
    cro_records = load_cro_records(year, base, party_map=party_map)
    if _CRO_ENRICHMENT:
        enrich_cro_records(cro_records)
    print(f"  {len(cro_records)} CRO directorship records loaded")

    # Sort by name for consistent output
    records.sort(key=lambda r: r["name"])

    stats = compute_stats(records)
    # Override cro_companies to use full CRO dataset
    stats["cro_companies"] = sum(
        len(r.get("declared_directorships", [])) for r in cro_records
    )

    top_flagged = sorted(
        [r for r in records if r["has_conflicts"]],
        key=lambda r: -r["conflict_count"],
    )[:10]

    # Jinja2 environment
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["year"] = year

    # ── Custom filters ────────────────────────────────────────────────────────
    _CAT_LABELS = {
        "occupations":           "Occupations",
        "shares":                "Shares",
        "directorships":         "Directorships",
        "land_property":         "Land & Property",
        "gifts":                 "Gifts",
        "property_supplied":     "Property / Services",
        "travel":                "Travel",
        "remunerated_positions": "Remunerated Positions",
        "contracts":             "Contracts",
        "other_information":     "Other",
    }

    def parse_interest(text: str) -> list:
        """Split pipe-separated '[category] text' evidence into a list of dicts.
        e.g. '[occupations] Farmer | [land_property] Farmland, Co. Kerry'
        → [{'label': 'Occupations', 'text': 'Farmer'},
           {'label': 'Land & Property', 'text': 'Farmland, Co. Kerry'}]
        """
        result = []
        for seg in (s.strip() for s in text.split(" | ") if s.strip()):
            m = re.match(r"^\[(\w+)\]\s*(.*)", seg, re.DOTALL)
            if m:
                cat = m.group(1)
                txt = m.group(2).strip()
            else:
                cat = ""
                txt = seg
            result.append({
                "cat":   cat,
                "label": _CAT_LABELS.get(cat, cat.replace("_", " ").title() if cat else "Interest"),
                "text":  txt,
            })
        return result

    env.filters["parse_interest"] = parse_interest

    def render(template_name: str, context: dict, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmpl = env.get_template(template_name)
        dest.write_text(tmpl.render(**context), encoding="utf-8")

    # ---------- TD profile pages ----------
    print(f"Generating TD profile pages...")
    for td in records:
        render(
            "td_profile.html",
            {"td": td, "stats": stats},
            out / "tds" / td["slug"] / "index.html",
        )
    print(f"  {len(records)} profiles written")

    # ---------- Index pages ----------
    render("index.html",    {"stats": stats, "top_flagged": top_flagged, "records": records}, out / "index.html")
    render("explorer.html", {"stats": stats, "records": records},                             out / "explorer" / "index.html")
    render("radar.html",    {"stats": stats, "records": records},                             out / "radar" / "index.html")
    render("cro.html",      {"stats": stats, "cro_records": cro_records},                     out / "cro" / "index.html")
    render("about.html",    {"stats": stats},                                                 out / "about" / "index.html")
    print("  Index pages written")

    # ---------- data.json ----------
    slim = slim_records(records)
    data_json = out / "static" / "data.json"
    data_json.parent.mkdir(parents=True, exist_ok=True)
    data_json.write_text(json.dumps(slim, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  data.json written ({len(slim)} records, {data_json.stat().st_size // 1024} KB)")

    # ---------- Static assets ----------
    if STATIC_DIR.exists():
        static_out = out / "static"
        for src in STATIC_DIR.iterdir():
            if src.is_file():
                shutil.copy2(src, static_out / src.name)
        print(f"  Static assets copied")

    print(f"\nBuild complete → {out}/")
    print(f"  Preview: python -m http.server 8000 --directory {out}/")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build the Oireachtas Interests Tracker static site")
    parser.add_argument("--year",   type=int, default=2025, help="Register year (default: 2025)")
    parser.add_argument("--output", default="site",         help="Output directory (default: site)")
    args = parser.parse_args()
    build(year=args.year, output_dir=args.output)


if __name__ == "__main__":
    main()
