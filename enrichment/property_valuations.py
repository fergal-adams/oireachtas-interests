"""
enrichment/property_valuations.py
==================================
Estimates market values for declared land and property holdings.

Methodology
-----------
Source data for declared properties comes from the Oireachtas Register of
Members' Interests (land_property and property_supplied categories).

Two valuation approaches:

  Residential / urban property:
    County median asking prices (Daft.ie Q4 2024 House Price Report).
    Applied where the declaration text describes a residence, apartment, or
    rental property in an identifiable county.

  Agricultural / rural land:
    Teagasc National Farm Survey / SCSI Land Market Review 2024.
    County average per-acre prices applied to declared acreage (if stated).
    Where acreage is not stated, a token range is flagged rather than a
    specific value.

Confidence levels:
  HIGH    — address + county clearly identified, acreage or property type stated
  MEDIUM  — county identified, no acreage or property type unclear
  LOW     — only county inferred from context; declaration is vague

Important caveats:
  - Estimates are regional averages, not site-specific valuations.
  - Commercial, office, or constituency-use properties are noted but not valued
    (market value for these requires professional appraisal).
  - Multiple properties per declaration require careful parsing.
  - Declarations may describe a share of a property (e.g., 50% ownership).
  - This data is illustrative; always link to the source declaration.
"""

import re

# ---------------------------------------------------------------------------
# County average residential prices — Daft.ie Q4 2024 (asking price, €)
# Source: Daft.ie House Price Report Q4 2024, county-level median asking prices
# Where county-level data unavailable, regional average used.
# ---------------------------------------------------------------------------

COUNTY_HOUSE_PRICE: dict[str, int] = {
    # Dublin
    "dublin":           550_000,
    "south dublin":     510_000,
    "dún laoghaire":    600_000,
    "fingal":           450_000,
    # Leinster
    "wicklow":          430_000,
    "kildare":          380_000,
    "meath":            360_000,
    "louth":            295_000,
    "westmeath":        220_000,
    "longford":         175_000,
    "offaly":           200_000,
    "laois":            205_000,
    "carlow":           225_000,
    "kilkenny":         260_000,
    "wexford":          265_000,
    # Munster
    "cork":             350_000,
    "cork city":        350_000,
    "limerick":         270_000,
    "tipperary":        200_000,
    "waterford":        255_000,
    "kerry":            280_000,
    "clare":            270_000,
    # Connacht
    "galway":           350_000,
    "galway city":      380_000,
    "mayo":             190_000,
    "roscommon":        185_000,
    "sligo":            220_000,
    "leitrim":          165_000,
    # Ulster (ROI)
    "cavan":            200_000,
    "monaghan":         185_000,
    "donegal":          190_000,
}

# ---------------------------------------------------------------------------
# County average agricultural land prices — SCSI / Teagasc 2023 (€ per acre)
# Source: Society of Chartered Surveyors Ireland Land Market Review 2023;
#         Teagasc National Farm Survey 2023 (regional averages).
# ---------------------------------------------------------------------------

COUNTY_LAND_PRICE_PER_ACRE: dict[str, int] = {
    # Leinster
    "kildare":    22_000,
    "meath":      18_000,
    "dublin":     20_000,
    "louth":      16_000,
    "wexford":    14_000,
    "carlow":     13_500,
    "kilkenny":   14_000,
    "wicklow":    12_000,
    "laois":      13_000,
    "offaly":     11_500,
    "westmeath":  12_000,
    "longford":   10_000,
    # Munster
    "cork":       11_000,
    "limerick":   12_500,
    "tipperary":  11_000,
    "waterford":  10_500,
    "kerry":       8_500,
    "clare":      10_000,
    # Connacht
    "galway":      9_500,
    "mayo":        7_000,
    "roscommon":   8_000,
    "sligo":       8_500,
    "leitrim":     6_500,
    # Ulster (ROI)
    "cavan":       9_000,
    "monaghan":    9_500,
    "donegal":     6_000,
}

# National fallback averages
NATIONAL_HOUSE_PRICE     = 295_000
NATIONAL_LAND_PER_ACRE   = 11_000

# ---------------------------------------------------------------------------
# County extraction
# ---------------------------------------------------------------------------

# All 26 ROI counties + common aliases
_COUNTY_ALIASES: dict[str, str] = {
    "co. dublin":    "dublin",
    "co dublin":     "dublin",
    "co. cork":      "cork",
    "co cork":       "cork",
    "co. kerry":     "kerry",
    "co kerry":      "kerry",
    "co. galway":    "galway",
    "co galway":     "galway",
    "co. limerick":  "limerick",
    "co limerick":   "limerick",
    "co. tipperary": "tipperary",
    "co tipperary":  "tipperary",
    "co. wicklow":   "wicklow",
    "co wicklow":    "wicklow",
    "co. wexford":   "wexford",
    "co wexford":    "wexford",
    "co. waterford": "waterford",
    "co waterford":  "waterford",
    "co. kildare":   "kildare",
    "co kildare":    "kildare",
    "co. meath":     "meath",
    "co meath":      "meath",
    "co. louth":     "louth",
    "co louth":      "louth",
    "co. cavan":     "cavan",
    "co cavan":      "cavan",
    "co. monaghan":  "monaghan",
    "co monaghan":   "monaghan",
    "co. donegal":   "donegal",
    "co donegal":    "donegal",
    "co. mayo":      "mayo",
    "co mayo":       "mayo",
    "co. sligo":     "sligo",
    "co sligo":      "sligo",
    "co. leitrim":   "leitrim",
    "co leitrim":    "leitrim",
    "co. roscommon": "roscommon",
    "co roscommon":  "roscommon",
    "co. longford":  "longford",
    "co longford":   "longford",
    "co. westmeath": "westmeath",
    "co westmeath":  "westmeath",
    "co. offaly":    "offaly",
    "co offaly":     "offaly",
    "co. laois":     "laois",
    "co laois":      "laois",
    "co. carlow":    "carlow",
    "co carlow":     "carlow",
    "co. kilkenny":  "kilkenny",
    "co kilkenny":   "kilkenny",
    "co. clare":     "clare",
    "co clare":      "clare",
    # Dublin postcodes → dublin
    "d1": "dublin",  "d2": "dublin",  "d3": "dublin",  "d4": "dublin",
    "d6": "dublin",  "d7": "dublin",  "d8": "dublin",  "d9": "dublin",
    "d10": "dublin", "d11": "dublin", "d12": "dublin", "d13": "dublin",
    "d14": "dublin", "d15": "dublin", "d16": "dublin", "d17": "dublin",
    "d18": "dublin", "d20": "dublin", "d22": "dublin", "d24": "dublin",
}

_COUNTY_NAMES: list[str] = [
    "dublin", "cork", "galway", "limerick", "waterford", "tipperary",
    "kerry", "wexford", "wicklow", "meath", "kildare", "louth",
    "cavan", "monaghan", "donegal", "mayo", "sligo", "leitrim",
    "roscommon", "longford", "westmeath", "offaly", "laois", "carlow",
    "kilkenny", "clare",
]

# Known towns → county map for common locations
_TOWN_TO_COUNTY: dict[str, str] = {
    "tralee": "kerry", "killarney": "kerry", "listowel": "kerry",
    "ennis": "clare", "shannon": "clare", "kilrush": "clare",
    "thurles": "tipperary", "clonmel": "tipperary", "nenagh": "tipperary",
    "cashel": "tipperary", "roscrea": "tipperary",
    "tullamore": "offaly", "birr": "offaly", "edenderry": "offaly",
    "athlone": "westmeath", "mullingar": "westmeath",
    "longford": "longford", "granard": "longford",
    "portlaoise": "laois", "portarlington": "laois",
    "carlow": "carlow", "muinebheag": "carlow",
    "kilkenny": "kilkenny", "thomastown": "kilkenny",
    "gorey": "wexford", "enniscorthy": "wexford", "new ross": "wexford",
    "bray": "wicklow", "greystones": "wicklow", "arklow": "wicklow",
    "navan": "meath", "trim": "meath", "ashbourne": "meath",
    "naas": "kildare", "newbridge": "kildare", "celbridge": "kildare",
    "drogheda": "louth", "dundalk": "louth",
    "castlebar": "mayo", "westport": "mayo", "ballina": "mayo",
    "sligo": "sligo", "boyle": "roscommon",
    "roscommon": "roscommon", "ballaghaderreen": "roscommon",
    "carrick-on-shannon": "leitrim", "manorhamilton": "leitrim",
    "cavan": "cavan", "virginia": "cavan",
    "monaghan": "monaghan", "carrickmacross": "monaghan",
    "letterkenny": "donegal", "donegal": "donegal", "buncrana": "donegal",
    "tuam": "galway", "ballinasloe": "galway", "loughrea": "galway",
    "claregalway": "galway",
    "ballymote": "sligo", "tobercurry": "sligo",
    "mitchelstown": "cork", "mallow": "cork", "macroom": "cork",
    "bantry": "cork", "skibbereen": "cork", "clonakilty": "cork",
    "youghal": "cork", "cobh": "cork", "midleton": "cork",
    "fermoy": "cork", "kanturk": "cork", "kinsale": "cork",
    "schull": "cork", "dunmanway": "cork",
    "limerick": "limerick", "rathkeale": "limerick", "newcastle west": "limerick",
    "bruree": "limerick",
    "waterford": "waterford", "dungarvan": "waterford",
    "glenbeigh": "kerry", "dingle": "kerry", "cahersiveen": "kerry",
    "kenmare": "kerry", "kilgarvan": "kerry",
    "scariff": "clare", "killaloe": "clare",
    "holycross": "tipperary",
    "ballinalee": "longford",
    "ballyshannon": "donegal",
    "williamstown": "galway",
    "meelick": "clare",
    "newmarket": "cork", "kiskeam": "cork",
    "keelogues": "galway",
    "strangefort": "galway",
    "mountshannon": "clare",
    "rathfarnham": "dublin", "ballsbridge": "dublin", "drumcondra": "dublin",
    "finglas": "dublin", "sallins": "kildare",
    "swords": "dublin", "tallaght": "dublin", "blanchardstown": "dublin",
    "clondalkin": "dublin", "lucan": "dublin", "malahide": "dublin",
    "sutton": "dublin", "howth": "dublin",
}


def _extract_county(text: str) -> str | None:
    """Extract the most likely Irish county from a property description string."""
    low = text.lower()

    # 1. Explicit "Co. X" pattern
    for alias, county in _COUNTY_ALIASES.items():
        if alias in low:
            return county

    # 2. Dublin postcode (e.g. "D4", "Dublin 11", "D16")
    if re.search(r"\bd(\d{1,2})\b", low) or re.search(r"\bdublin\s+\d{1,2}\b", low):
        return "dublin"

    # 3. Bare county names
    for cname in _COUNTY_NAMES:
        if re.search(r"\b" + cname + r"\b", low):
            return cname

    # 4. Known towns
    for town, county in _TOWN_TO_COUNTY.items():
        if re.search(r"\b" + re.escape(town) + r"\b", low):
            return county

    return None


# ---------------------------------------------------------------------------
# Property type detection
# ---------------------------------------------------------------------------

def _classify_type(text: str) -> str:
    """
    Returns one of: 'residential', 'agricultural', 'commercial',
                    'constituency_office', 'unknown'.
    """
    low = text.lower()
    # Constituency / non-personal
    if any(k in low for k in [
        "constituency office", "administrative office", "party office", "campaign office"
    ]):
        return "constituency_office"
    # Commercial
    if any(k in low for k in [
        "shop", "commercial", "retail", "office", "industrial", "warehouse",
        "newsagent", "takeaway", "unit", "premises",
    ]):
        return "commercial"
    # Agricultural
    if any(k in low for k in [
        "farm", "farming", "agricultural", "land", "acres", "hectares",
        "mountain", "rural", "tillage", "grazing", "commonage",
    ]):
        return "agricultural"
    # Residential
    if any(k in low for k in [
        "apartment", "flat", "house", "home", "residence", "cottage",
        "bed and breakfast", "b&b", "holiday", "letting", "rental", "rented",
        "dwelling", "bungalow", "semi",
    ]):
        return "residential"
    return "unknown"


# ---------------------------------------------------------------------------
# Acreage extraction
# ---------------------------------------------------------------------------

def _extract_acres(text: str) -> float | None:
    """
    Try to extract an acreage figure from text.
    Handles: '50 acres', '1.08 hectares', 'approx. 35 acres', etc.
    Returns acres (converts hectares → acres at 2.47×).
    """
    # Hectares
    m = re.search(r"([\d,\.]+)\s*hectares?", text, re.I)
    if m:
        try:
            return round(float(m.group(1).replace(",", "")) * 2.471, 1)
        except ValueError:
            pass
    # Acres (e.g. "50 acres", "approx. 35 acres", "38 Acres")
    m = re.search(r"(?:approx\.?\s*)?([\d,\.]+)\s*acres?", text, re.I)
    if m:
        try:
            return round(float(m.group(1).replace(",", "")), 1)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Individual property parser
# ---------------------------------------------------------------------------

def _parse_single_property(text: str) -> dict:
    """
    Parse a single property description string into a valuation dict.

    Returns:
      {
        description, type, county,
        acres (float|None),
        estimated_value (int|None),
        value_range (str|None),     e.g. "€8,000–€12,000/acre"
        valuation_source (str),
        confidence,
        notes (str),
      }
    """
    text = text.strip()
    prop_type = _classify_type(text)
    county = _extract_county(text)
    acres  = _extract_acres(text) if prop_type == "agricultural" else None

    # ── Constituency / commercial → no estimate ──────────────────────────────
    if prop_type == "constituency_office":
        return {
            "description": text,
            "type": prop_type,
            "county": county,
            "acres": None,
            "estimated_value": None,
            "value_range": None,
            "valuation_source": None,
            "confidence": None,
            "notes": "Constituency / administrative office — not a personal property asset.",
        }

    if prop_type == "commercial":
        return {
            "description": text,
            "type": prop_type,
            "county": county,
            "acres": None,
            "estimated_value": None,
            "value_range": None,
            "valuation_source": None,
            "confidence": None,
            "notes": "Commercial property — value requires professional appraisal.",
        }

    # ── Agricultural ──────────────────────────────────────────────────────────
    if prop_type == "agricultural":
        price_per_acre = (
            COUNTY_LAND_PRICE_PER_ACRE.get(county, NATIONAL_LAND_PER_ACRE)
            if county else NATIONAL_LAND_PER_ACRE
        )
        source = (
            f"SCSI/Teagasc land values {county.title() if county else 'national average'} 2023"
        )
        if acres is not None and acres > 0:
            est = round(acres * price_per_acre / 1000) * 1000
            low_v = round(acres * price_per_acre * 0.7 / 1000) * 1000
            high_v = round(acres * price_per_acre * 1.3 / 1000) * 1000
            return {
                "description": text,
                "type": prop_type,
                "county": county,
                "acres": acres,
                "estimated_value": est,
                "value_range": f"€{low_v:,} – €{high_v:,}",
                "valuation_source": source,
                "confidence": "MEDIUM" if county else "LOW",
                "notes": (
                    f"{acres} acres × approx. €{price_per_acre:,}/acre "
                    f"({'county' if county else 'national'} average). "
                    f"Actual value varies with soil quality, access, and planning status."
                ),
            }
        else:
            # Acreage unknown — show per-acre range only
            return {
                "description": text,
                "type": prop_type,
                "county": county,
                "acres": None,
                "estimated_value": None,
                "value_range": f"~€{price_per_acre:,}/acre",
                "valuation_source": source,
                "confidence": "LOW",
                "notes": "Acreage not stated in declaration — per-acre rate shown only.",
            }

    # ── Residential / unknown ─────────────────────────────────────────────────
    if prop_type in ("residential", "unknown"):
        price = (
            COUNTY_HOUSE_PRICE.get(county, NATIONAL_HOUSE_PRICE)
            if county else NATIONAL_HOUSE_PRICE
        )
        source = (
            f"Daft.ie Q4 2024 {county.title() if county else 'national'} median"
        )
        low_v = round(price * 0.7 / 1000) * 1000
        high_v = round(price * 1.3 / 1000) * 1000

        confidence = "MEDIUM"
        notes = (
            f"County median asking price. Actual value depends on "
            f"size, condition, and exact location."
        )
        if not county:
            confidence = "LOW"
            notes = "County not identified — national average used. Treat with caution."
        # Apartment / flat tends to be below median
        if any(k in text.lower() for k in ["apartment", "flat"]):
            price = round(price * 0.75)
            low_v = round(price * 0.7 / 1000) * 1000
            high_v = round(price * 1.3 / 1000) * 1000
            notes = "Apartment/flat — adjusted to ~75% of county median. " + notes

        return {
            "description": text,
            "type": prop_type,
            "county": county,
            "acres": None,
            "estimated_value": price,
            "value_range": f"€{low_v:,} – €{high_v:,}",
            "valuation_source": source,
            "confidence": confidence,
            "notes": notes,
        }

    return {
        "description": text,
        "type": "unknown",
        "county": county,
        "acres": None,
        "estimated_value": None,
        "value_range": None,
        "valuation_source": None,
        "confidence": None,
        "notes": "Could not classify property type.",
    }


# ---------------------------------------------------------------------------
# Declaration text splitter
# ---------------------------------------------------------------------------

def _split_declarations(text: str) -> list[str]:
    """
    Split a declaration string with multiple properties (e.g. "(1) ... (2) ...").
    Returns a list of individual property description strings.
    """
    # Pattern: "(1) text (2) text ..."
    parts = re.split(r"\(\d+\)\s*", text)
    parts = [p.strip().rstrip(";").strip() for p in parts if p.strip()]
    if not parts:
        parts = [text.strip()]
    return parts


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def value_property_interests(land_property_text: str) -> list[dict]:
    """
    Parse and estimate values for a TD's declared land_property interests.

    Parameters
    ----------
    land_property_text : raw text from [land_property] interests_summary field

    Returns
    -------
    List of property dicts, one per declared holding.
    """
    if not land_property_text or not land_property_text.strip():
        return []
    # Strip leading [land_property] tag if present
    text = re.sub(r"^\[land_property\]\s*", "", land_property_text.strip())
    if not text or text.lower() in (
        "i do not own any land or property.",
        "none.",
        "nil.",
        "n/a.",
    ):
        return []
    parts = _split_declarations(text)
    results = []
    for part in parts:
        if not part:
            continue
        results.append(_parse_single_property(part))
    return results


def summarise_valuation(properties: list[dict]) -> dict:
    """
    Aggregate multiple property estimates into a summary dict.

    Returns:
      {
        count           : int,
        total_estimated : int | None,   (sum of estimated_values where available)
        breakdown       : list[dict],   (individual properties)
        any_estimated   : bool,
        notes           : list[str],    (caveats)
      }
    """
    total = 0
    any_est = False
    caveats = []
    for p in properties:
        if p.get("estimated_value") is not None:
            total += p["estimated_value"]
            any_est = True
        if p.get("notes"):
            caveats.append(p["notes"])

    return {
        "count":           len(properties),
        "total_estimated": total if any_est else None,
        "breakdown":       properties,
        "any_estimated":   any_est,
        "notes":           list(dict.fromkeys(caveats)),  # deduplicated
    }


# ---------------------------------------------------------------------------
# CLI test runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent))

    radar_path = Path(__file__).parent.parent / "radar_output_2025.json"
    data = json.loads(radar_path.read_text(encoding="utf-8"))

    print(f"{'TD':<35} {'Type':<14} {'County':<12} {'Acres':>6}  {'Estimate':>12}  Conf.")
    print("-" * 90)

    for td in data:
        summary = td.get("interests_summary", {})
        for _sector, evidence in summary.items():
            for segment in evidence.split(" | "):
                if "[land_property]" in segment:
                    text = re.sub(r"^\[land_property\]\s*", "", segment).strip()
                    props = value_property_interests(text)
                    for p in props:
                        est_str = f"€{p['estimated_value']:,}" if p.get("estimated_value") else (p.get("value_range") or "—")
                        print(
                            f"{td['name'][:35]:<35} "
                            f"{p['type']:<14} "
                            f"{(p['county'] or '?'):<12} "
                            f"{(str(p['acres']) if p['acres'] else '—'):>6}  "
                            f"{est_str:>12}  "
                            f"{p.get('confidence') or '—'}"
                        )
                    break
