"""
migrate_sheets_to_kanka.py — Push Google Sheet map data as Kanka entity Attributes.

Reads location/road data from the existing Google Apps Script endpoints, matches
each row to a Kanka entity by name, and writes map_* attributes to Kanka.

Usage
-----
Dry run (prints changes, writes nothing):
    KANKA_TOKEN=<token> python scripts/migrate_sheets_to_kanka.py --sheet towns

Execute for one named location:
    KANKA_TOKEN=<token> python scripts/migrate_sheets_to_kanka.py --sheet towns --name "Fainsborough" --execute

Execute all locations:
    KANKA_TOKEN=<token> python scripts/migrate_sheets_to_kanka.py --sheet towns --execute

Sheets available:
    towns       Point locations (map_lat / map_lon)
    roads       Polyline locations (map_coords / map_road_type)
    landmarks   Diverse geometry landmarks (map_coords or map_lat/map_lon)

Kanka attributes written
------------------------
  map_lat, map_lon           — point coordinates
  map_coords                 — polyline coordinate string
  map_road_type              — "land" or "sea"
  map_layer                  — "landmarks" (landmarks sheet only)
  map_icon, map_color        — visual overrides (only if non-default)
  map_plane                  — only if not "overworld"
  map_start_year, map_end_year — only if present in Sheet
  map_line_weight            — only if not 2
  map_type                   — only if explicitly set in Sheet
"""

import argparse
import json
import os
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CAMPAIGN = 347078
BASE_URL = f"https://api.kanka.io/1.0/campaigns/{CAMPAIGN}"
TOKEN = os.environ.get("KANKA_TOKEN", "")
RATE_DELAY = 2.0  # seconds between Kanka API calls
RETRY_DELAY = 60  # seconds to wait after a 429 before retrying

SHEET_URLS = {
    "towns": (
        "https://script.google.com/macros/s/"
        "AKfycbzSbU-10nUV-odUN1kUZLquAa5olKwfa9vR5791-DJeSbz3lpEkKzJtCdtdIxC7RWPmqg/exec"
    ),
    "roads": (
        "https://script.google.com/macros/s/"
        "AKfycbzkUU1f0NlWKuCRou6Rlfi5okoKGiHxvrFFviLVrc3zNMd_WSwMSr5JbK-xrQgToRPTbQ/exec"
    ),
    "landmarks": (
        "https://script.google.com/macros/s/"
        "AKfycbx6IJwr3GuU0V2-vHywpL3bgLxcjdiOze6G2IhH42MOmnNzI4qm4okca76tlw3v0o1BBw/exec"
    ),
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_session = requests.Session()


def _kanka_request(method: str, endpoint: str, **kwargs) -> dict:
    _session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    for attempt in range(5):
        time.sleep(RATE_DELAY)
        r = getattr(_session, method)(f"{BASE_URL}{endpoint}", **kwargs)
        if r.status_code == 429:
            wait = RETRY_DELAY * (attempt + 1)
            print(f"    429 rate limit — waiting {wait}s before retry...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def kanka_get(endpoint: str, params: dict | None = None) -> dict:
    return _kanka_request("get", endpoint, params=params or {})


def kanka_post(endpoint: str, body: dict) -> dict:
    return _kanka_request("post", endpoint, json=body)


def kanka_patch(endpoint: str, body: dict) -> dict:
    return _kanka_request("patch", endpoint, json=body)


def fetch_sheet(sheet: str) -> list[dict]:
    url = SHEET_URLS[sheet]
    print(f"Fetching Sheet data from Apps Script ({sheet})...")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    print(f"  {len(data)} rows returned")
    return data


def fetch_all_kanka_locations() -> dict[str, dict]:
    """Return {name_lower: location_dict} for all Kanka location entities."""
    print("Fetching all Kanka location entities...")
    locations = {}
    page = 1
    while True:
        data = kanka_get("/locations", {"page": page, "per_page": 50})
        for loc in data.get("data", []):
            name = (loc.get("name") or "").strip()
            if name:
                locations[name.lower()] = loc
        links = data.get("links", {})
        if not links.get("next"):
            break
        page += 1
    print(f"  {len(locations)} Kanka locations loaded")
    return locations


def fetch_existing_attributes(entity_id: int) -> dict[str, dict]:
    """Return {attr_name: attr_dict} for existing attributes on an entity."""
    try:
        data = kanka_get(f"/entities/{entity_id}/attributes")
        return {a["name"]: a for a in data.get("data", [])}
    except Exception as e:
        print(f"    warning: could not fetch attributes for entity {entity_id}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Attribute builders
# ---------------------------------------------------------------------------

def _str(val) -> str:
    return str(val).strip() if val is not None else ""


def build_town_attrs(row: dict) -> dict[str, str]:
    """Map a towns Sheet row to the Kanka attribute dict we want."""
    attrs = {}

    lat = _str(row.get("lat") or row.get("Lat") or row.get("latitude") or "")
    lon = _str(row.get("lon") or row.get("Lon") or row.get("longitude") or "")
    if not lat or not lon:
        return {}  # no coordinates — skip
    attrs["map_lat"] = lat
    attrs["map_lon"] = lon

    icon = _str(row.get("icon") or row.get("Icon") or "")
    if icon and icon.lower() not in ("", "town"):
        attrs["map_icon"] = icon

    color = _str(row.get("color") or row.get("Color") or "")
    if color:
        attrs["map_color"] = color

    plane = _str(row.get("plane") or row.get("Plane") or "")
    if plane and plane.lower() not in ("", "overworld"):
        attrs["map_plane"] = plane

    start = _str(row.get("startYear") or row.get("startyear") or row.get("start") or "")
    if start:
        attrs["map_start_year"] = start

    end = _str(row.get("endYear") or row.get("endyear") or row.get("end") or "")
    if end:
        attrs["map_end_year"] = end

    map_type = _str(row.get("map_type") or row.get("type") or row.get("Type") or "")
    if map_type and map_type.lower() not in ("", "town"):
        attrs["map_type"] = map_type

    return attrs


def build_road_attrs(row: dict) -> dict[str, str]:
    attrs = {}

    coords = _str(row.get("coords") or row.get("Coords") or "")
    if not coords:
        return {}
    attrs["map_coords"] = coords

    road_type = _str(
        row.get("road_type") or row.get("roadType") or row.get("type") or row.get("Type") or ""
    )
    if road_type:
        attrs["map_road_type"] = road_type.lower()

    color = _str(row.get("color") or row.get("Color") or "")
    if color:
        attrs["map_color"] = color

    lw = _str(row.get("line_weight") or row.get("lineWeight") or row.get("weight") or "")
    if lw and lw not in ("", "2"):
        attrs["map_line_weight"] = lw

    start = _str(row.get("startYear") or row.get("startyear") or row.get("start") or "")
    if start:
        attrs["map_start_year"] = start

    end = _str(row.get("endYear") or row.get("endyear") or row.get("end") or "")
    if end:
        attrs["map_end_year"] = end

    return attrs


def build_landmark_attrs(row: dict) -> dict[str, str]:
    attrs = build_town_attrs(row) or {}

    # Also check for coords (polyline/polygon landmarks)
    coords = _str(row.get("coords") or row.get("Coords") or "")
    if coords and "map_lat" not in attrs:
        attrs["map_coords"] = coords

    geom_type = _str(row.get("type") or row.get("Type") or "")
    if geom_type:
        attrs["map_type"] = geom_type

    lw = _str(row.get("line_weight") or row.get("lineWeight") or row.get("weight") or "")
    if lw and lw not in ("", "2"):
        attrs["map_line_weight"] = lw

    if attrs:
        attrs["map_layer"] = "landmarks"

    return attrs


ATTR_BUILDERS = {
    "towns": build_town_attrs,
    "roads": build_road_attrs,
    "landmarks": build_landmark_attrs,
}


# ---------------------------------------------------------------------------
# Apply attributes to one entity
# ---------------------------------------------------------------------------

def apply_attributes(
    entity_id: int,
    name: str,
    desired: dict[str, str],
    existing: dict[str, dict],
    execute: bool,
):
    """Create or update Kanka attributes. Prints a summary of changes."""
    to_create = {}
    to_update = {}

    for attr_name, attr_value in desired.items():
        if attr_name in existing:
            current_value = _str(existing[attr_name].get("value") or "")
            if current_value != attr_value:
                to_update[attr_name] = (existing[attr_name]["id"], attr_value)
        else:
            to_create[attr_name] = attr_value

    if not to_create and not to_update:
        print(f"  '{name}' — already up to date, no changes needed")
        return

    prefix = "  " if execute else "  [DRY RUN] "

    for attr_name, attr_value in to_create.items():
        print(f"  {prefix}CREATE  {attr_name} = {attr_value!r}")
        if execute:
            kanka_post(
                f"/entities/{entity_id}/attributes",
                {"name": attr_name, "value": attr_value, "type": "", "is_private": False},
            )

    for attr_name, (attr_id, attr_value) in to_update.items():
        print(f"  {prefix}UPDATE  {attr_name} = {attr_value!r}")
        if execute:
            kanka_patch(
                f"/entities/{entity_id}/attributes/{attr_id}",
                {"value": attr_value},
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Migrate Google Sheet map data to Kanka attributes")
    parser.add_argument("--sheet", required=True, choices=list(SHEET_URLS), help="Which Sheet tab to migrate")
    parser.add_argument("--name", default=None, help="Only process the location with this exact name (case-insensitive)")
    parser.add_argument("--execute", action="store_true", help="Write to Kanka (default is dry run)")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: KANKA_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"\n=== migrate_sheets_to_kanka  sheet={args.sheet}  mode={mode} ===\n")

    sheet_rows = fetch_sheet(args.sheet)
    kanka_locations = fetch_all_kanka_locations()
    build_attrs = ATTR_BUILDERS[args.sheet]

    matched = 0
    unmatched = []

    for row in sheet_rows:
        name = _str(row.get("name") or row.get("Name") or row.get("title") or "")
        if not name:
            continue

        # Filter to a single name if --name was given
        if args.name and name.lower() != args.name.lower():
            continue

        kanka_loc = kanka_locations.get(name.lower())
        if not kanka_loc:
            unmatched.append(name)
            continue

        entity_id = kanka_loc.get("entity_id") or kanka_loc.get("id")
        desired_attrs = build_attrs(row)

        if not desired_attrs:
            print(f"  '{name}' — no map coordinates in Sheet, skipping")
            continue

        matched += 1
        print(f"\n[{matched}] '{name}'  (entity {entity_id})")

        existing_attrs = fetch_existing_attributes(entity_id)
        apply_attributes(entity_id, name, desired_attrs, existing_attrs, execute=args.execute)

    print(f"\n--- Summary ---")
    print(f"  Processed: {matched}")
    if unmatched:
        print(f"  No Kanka match found for {len(unmatched)} Sheet rows:")
        for n in unmatched:
            print(f"    - {n}")
    if not args.execute:
        print("\nThis was a dry run. Re-run with --execute to apply changes.")


if __name__ == "__main__":
    main()
