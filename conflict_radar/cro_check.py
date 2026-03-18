"""
CRO (Companies Registration Office) directorship cross-check.

For each TD with declared directorships:
  1. Parses the free-text directorships field into structured records
  2. Looks up each company via the CRO API (or generates a search URL in no-key mode)
  3. Checks whether the TD appears as an officer of that company
  4. Searches CRO by TD name to find directorships NOT declared in the register
  5. Flags discrepancies — factual only, no allegation of wrongdoing

CRO API base: https://services.cro.ie/cws/
Auth: Authorization: Basic base64(email:key)
JSON: append ?format=json to all requests

No-key mode: parses company names, generates CRO search URLs, no API calls.
"""

import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

try:
    from unidecode import unidecode
except ImportError:
    def unidecode(s):
        return s

CRO_BASE = "https://services.cro.ie/cws"
CRO_SEARCH_URL = "https://core.cro.ie/search/company"
CACHE_DIR = Path(__file__).parent / "cache"

# ---------------------------------------------------------------------------
# Directorship text parser
# ---------------------------------------------------------------------------

# Roles we recognise in interest declarations (order matters — longer patterns first)
ROLE_PATTERNS = [
    r"non[\s-]?executive\s+director",
    r"voluntary\s+director",
    r"unpaid\s+director",
    r"trustee\s+(?:&|and)\s+director",
    r"trustee(?:/director)?",
    r"director\s+of",
    r"director",
    r"member\s+of\s+board",
    r"member\s+of",
    r"board\s+member",
    r"partner(?:ship)?",
    r"co[\s-]?owner",
    r"proprietor",
    r"shareholder\s+director",
    r"chair(?:man|person)?\s+of",
    r"chair(?:man|person)?",
]

ROLE_RE = re.compile(
    r"\b(" + "|".join(ROLE_PATTERNS) + r")\b",
    re.IGNORECASE,
)

CRO_NUM_RE = re.compile(r"CRO#?\s*(\d{4,7})", re.IGNORECASE)

# Split on numbered/roman item separators between entries
ITEM_SEP_RE = re.compile(
    r"(?:^|\s)(?:\(\s*(?:\d+|[ivxlIVXL]+)\s*\)|\d+\.)(?=\s)",
    re.MULTILINE,
)

VOLUNTARY_RE = re.compile(
    r"\b(voluntary|unpaid|non[\s-]?remunerated|pro\s+bono)\b",
    re.IGNORECASE,
)


def _split_items(text: str) -> list:
    """
    Split a directorships text blob into individual company chunks.
    Handles: (1) ... (2) ..., (i) ... (ii) ..., 1. ... 2. ...
    Falls back to semicolon splitting if no numbered separators found.
    """
    # Find positions of item separators
    splits = [m.start() for m in ITEM_SEP_RE.finditer(text)]
    if len(splits) >= 2:
        # Build chunks between separators
        chunks = []
        for i, pos in enumerate(splits):
            end = splits[i + 1] if i + 1 < len(splits) else len(text)
            chunk = text[pos:end].strip()
            # Strip leading "(1)", "(i)", "1." etc.
            chunk = re.sub(r"^\(\s*(?:\d+|[ivxlIVXL]+)\s*\)\s*", "", chunk)
            chunk = re.sub(r"^\d+\.\s*", "", chunk)
            if chunk:
                chunks.append(chunk)
        return chunks

    # Fallback: split on semicolons that separate entries (not address parts)
    # Only split on "; " followed by a capital letter or a role keyword
    parts = re.split(r";\s+(?=[A-Z(])", text)
    return [p.strip() for p in parts if p.strip()]


# Regex for roles that introduce company name via "ROLE of CompanyName" (no colon)
_ROLE_OF_RE = re.compile(
    r"^(?:director|member|chair(?:man|person)?|trustee)\s+of\b",
    re.IGNORECASE,
)

# Pattern to normalise "(Director)" style parenthesised roles to "Director:"
_PAREN_ROLE_RE = re.compile(
    r"^\(\s*(" + "|".join(ROLE_PATTERNS) + r")\s*\)\s*",
    re.IGNORECASE,
)


def _extract_role(chunk: str) -> tuple:
    """
    Return (role_string, text_containing_company_name).
    If no role found, role_string is "Director" (assumed).

    Handles patterns like:
      "Director: Company Name, Addr: description"
      "Director & Chairman: Company Name, Addr: description"
      "Director of Company Name Ltd.: description"
      "(Director) Company Name: description"
      "Non-Executive Director: Company Name"
      "Trustee & Director, Company Name"
      "Member of Board: Company Name"
    """
    # Preprocess: normalise "(Director) Company" → "Director: Company"
    paren_m = _PAREN_ROLE_RE.match(chunk)
    if paren_m:
        role = re.sub(r"\s+", " ", paren_m.group(1)).strip().title()
        remainder = chunk[paren_m.end():].lstrip(" :,\t")
        return role, remainder

    colon_pos = chunk.find(":")
    comma_pos = chunk.find(",")

    # Prefer colon as role/company separator when it appears before any comma
    sep_pos = -1
    if colon_pos >= 0 and (comma_pos < 0 or colon_pos <= comma_pos):
        sep_pos = colon_pos
    elif comma_pos >= 0:
        sep_pos = comma_pos

    if sep_pos > 0:
        before_sep = chunk[:sep_pos].strip()
        after_sep = chunk[sep_pos + 1:].strip()

        m = ROLE_RE.search(before_sep)
        if m:
            role = re.sub(r"\s+", " ", m.group(1)).strip().title()

            # "ROLE of CompanyName" — company is embedded in before_sep
            # e.g. "Director of Clongriffin Business Administration Limited"
            # e.g. "Member of Oireachtas Commission"
            if _ROLE_OF_RE.match(before_sep):
                company_text = before_sep[m.end():].lstrip(" ").strip()
                if company_text and len(company_text) > 3:
                    return role, company_text

            return role, after_sep

    # Fall back: search anywhere in chunk
    m = ROLE_RE.search(chunk)
    if m:
        role = re.sub(r"\s+", " ", m.group(1)).strip().title()
        after = chunk[m.end():].lstrip(" :,\t")
        return role, after

    # No role found — assume Director
    return "Director", chunk


def _extract_company_name(text: str) -> tuple:
    """
    Return (company_name, remainder).
    Strategy:
      1. If text contains a corporate suffix (Ltd, CLG, etc.), truncate there.
      2. Otherwise take text up to the first colon (strips description).
      3. Then take text up to the first comma (strips address).
    """
    # Strip CRO number (captured separately)
    text_clean = CRO_NUM_RE.sub("", text).strip(" ,;")

    # Known suffixes that self-terminate the company name
    suffix_re = re.compile(
        r"\b(Ltd|Limited|CLG|DAC|PLC|LLP|UC|CIO|CIC|Teoranta|Teo\.?)\b[.,]?",
        re.IGNORECASE,
    )

    m = suffix_re.search(text_clean)
    if m:
        end = m.end()
        company = text_clean[:end].strip(" ,;:")
        remainder = text_clean[end:].strip(" ,;:")
        return company, remainder

    # No suffix: take up to first colon (removes description)
    parts = text_clean.split(":", 1)
    company_candidate = parts[0].strip(" ,;")
    remainder = parts[1].strip() if len(parts) > 1 else ""

    # Then trim at first comma to strip address
    comma_parts = company_candidate.split(",", 1)
    company = comma_parts[0].strip(" .;")

    return company, remainder


def parse_directorships(text: str) -> list:
    """
    Parse free-text directorships declaration into a list of structured records.

    Returns:
        [
            {
                "role": "Director",
                "company_name": "Kerry Tourism Alliance",
                "cro_number": None,
                "voluntary": False,
            },
            ...
        ]
    """
    if not text or not text.strip():
        return []

    chunks = _split_items(text)
    results = []

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        # Extract CRO number from raw chunk before any stripping
        cro_match = CRO_NUM_RE.search(chunk)
        cro_number = cro_match.group(1) if cro_match else None

        # Voluntary flag
        voluntary = bool(VOLUNTARY_RE.search(chunk))

        # Extract role
        role, after_role = _extract_role(chunk)

        # Extract company name
        company_name, _ = _extract_company_name(after_role)

        # Clean up company name
        company_name = re.sub(r"\s+", " ", company_name).strip(" ,;:()")
        company_name = re.sub(r"\bCRO#?\s*\d+\b", "", company_name, flags=re.IGNORECASE).strip()

        # Strip address fragments: everything after the second comma is likely an address
        # e.g. "Crumlin Centre for Musical Arts, Armagh Road, Crumlin, Dublin 12"
        #      → "Crumlin Centre for Musical Arts"
        parts = company_name.split(",")
        if len(parts) >= 3:
            # Check if part after first comma looks like an address (short token, digit, street word)
            second_part = parts[1].strip()
            if re.search(r"\b(road|street|avenue|lane|drive|way|close|court|place|sq|st|rd|co\.|dublin|cork|galway|limerick|\d)\b", second_part, re.IGNORECASE):
                company_name = parts[0].strip()

        # Skip if result is just a common English word (spurious parse artifact)
        SKIP_NAMES = {"member", "director", "trustee", "partner", "board", "company"}
        if company_name.lower() in SKIP_NAMES or len(company_name) < 4:
            continue

        results.append({
            "role": role,
            "company_name": company_name,
            "cro_number": cro_number,
            "voluntary": voluntary,
        })

    return results


# ---------------------------------------------------------------------------
# CRO API helpers
# ---------------------------------------------------------------------------

def _make_session(api_email: str, api_key: str):
    """Return a requests.Session with Basic auth headers set."""
    if not _REQUESTS_AVAILABLE:
        raise ImportError("requests library required for CRO API calls")
    credentials = base64.b64encode(f"{api_email}:{api_key}".encode()).decode()
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Basic {credentials}",
        "Accept": "application/json",
    })
    return session


def _cro_get(url: str, params: dict, session) -> dict | None:
    """Make a CRO API GET request. Returns parsed JSON or None on error."""
    params = {**params, "format": "json"}
    try:
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def cro_lookup_company(company_name: str, cro_number: str | None, session) -> dict:
    """
    Look up a company in CRO.

    If cro_number is provided, fetch by number (preferred).
    Otherwise, search by name.

    Returns:
        {
            "found": True/False/None,   # None = no API key
            "company_num": str,
            "company_name_cro": str,
            "status": str,              # "Normal", "Dissolved", "Struck Off", etc.
            "match_confidence": str,    # "number_match", "exact", "fuzzy"
            "cro_url": str,             # always populated
        }

    CRO API response shapes:
      By number: single dict with keys company_num, company_name, company_status_desc
      By name search: list of dicts with same keys
    """
    search_url = f"{CRO_SEARCH_URL}?q={quote(company_name)}"

    if session is None:
        return {
            "found": None,
            "company_num": cro_number,
            "company_name_cro": None,
            "status": None,
            "match_confidence": None,
            "cro_url": search_url,
        }

    # Prefer lookup by CRO number
    if cro_number:
        data = _cro_get(f"{CRO_BASE}/company/{cro_number}/c", {}, session)
        if data and isinstance(data, dict) and data.get("company_name"):
            return {
                "found": True,
                "company_num": str(data.get("company_num", cro_number)),
                "company_name_cro": data.get("company_name", ""),
                "status": data.get("company_status_desc", ""),
                "match_confidence": "number_match",
                "cro_url": search_url,
            }

    # Search by name — response is a list
    data = _cro_get(
        f"{CRO_BASE}/companies",
        {"company_name": company_name, "busObjectType": "companies", "max": 5},
        session,
    )

    # Normalise: API returns list directly
    if isinstance(data, list):
        hits = data
    elif isinstance(data, dict):
        hits = data.get("companies", data.get("results", []))
    else:
        hits = []

    if not hits:
        return {
            "found": False,
            "company_num": cro_number,
            "company_name_cro": None,
            "status": None,
            "match_confidence": None,
            "cro_url": search_url,
        }

    # Pick best match
    best = hits[0]
    cro_name = best.get("company_name", best.get("companyName", ""))
    confidence = "exact" if cro_name.lower() == company_name.lower() else "fuzzy"

    return {
        "found": True,
        "company_num": str(best.get("company_num", best.get("companyNum", ""))),
        "company_name_cro": cro_name,
        "status": best.get("company_status_desc", best.get("companyStatus", "")),
        "match_confidence": confidence,
        "cro_url": search_url,
    }


def cro_lookup_person(td_name: str, session) -> list:
    """
    Search CRO for all directorships held by a person (to find undeclared ones).

    td_name: register format, e.g. "BRABAZON, Tom (Dublin Bay North)"
    Returns list of {company_name, company_num, role, status}

    CRO persons API may return a list of company/officer dicts.
    """
    if session is None:
        return []

    # Normalise: "BRABAZON, Tom (Dublin Bay North)" → "Tom Brabazon"
    normalised = _normalise_td_name(td_name)

    data = _cro_get(
        f"{CRO_BASE}/persons",
        {"person_name": normalised, "max": 20},
        session,
    )

    if not data:
        return []

    # Normalise list vs wrapped response
    if isinstance(data, list):
        persons = data
    else:
        persons = data.get("persons", data.get("results", []))

    results = []
    for p in persons:
        results.append({
            "company_name": p.get("company_name", p.get("companyName", "")),
            "company_num": str(p.get("company_num", p.get("companyNum", ""))),
            "role": p.get("role", p.get("officer_type", "")),
            "status": p.get("company_status_desc", p.get("companyStatus", "")),
        })
    return results


def cro_check_officers(company_num: str, td_name: str, session) -> dict:
    """
    Check if a TD appears in the current officers of a company.

    Returns {"td_found": bool, "officers": [list of officer names]}

    CRO officers API may return a list of officer dicts with person_name or personName.
    """
    if session is None or not company_num:
        return {"td_found": None, "officers": []}

    data = _cro_get(f"{CRO_BASE}/company/{company_num}/officers", {}, session)
    if not data:
        return {"td_found": False, "officers": []}

    # Normalise list vs wrapped response
    if isinstance(data, list):
        officers_raw = data
    else:
        officers_raw = data.get("officers", data.get("results", []))

    officer_names = []
    for o in officers_raw:
        name = o.get("person_name", o.get("personName", o.get("name", "")))
        if name:
            officer_names.append(name)

    td_found = _name_matches_any(td_name, officer_names)
    return {"td_found": td_found, "officers": officer_names}


# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_td_name(register_name: str) -> str:
    """
    'BRABAZON, Tom (Dublin Bay North)' → 'Tom Brabazon'
    'AIRD, William (Laois)'            → 'William Aird'
    """
    # Remove constituency
    name = re.sub(r"\(.*?\)", "", register_name)
    # Remove unidecode accents
    name = unidecode(name)
    # Split on comma: "SURNAME, Firstname"
    parts = [p.strip() for p in name.split(",", 1)]
    if len(parts) == 2:
        surname, firstname = parts
        return f"{firstname.title()} {surname.title()}"
    return name.strip().title()


def _name_matches_any(td_name: str, officer_names: list) -> bool:
    """
    Return True if td_name fuzzy-matches any name in officer_names.
    Tries: exact case-insensitive, then last-name-only.
    """
    query = _normalise_td_name(td_name).lower()
    query_parts = query.split()
    for officer in officer_names:
        officer_lower = unidecode(officer).lower()
        if query == officer_lower:
            return True
        # All query tokens present in officer name
        if all(p in officer_lower for p in query_parts):
            return True
    return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_cro_check(
    interests_path: str,
    api_email: str | None = None,
    api_key: str | None = None,
    year: int = 2025,
    force_refresh: bool = False,
) -> list:
    """
    Run CRO directorship cross-check for all TDs with declared directorships.

    Returns list of per-TD CRO check reports.
    """
    import os

    # API session (None = parse-only mode)
    session = None
    if api_email and api_key:
        session = _make_session(api_email, api_key)
    elif os.environ.get("CRO_EMAIL") and os.environ.get("CRO_API_KEY"):
        session = _make_session(os.environ["CRO_EMAIL"], os.environ["CRO_API_KEY"])

    mode = "full API verification" if session else "parse-only (no API key)"
    print(f"  CRO check mode: {mode}")

    # Check cache
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"cro_{year}.json"
    if cache_file.exists() and not force_refresh and session:
        print(f"  Loading CRO results from cache: {cache_file}")
        with open(cache_file, encoding="utf-8") as f:
            return json.load(f)

    # Load interests
    with open(interests_path, encoding="utf-8") as f:
        td_interests = json.load(f)

    reports = []

    for td in td_interests:
        directorship_text = td.get("interests", {}).get("directorships", "").strip()
        if not directorship_text:
            continue

        register_name = td["name"]
        parsed = parse_directorships(directorship_text)
        if not parsed:
            continue

        declared = []
        all_flags = []

        for entry in parsed:
            company_name = entry["company_name"]
            cro_number = entry["cro_number"]

            # CRO lookup
            cro_result = cro_lookup_company(company_name, cro_number, session)

            flags = []

            if session:
                if cro_result["found"] is False:
                    flags.append("COMPANY_NOT_FOUND")
                elif cro_result["found"] is True:
                    status = (cro_result.get("status") or "").lower()
                    if any(s in status for s in ("dissolved", "struck off", "liquidat")):
                        flags.append("COMPANY_DISSOLVED")

                    # Check if TD appears as officer
                    officer_check = cro_check_officers(
                        cro_result.get("company_num") or cro_number or "",
                        register_name,
                        session,
                    )
                    cro_result["td_listed_as_officer"] = officer_check["td_found"]
                    if officer_check["td_found"] is False:
                        flags.append("TD_NOT_LISTED_AS_OFFICER")
                else:
                    cro_result["td_listed_as_officer"] = None

                time.sleep(0.15)  # be polite to the API
            else:
                cro_result["td_listed_as_officer"] = None

            declared.append({
                "role": entry["role"],
                "company_name_declared": company_name,
                "cro_number_declared": cro_number,
                "voluntary": entry["voluntary"],
                "cro_result": cro_result,
                "flags": flags,
            })
            all_flags.extend(flags)

        # Search CRO by person name to find undeclared directorships
        undeclared = []
        if session:
            cro_directorships = cro_lookup_person(register_name, session)
            declared_names_lower = {
                d["company_name_declared"].lower() for d in declared
            }
            for cro_dir in cro_directorships:
                cro_name_lower = cro_dir["company_name"].lower()
                # Check if this CRO company is in the declared list
                already_declared = any(
                    cro_name_lower in d.lower() or d.lower() in cro_name_lower
                    for d in declared_names_lower
                )
                if not already_declared:
                    undeclared.append(cro_dir)
                    all_flags.append("UNDECLARED_CRO_DIRECTORSHIP")
            time.sleep(0.15)

        reports.append({
            "name": register_name,
            "declared_directorships": declared,
            "undeclared_cro_directorships": undeclared,
            "flags": sorted(set(all_flags)),
        })

    # Cache results if we made API calls
    if session:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(reports, f, indent=2, ensure_ascii=False)
        print(f"  Cached CRO results → {cache_file}")

    return reports


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

def summarise_cro(reports: list) -> str:
    total = len(reports)
    with_flags = sum(1 for r in reports if r["flags"])
    not_found = sum(
        1 for r in reports
        if any("COMPANY_NOT_FOUND" in d["flags"] for d in r["declared_directorships"])
    )
    dissolved = sum(
        1 for r in reports
        if any("COMPANY_DISSOLVED" in d["flags"] for d in r["declared_directorships"])
    )
    not_officer = sum(
        1 for r in reports
        if any("TD_NOT_LISTED_AS_OFFICER" in d["flags"] for d in r["declared_directorships"])
    )
    undeclared = sum(1 for r in reports if r["undeclared_cro_directorships"])

    lines = [
        "=" * 70,
        "  CRO DIRECTORSHIP CROSS-CHECK — SUMMARY",
        "=" * 70,
        f"  TDs with declared directorships:  {total}",
        f"  TDs with any flag:               {with_flags}",
        f"  Company not found in CRO:        {not_found}",
        f"  Company dissolved/struck off:    {dissolved}",
        f"  TD not listed as officer:        {not_officer}",
        f"  Undeclared CRO directorships:    {undeclared}",
        "",
        "  FLAGGED TDs:",
        "-" * 70,
    ]

    for r in reports:
        if not r["flags"]:
            continue
        lines.append(f"  {r['name']}")
        for d in r["declared_directorships"]:
            if d["flags"]:
                cro_name = d["cro_result"].get("company_name_cro") or ""
                cro_url = d["cro_result"].get("cro_url", "")
                lines.append(
                    f"    [{', '.join(d['flags'])}] {d['company_name_declared']}"
                    + (f" → {cro_name}" if cro_name else "")
                )
                if cro_url:
                    lines.append(f"      Search: {cro_url}")
        for u in r["undeclared_cro_directorships"]:
            lines.append(
                f"    [UNDECLARED_CRO_DIRECTORSHIP] {u['company_name']} "
                f"(#{u['company_num']}, {u['status']})"
            )
        lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)
