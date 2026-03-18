"""
enrichment/cro_enrichment.py
=============================
Extends the CRO parse-only output from conflict_radar/cro_check.py.

What this module does (without an API key):
  1. For companies with a declared CRO number, generate a direct link to the
     CRO company page at core.cro.ie/company/{number}.
  2. For all companies, generate a cleaned search URL and a link to the
     Gazette (IRIS Oifigiúil) for notices.
  3. Classify company type from the legal suffix in the name (Ltd, DAC,
     CLG, UC, PLC, etc.) and set a short description.
  4. Flag voluntary / non-profit companies (CLG = Company Limited by Guarantee).
  5. Summarise per-TD: count of active (non-struck-off) directorships,
     count flagged, voluntary count.

With an API key (future):
  Pass `api_key` to `enrich_cro_records()`.  The function will then:
  - Query the CRO REST API (api.cro.ie) to resolve company numbers.
  - Populate `status`, `td_listed_as_officer`, `match_confidence` fields.
  - Set `found = True` and populate `company_name_cro`.

Note: The CRO website (core.cro.ie) is Cloudflare-protected and cannot be
scraped programmatically.  The enrichment here is URL-generation only.
"""

import re
import urllib.parse

# ---------------------------------------------------------------------------
# CRO direct URL builder
# ---------------------------------------------------------------------------

CRO_COMPANY_BASE = "https://core.cro.ie/company/"
CRO_SEARCH_BASE  = "https://core.cro.ie/search/company?q="


def cro_company_url(company_num: str | int) -> str:
    """Return the CRO company page URL for a given company number."""
    return f"{CRO_COMPANY_BASE}{company_num}"


def cro_search_url(company_name: str) -> str:
    """Return a CRO search URL for a company name."""
    q = urllib.parse.quote(company_name.strip())
    return f"{CRO_SEARCH_BASE}{q}"


# ---------------------------------------------------------------------------
# Company type classification
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, tuple[str, str]] = {
    # suffix_lower → (short_label, description)
    "ltd":       ("Ltd",   "Private company limited by shares"),
    "limited":   ("Ltd",   "Private company limited by shares"),
    "plc":       ("PLC",   "Public limited company"),
    "dac":       ("DAC",   "Designated activity company"),
    "clg":       ("CLG",   "Company limited by guarantee (typically non-profit)"),
    "uc":        ("UC",    "Unlimited company"),
    "slp":       ("SLP",   "Scottish limited partnership"),
    "lp":        ("LP",    "Limited partnership"),
    "llp":       ("LLP",   "Limited liability partnership"),
    "co-op":     ("Co-op", "Co-operative society"),
    "society":   ("Soc",   "Society / co-operative"),
    "trust":     ("Trust", "Trust / charitable body"),
    "charity":   ("Charity","Charitable organisation"),
    "community": ("CLG",   "Community organisation (likely CLG)"),
    "foundation":("Found.","Foundation / charitable body"),
    "council":   ("CLG",   "Community council (likely CLG)"),
    "association":("Assoc.","Association / voluntary body"),
}


def classify_company(name: str) -> dict:
    """
    Classify a company by its name suffix / keywords.

    Returns:
      {
        type_code    : str,   e.g. "CLG", "Ltd", "PLC", "unknown"
        type_label   : str,   e.g. "Company limited by guarantee (typically non-profit)"
        likely_nonprofit : bool,
      }
    """
    low = name.lower()
    for suffix, (code, label) in _TYPE_MAP.items():
        if re.search(r"\b" + re.escape(suffix) + r"\b", low):
            likely_np = code in ("CLG", "Co-op", "Soc", "Charity", "Found.", "CLG", "Assoc.")
            return {"type_code": code, "type_label": label, "likely_nonprofit": likely_np}

    # Try to infer from name patterns
    if any(k in low for k in ["charity", "charitable", "voluntary", "non-profit"]):
        return {"type_code": "Charity", "type_label": "Charitable / voluntary body", "likely_nonprofit": True}

    return {"type_code": "unknown", "type_label": "Company type not identified", "likely_nonprofit": False}


# ---------------------------------------------------------------------------
# Directorship record enrichment
# ---------------------------------------------------------------------------

def enrich_directorship(d: dict, api_key: str | None = None) -> dict:
    """
    Enrich a single directorship record (from cro_check.py output).

    Mutates `d` in-place and returns it.
    """
    company_name = d.get("company_name_declared", "")
    cro_num_declared = d.get("cro_number_declared")

    # ── URL enrichment ────────────────────────────────────────────────────────
    cro_result = d.get("cro_result") or {}

    if cro_num_declared:
        # We have a CRO number — generate direct link
        cro_result["cro_url"]     = cro_company_url(cro_num_declared)
        cro_result["company_num"] = str(cro_num_declared)
        cro_result["found"]       = True
    elif not cro_result.get("cro_url"):
        cro_result["cro_url"] = cro_search_url(company_name)

    d["cro_result"] = cro_result

    # ── Company classification ────────────────────────────────────────────────
    classification = classify_company(company_name)
    d["company_type"]       = classification["type_code"]
    d["company_type_label"] = classification["type_label"]

    # Auto-flag voluntary / CLG if not already flagged
    if classification["likely_nonprofit"] and "voluntary_org" not in d.get("flags", []):
        d.setdefault("flags", [])
        # Only add if not already flagged as voluntary=True
        if not d.get("voluntary"):
            d["flags"] = [f for f in d["flags"] if f != "voluntary_org"]
            d["flags"].append("voluntary_org")

    # If voluntary=True is already set, ensure type reflects it
    if d.get("voluntary") and classification["type_code"] == "unknown":
        d["company_type"]       = "Voluntary"
        d["company_type_label"] = "Voluntary / unpaid role"

    return d


# ---------------------------------------------------------------------------
# Per-TD summary
# ---------------------------------------------------------------------------

def td_cro_summary(td_record: dict) -> dict:
    """
    Summarise the enriched CRO data for one TD.

    Parameters
    ----------
    td_record : enriched TD dict (after enrich_cro_record())

    Returns
    -------
    {
      total_declared    : int,
      commercial_count  : int,   (non-voluntary / non-CLG)
      voluntary_count   : int,
      flagged_count     : int,
      has_direct_links  : bool,  (at least one direct CRO company URL)
      companies         : [str], (list of company names)
    }
    """
    dirs = td_record.get("declared_directorships", [])
    commercial = sum(
        1 for d in dirs
        if not d.get("voluntary") and not d.get("company_type") == "CLG"
    )
    voluntary  = sum(1 for d in dirs if d.get("voluntary") or d.get("company_type") == "CLG")
    flagged    = sum(1 for d in dirs if d.get("flags"))
    direct     = any(
        d.get("cro_result", {}).get("found") for d in dirs
    )
    return {
        "total_declared":   len(dirs),
        "commercial_count": commercial,
        "voluntary_count":  voluntary,
        "flagged_count":    flagged,
        "has_direct_links": direct,
        "companies":        [d.get("company_name_declared", "") for d in dirs],
    }


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------

def enrich_cro_records(cro_data: list[dict], api_key: str | None = None) -> list[dict]:
    """
    Enrich all CRO records in-place.

    Parameters
    ----------
    cro_data  : list of TD dicts from cro_output_{year}.json
    api_key   : optional CRO API key (not yet used — reserved for future)

    Returns
    -------
    The same list, mutated.
    """
    for td in cro_data:
        for d in td.get("declared_directorships", []):
            enrich_directorship(d, api_key=api_key)
        for d in td.get("undeclared_cro_directorships", []):
            enrich_directorship(d, api_key=api_key)
        td["cro_summary"] = td_cro_summary(td)

    return cro_data


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    from pathlib import Path

    cro_path = Path(__file__).parent.parent / "cro_output_2025.json"
    data = json.loads(cro_path.read_text(encoding="utf-8"))

    enrich_cro_records(data)

    print(f"{'TD':<35} {'Dirs':>4} {'Comm.':>6} {'Vol.':>5} {'Flagged':>7}  Companies")
    print("-" * 100)
    for td in sorted(data, key=lambda r: r["name"]):
        s = td.get("cro_summary", {})
        if not s.get("total_declared"):
            continue
        companies = ", ".join(s.get("companies", []))[:60]
        print(
            f"{td['name'][:35]:<35} "
            f"{s['total_declared']:>4} "
            f"{s['commercial_count']:>6} "
            f"{s['voluntary_count']:>5} "
            f"{s['flagged_count']:>7}  "
            f"{companies}"
        )
