"""
Oireachtas Open Data API client with local JSON caching.

Endpoints used:
  GET /v1/members   — TD profiles with nested committee memberships
  GET /v1/votes     — division records with per-member vote tallies
"""

import json
import os
import time
from pathlib import Path

import requests

BASE_URL = "https://api.oireachtas.ie/v1"
CACHE_DIR = Path(__file__).parent / "cache"

# Map register year → Dáil session start date (for member API filter)
# The register covers TDs who were serving during that calendar year.
DAIL_START_DATES = {
    2025: "2024-11-29",  # 34th Dáil (started after Nov 2024 election)
    2024: "2020-02-08",  # 33rd Dáil
    2023: "2020-02-08",
    2022: "2020-02-08",
    2021: "2020-02-08",
    2020: "2020-02-08",
}


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / name


def _get(url: str, params: dict) -> dict:
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _paginate(url: str, params: dict, count_key: str, batch: int = 200) -> list:
    """Fetch all pages from a paginated endpoint."""
    params = {**params, "limit": batch, "skip": 0}
    first = _get(url, params)
    total = first["head"]["counts"].get(count_key, 0)
    results = first["results"]
    while len(results) < total:
        params["skip"] = len(results)
        page = _get(url, params)
        batch_results = page["results"]
        if not batch_results:
            break
        results.extend(batch_results)
        time.sleep(0.1)
    return results


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def _parse_member(raw: dict) -> dict:
    """Flatten a raw API member record into a simpler shape."""
    m = raw.get("member", raw)
    committees = []
    for ms in m.get("memberships", []):
        membership = ms.get("membership", ms)
        for c in membership.get("committees", []):
            committee = c.get("committee", c)
            names = committee.get("committeeName", [])
            name_en = ""
            if names:
                first = names[0]
                name_en = first.get("nameEn") or first.get("showAs", "")
            if name_en:
                committees.append({
                    "committeeID": committee.get("committeeID"),
                    "committeeName": name_en,
                    "role": committee.get("role", []),
                    "status": committee.get("mainStatus", ""),
                })
    # Deduplicate by committeeID
    seen = set()
    unique_committees = []
    for c in committees:
        cid = c["committeeID"]
        if cid not in seen:
            seen.add(cid)
            unique_committees.append(c)

    parties = []
    for ms in m.get("memberships", []):
        membership = ms.get("membership", ms)
        for p in membership.get("parties", []):
            party = p.get("party", p)
            parties.append(party.get("showAs", ""))
    party = parties[-1] if parties else ""

    return {
        "memberCode": m.get("memberCode", ""),
        "fullName": m.get("fullName", ""),
        "firstName": m.get("firstName", ""),
        "lastName": m.get("lastName", ""),
        "pId": m.get("pId", ""),
        "party": party,
        "committees": unique_committees,
    }


def fetch_members(year: int = 2025, force_refresh: bool = False) -> list:
    """
    Return list of TD member records with committee memberships for the given year.
    Cached to cache/members_{year}.json.
    """
    cache_file = _cache_path(f"members_{year}.json")
    if cache_file.exists() and not force_refresh:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    dail_start = DAIL_START_DATES.get(year, f"{year}-01-01")
    print(f"  Fetching members for {year} from API (Dáil start: {dail_start})...")
    raw_results = _paginate(
        f"{BASE_URL}/members",
        {"chamber": "dail", "date_start": dail_start},
        count_key="memberCount",
    )
    members = [_parse_member(r) for r in raw_results]
    members = [m for m in members if m["memberCode"]]

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(members, f, indent=2, ensure_ascii=False)
    print(f"  Fetched {len(members)} members → cached")
    return members


# ---------------------------------------------------------------------------
# Votes
# ---------------------------------------------------------------------------

def _parse_vote(raw: dict) -> dict:
    """Flatten a raw API vote record."""
    div = raw.get("division", raw)
    debate = div.get("debate", {})
    subject = div.get("subject", {})

    debate_title = debate.get("showAs", "") or subject.get("showAs", "")

    tallies = div.get("tallies", {})

    def member_codes(tally_key: str) -> list:
        tally = tallies.get(tally_key) or {}
        members = tally.get("members", [])
        codes = []
        for m in members:
            member = m.get("member", m)
            code = member.get("memberCode", "")
            if code:
                codes.append(code)
        return codes

    return {
        "voteId": div.get("voteId", ""),
        "datetime": div.get("datetime", ""),
        "debate_title": debate_title.strip(),
        "outcome": div.get("outcome", ""),
        "ta_members": member_codes("taVotes"),    # Tá (yes)
        "nil_members": member_codes("nilVotes"),  # Níl (no)
        "staonn_members": member_codes("staonVotes"),  # abstain
    }


def fetch_votes(year: int = 2025, force_refresh: bool = False) -> list:
    """
    Return ALL divisions from the Dáil term that covers the given register year.
    Uses proper pagination to retrieve every division.
    Cached to cache/votes_{year}.json.

    Date range:
      date_start = Dáil term start (e.g. 2024-11-29 for 34th Dáil)
      date_end   = today (captures all votes available so far)
    """
    import datetime

    cache_file = _cache_path(f"votes_{year}.json")
    if cache_file.exists() and not force_refresh:
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    date_start = DAIL_START_DATES.get(year, f"{year}-01-01")
    date_end = datetime.date.today().isoformat()

    print(f"  Fetching votes {date_start} → {date_end} (paginated)...")

    raw_results = _paginate(
        f"{BASE_URL}/votes",
        params={
            "chamber": "dail",
            "date_start": date_start,
            "date_end": date_end,
        },
        count_key="divisionCount",
        batch=200,
    )

    votes = [_parse_vote(r) for r in raw_results]
    votes = [v for v in votes if v["voteId"]]

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(votes, f, indent=2, ensure_ascii=False)
    print(f"  Fetched {len(votes)} votes → cached")
    return votes


def build_member_vote_index(votes: list) -> dict:
    """
    Invert votes list into a per-member lookup.
    Returns: { memberCode: [ {voteId, datetime, debate_title, outcome, voted} ] }
    where voted is 'Tá', 'Níl', or 'Staonn'.
    """
    index = {}
    for v in votes:
        entry_base = {
            "voteId": v["voteId"],
            "datetime": v["datetime"],
            "debate_title": v["debate_title"],
            "outcome": v["outcome"],
        }
        for code in v["ta_members"]:
            index.setdefault(code, []).append({**entry_base, "voted": "Tá"})
        for code in v["nil_members"]:
            index.setdefault(code, []).append({**entry_base, "voted": "Níl"})
        for code in v["staonn_members"]:
            index.setdefault(code, []).append({**entry_base, "voted": "Staonn"})
    return index
