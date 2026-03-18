"""
enrichment/pension_calculator.py
==================================
Estimates TD pension values based on declared Oireachtas service history.

Methodology
-----------
Source:
  Oireachtas Members' Superannuation Fund — governed by the
  Oireachtas (Allowances to Members) and Ministerial and Parliamentary
  Offices Acts, as amended.

Two schemes apply depending on when the TD first entered the Oireachtas:

  Pre-2013 (Defined Benefit, "Legacy DB"):
    - Accrual: 1/40 of final salary per year of service
    - Maximum: 40 years (full salary)
    - Lump sum: 3× annual pension
    - Minimum qualifying service: 3 years (post-2000 amendment)

  Post-2013 (Single Public Service Pension Scheme, "SPSPS"):
    - Introduced for new entrants from 1 January 2013
    - Accrual: 0.58% of career-average salary per year of service
    - Lump sum: 3.75× annual pension
    - Minimum qualifying service: 2 years

Annuity equivalent:
  Calculated as: annual_pension / annuity_rate
  Annuity rate: 4.5% (approximate Irish open-market rate for a level
  annuity at age 65, 2025). This is the rate you would need to earn
  from a lump sum to replicate the pension income indefinitely.

Important caveats:
  - Calculations assume current TD salary (€96,189) throughout service
  - Ministerial / junior-minister service accrues at higher rates (not modelled)
  - For currently serving TDs, pension is unrealised
  - Does not model commutation, spousal pension, or early retirement factors
  - Only Dáil service is counted (Seanad service adds further benefits)
"""

import json
import time
from datetime import date, datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TD_SALARY = 96_189       # Annual TD salary, € (2024 rate, Oireachtas)
ANNUITY_RATE = 0.045     # 4.5% — Irish market rate for level annuity at 65

# Minimum qualifying years (pre-2013 post-2000 amendment: 3 years)
MIN_YEARS_PRE2013 = 3
MIN_YEARS_SPSPS   = 2

# Historical Dáil periods (Dáil number → approximate date range)
DAIL_PERIODS = [
    ("34", "2024-11-29", "2030-01-01"),
    ("33", "2020-02-08", "2024-11-28"),
    ("32", "2016-03-10", "2020-02-07"),
    ("31", "2011-03-09", "2016-03-09"),
    ("30", "2007-06-14", "2011-03-08"),
    ("29", "2002-06-06", "2007-06-13"),
    ("28", "1997-06-26", "2002-06-05"),
    ("27", "1992-11-25", "1997-06-25"),
    ("26", "1989-07-12", "1992-11-24"),
    ("25", "1987-03-10", "1989-07-11"),
    ("24", "1982-11-24", "1987-03-09"),
    ("23", "1982-02-18", "1982-11-23"),
]

BASE_URL = "https://api.oireachtas.ie/v1"

HERE = Path(__file__).parent
CACHE_DIR = HERE.parent / "conflict_radar" / "cache"
PENSION_CACHE = CACHE_DIR / "pension_2025.json"

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _fetch_dail_pids(dail_no: str, start: str, end: str) -> dict:
    """
    Fetch all TD pIds for a given Dáil session.
    Returns: {pId: {fullName, start, end, dail_no}}
    Cached per Dáil.
    """
    cache_path = CACHE_DIR / f"dail_{dail_no}_pids.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    all_members = []
    skip = 0
    while True:
        resp = requests.get(
            f"{BASE_URL}/members",
            params={"chamber": "dail", "date_start": start, "date_end": end,
                    "limit": 250, "skip": skip},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("results", [])
        if not batch:
            break
        all_members.extend(batch)
        total = data.get("head", {}).get("counts", {}).get("memberCount", 0)
        skip += len(batch)
        if skip >= total:
            break
        time.sleep(0.1)

    result = {}
    for m in all_members:
        member = m.get("member", m)
        pid = (member.get("pId") or "").strip()
        full_name = (member.get("fullName") or "").strip()
        if not pid:
            continue
        for ms in member.get("memberships", []):
            ms_data = ms.get("membership", {})
            dr = ms_data.get("dateRange", {})
            result[pid] = {
                "fullName": full_name,
                "start": str(dr.get("start", ""))[:10],
                "end":   str(dr.get("end",   ""))[:10] if dr.get("end") else None,
                "dail_no": dail_no,
            }
            break  # first membership for this Dáil

    CACHE_DIR.mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Dáil {dail_no}: {len(result)} TDs cached")
    return result


def _years_between(start_str: str, end_str: str | None) -> float:
    """Calculate fractional years between two date strings."""
    if not start_str:
        return 0.0
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").date()
        end   = datetime.strptime(end_str,   "%Y-%m-%d").date() if end_str else date.today()
        return max(0.0, (end - start).days / 365.25)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Service history
# ---------------------------------------------------------------------------

def get_service_history(pid: str) -> list[dict]:
    """
    Return a list of Dáil service periods for a TD identified by pId.
    Periods: [{dail_no, start, end, years_served, fullName}]
    Sorted chronologically.
    """
    periods = []
    for dail_no, start_range, end_range in DAIL_PERIODS:
        dail_pids = _fetch_dail_pids(dail_no, start_range, end_range)
        if pid in dail_pids:
            record = dail_pids[pid]
            years = _years_between(record["start"], record["end"])
            if years > 0:
                periods.append({
                    "dail_no":    dail_no,
                    "start":      record["start"],
                    "end":        record["end"],
                    "years_served": round(years, 2),
                    "fullName":   record["fullName"],
                })
    return sorted(periods, key=lambda p: p.get("start", ""))


# ---------------------------------------------------------------------------
# Pension calculation
# ---------------------------------------------------------------------------

def calculate_pension(pid: str, display_name: str) -> dict:
    """
    Calculate estimated pension and annuity equivalent for one TD.

    Parameters
    ----------
    pid          : TD pId from Oireachtas API (e.g. "WilliamAird")
    display_name : Human-readable name for error messages

    Returns a dict with all calculated fields, plus methodology notes.
    """
    service_periods = get_service_history(pid)
    total_years = sum(p["years_served"] for p in service_periods)

    if not service_periods or total_years < 0.5:
        return {
            "pid":                      pid,
            "display_name":             display_name,
            "total_years":              round(total_years, 1),
            "first_elected":            None,
            "pension_scheme":           None,
            "eligible":                 False,
            "annual_pension_estimate":  None,
            "lump_sum_estimate":        None,
            "annuity_equivalent":       None,
            "service_periods":          service_periods,
            "methodology":              "Insufficient service history found in Oireachtas API.",
        }

    first_elected  = service_periods[0]["start"]
    first_year     = int(first_elected[:4])
    qualifying_years = round(total_years, 2)

    # ── Scheme determination ──────────────────────────────────────
    if first_year < 2013:
        # Legacy Defined Benefit scheme
        scheme       = "pre_2013_db"
        scheme_label = "Legacy DB (pre-2013)"
        min_years    = MIN_YEARS_PRE2013
        eligible     = qualifying_years >= min_years
        capped_years = min(qualifying_years, 40.0)
        if eligible:
            annual_pension = (capped_years / 40.0) * TD_SALARY
            lump_sum       = 3.0 * annual_pension
        else:
            annual_pension = 0.0
            lump_sum       = 0.0
        formula = (
            f"min({qualifying_years:.1f} yrs, 40) / 40 × €{TD_SALARY:,} "
            f"= {capped_years:.1f}/40 × salary"
        )
    else:
        # Single Public Service Pension Scheme (SPSPS)
        scheme       = "spsps_2013"
        scheme_label = "SPSPS (post-2013)"
        min_years    = MIN_YEARS_SPSPS
        eligible     = qualifying_years >= min_years
        if eligible:
            annual_pension = 0.0058 * TD_SALARY * qualifying_years
            lump_sum       = 3.75 * annual_pension
        else:
            annual_pension = 0.0
            lump_sum       = 0.0
        formula = (
            f"0.58% × €{TD_SALARY:,} × {qualifying_years:.1f} yrs"
        )

    annuity_equiv = round(annual_pension / ANNUITY_RATE) if annual_pension > 0 else 0

    methodology = (
        f"Scheme: {scheme_label} (first elected {first_year}). "
        f"Formula: {formula}. "
        f"Annuity equivalent = annual pension ÷ {ANNUITY_RATE*100:.1f}% open-market annuity rate. "
        f"Based on current TD salary €{TD_SALARY:,}. "
        f"Dáil service only; does not include Seanad service or ministerial uplift. "
        f"Estimates only — actual value depends on commutation, early retirement, and other factors."
    )

    return {
        "pid":                      pid,
        "display_name":             display_name,
        "total_years":              round(total_years, 1),
        "first_elected":            first_elected,
        "first_elected_year":       first_year,
        "pension_scheme":           scheme,
        "scheme_label":             scheme_label,
        "eligible":                 eligible,
        "annual_pension_estimate":  round(annual_pension),
        "lump_sum_estimate":        round(lump_sum),
        "annuity_equivalent":       annuity_equiv,
        "service_periods":          service_periods,
        "td_salary":                TD_SALARY,
        "annuity_rate_pct":         ANNUITY_RATE * 100,
        "formula":                  formula,
        "methodology":              methodology,
    }


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_pension_calculations(
    members: list[dict],
    force_refresh: bool = False,
) -> dict[str, dict]:
    """
    Calculate pensions for all members.

    Parameters
    ----------
    members : list of {pId, fullName, ...} dicts from members_2025.json
    force_refresh : if True, ignores the pension cache

    Returns
    -------
    {pId: pension_record}
    """
    if PENSION_CACHE.exists() and not force_refresh:
        print(f"  Loading pension data from cache ({PENSION_CACHE.name})...")
        return json.loads(PENSION_CACHE.read_text(encoding="utf-8"))

    print(f"  Pre-caching Dáil membership data across {len(DAIL_PERIODS)} sessions...")
    for dail_no, start, end in DAIL_PERIODS:
        _fetch_dail_pids(dail_no, start, end)

    print(f"  Calculating pensions for {len(members)} TDs...")
    results = {}
    for i, m in enumerate(members, 1):
        pid  = m.get("pId", "")
        name = m.get("fullName", "")
        if not pid:
            continue
        results[pid] = calculate_pension(pid, name)
        if i % 10 == 0:
            print(f"    {i}/{len(members)} done")

    CACHE_DIR.mkdir(exist_ok=True)
    PENSION_CACHE.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  Pension data written → {PENSION_CACHE.name}")
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from conflict_radar.oireachtas_api import fetch_members

    members = fetch_members(year=2025)
    results = run_pension_calculations(members, force_refresh="--refresh" in sys.argv)

    # Print summary table
    rows = [r for r in results.values() if r.get("annual_pension_estimate")]
    rows.sort(key=lambda r: -(r.get("annuity_equivalent") or 0))

    print(f"\n{'TD':<30} {'Yrs':>5} {'Scheme':<12} {'Annual €':>10} {'Lump Sum €':>11} {'Annuity Equiv €':>16}")
    print("-" * 90)
    for r in rows[:20]:
        print(
            f"{r['display_name']:<30} "
            f"{r['total_years']:>5.1f} "
            f"{(r['scheme_label'] or ''):<12} "
            f"{r['annual_pension_estimate']:>10,} "
            f"{r['lump_sum_estimate']:>11,} "
            f"{r['annuity_equivalent']:>16,}"
        )
