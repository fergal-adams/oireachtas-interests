"""
enrichment/lobbying.py
======================
Fetches lobbying contact data from lobbyieng.com for each TD and enriches
the TD records with:
  - Total lobbying contact count
  - Sector-tagged lobbying (where lobbyist topic matches declared interest sectors)
  - Top recent lobbyists (up to 50 most recent returns)

Data source: https://lobbyieng.com  (JSON API, no key required)
Caches to: conflict_radar/cache/lobbying_officials.json  (name→slug mapping)
           conflict_radar/cache/lobbying_{norm_slug}.json (per-TD returns)
"""

import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from unidecode import unidecode
except ImportError:
    def unidecode(s):
        return s

from conflict_radar.sector_tags import SECTORS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = "https://lobbyieng.com/api"
_PAGE_SIZE = 10          # API hard limit
_MAX_PAGES = 5           # fetch up to 50 most recent records per TD
_CACHE_DIR = Path(__file__).parent.parent / "conflict_radar" / "cache"
_SESSION = requests.Session()
_SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Oireachtas Interests Tracker)",
})


# ---------------------------------------------------------------------------
# Sector tagging for lobbying text
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return text.lower()


def _kw_match(text: str, keywords: list) -> bool:
    t = _norm(text)
    for kw in keywords:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, t):
            return True
    return False


def tag_lobbying(text: str) -> set:
    """
    Return set of sector labels matching a lobbying return's text
    (specific_details + intended_results).
    Uses the same sector keyword lists as vote tagging.
    """
    matched = set()
    for sector, cfg in SECTORS.items():
        # Use 'vote' keywords as proxy — they're topic-based like lobbying text
        if _kw_match(text, cfg.get("vote", [])) or _kw_match(text, cfg.get("interest", [])):
            matched.add(sector)
    return matched


# ---------------------------------------------------------------------------
# Name normalisation for matching
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    """Normalise a full name for fuzzy matching: unidecode, lowercase, alpha only."""
    return re.sub(r"[^a-z]", "", unidecode(name.lower()))


def _register_name_to_full(register_name: str) -> str:
    """
    Convert 'SURNAME, Firstname (Constituency)' → 'Firstname Surname'.
    Used to match against lobbyieng's 'Firstname Surname' format.
    """
    name = re.sub(r"\s*\([^)]+\)\s*$", "", register_name).strip()
    parts = name.split(",", 1)
    if len(parts) == 2:
        return (parts[1].strip() + " " + parts[0].strip().title()).strip()
    return name.title()


# ---------------------------------------------------------------------------
# Officials index (name → lobbyieng slug)
# ---------------------------------------------------------------------------

def _load_officials_index(refresh: bool = False) -> dict:
    """
    Fetch the full officials list from lobbyieng.com and build a
    {norm_name: slug} mapping.  Cached to lobbying_officials.json.
    """
    cache_path = _CACHE_DIR / "lobbying_officials.json"
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    print("  Fetching lobbyieng.com officials index...")
    resp = _SESSION.get(f"{_BASE}/officials", timeout=20)
    resp.raise_for_status()
    officials = resp.json()

    # Build {norm_name → slug} — keep TDs only (filter by job_title)
    # We include all titles in case some are labelled Minister, etc.
    mapping = {}
    for o in officials:
        key = _norm_name(o["name"])
        if key:
            # If slug already seen, prefer the one with a TD title
            existing = mapping.get(key)
            if existing is None or "td" in o.get("job_title", "").lower():
                mapping[key] = o["slug"]

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Cached {len(mapping)} officials")
    return mapping


def _find_lobby_slug(register_name: str, officials_index: dict) -> str | None:
    """
    Given a TD's register name, return their lobbyieng.com slug, or None.
    """
    full = _register_name_to_full(register_name)
    key = _norm_name(full)
    return officials_index.get(key)


# ---------------------------------------------------------------------------
# Per-TD lobbying fetch
# ---------------------------------------------------------------------------

def _fetch_td_lobbying(lobby_slug: str, max_pages: int = _MAX_PAGES) -> dict:
    """
    Fetch up to max_pages × PAGE_SIZE recent lobbying returns for a TD.
    Returns:
      {
        "total": int,
        "fetched": int,
        "records": [...],
      }
    Cached to lobbying_{norm_slug}.json.
    """
    safe_slug = re.sub(r"[^a-z0-9\-]", "_", lobby_slug.lower())
    cache_path = _CACHE_DIR / f"lobbying_{safe_slug}.json"

    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    all_records = []
    total = None

    for page in range(1, max_pages + 1):
        try:
            resp = _SESSION.get(
                f"{_BASE}/officials/{lobby_slug}",
                params={"page": page, "pageSize": _PAGE_SIZE},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"    Warning: lobbying fetch failed for {lobby_slug} page {page}: {e}")
            break

        data = resp.json()
        if total is None:
            total = data.get("total", 0)

        records = data.get("records", [])
        if not records:
            break
        all_records.extend(records)
        time.sleep(0.15)   # polite rate limiting

    result = {
        "total": total or len(all_records),
        "fetched": len(all_records),
        "records": all_records,
    }
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------

def enrich_lobbying(records: list) -> None:
    """
    Enrich TD records in-place with lobbying contact data.
    Adds a 'lobbying' dict to each record:
      {
        "total":          int,   # total lobbying contacts on record
        "lobby_slug":     str,   # lobbyieng.com slug for this TD
        "sector_contacts": {     # sector → list of relevant recent contacts
            "property": [{"lobbyist", "date", "details", "sector"}, ...],
            ...
        },
        "top_lobbyists":  [{"name": ..., "count": ...}, ...],  # top 5 by frequency
        "recent":         [{"lobbyist", "date", "details"}, ...],  # 5 most recent
      }
    or 'lobbying': None if no match found.
    """
    officials_index = _load_officials_index()

    matched = 0
    for td in records:
        lobby_slug = _find_lobby_slug(td["name"], officials_index)
        if not lobby_slug:
            td["lobbying"] = None
            continue

        data = _fetch_td_lobbying(lobby_slug)
        all_records = data.get("records", [])
        total = data.get("total", 0)

        interest_sectors = set(td.get("interest_sectors", []))

        # ── Sector-tagged contacts ───────────────────────────────────────────
        sector_contacts: dict = {}
        lobbyist_counts: dict = {}

        for r in all_records:
            text = " ".join([
                r.get("specific_details") or "",
                " :: ".join(r.get("intended_results") or [])
                if isinstance(r.get("intended_results"), list)
                else (r.get("intended_results") or ""),
            ])

            # Tag this return with sectors
            sectors = tag_lobbying(text)

            # Only surface sector contacts that overlap with TD's declared interests
            for sector in sectors & interest_sectors:
                sector_contacts.setdefault(sector, []).append({
                    "lobbyist": r.get("lobbyist_name", ""),
                    "date":     (r.get("date_published") or "")[:10],
                    "details":  (r.get("specific_details") or "")[:200],
                })

            # Count lobbyist frequency
            name = r.get("lobbyist_name", "")
            if name:
                lobbyist_counts[name] = lobbyist_counts.get(name, 0) + 1

        # ── Top lobbyists (by frequency in fetched sample) ──────────────────
        top_lobbyists = sorted(
            [{"name": n, "count": c} for n, c in lobbyist_counts.items()],
            key=lambda x: -x["count"],
        )[:5]

        # ── Most recent 5 records ────────────────────────────────────────────
        recent = [
            {
                "lobbyist": r.get("lobbyist_name", ""),
                "date":     (r.get("date_published") or "")[:10],
                "details":  (r.get("specific_details") or "")[:200],
                "url":      r.get("url", ""),
            }
            for r in all_records[:5]
        ]

        td["lobbying"] = {
            "total":           total,
            "lobby_slug":      lobby_slug,
            "sector_contacts": sector_contacts,
            "top_lobbyists":   top_lobbyists,
            "recent":          recent,
        }
        matched += 1

    unmatched = sum(1 for td in records if td.get("lobbying") is None)
    print(f"  Lobbying: matched {matched} TDs, {unmatched} unmatched")
