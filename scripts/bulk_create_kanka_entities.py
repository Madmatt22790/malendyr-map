"""
bulk_create_kanka_entities.py — Create missing Kanka Location entities from Google Sheet data.

For each row in the Sheet that has no matching Kanka entity, this script:
  1. Creates a new Kanka Location entity (name only)
  2. Immediately writes the map attributes to it

Usage
-----
Dry run (prints what would be created, writes nothing):
    KANKA_TOKEN=<token> python scripts/bulk_create_kanka_entities.py --sheet towns

Execute:
    KANKA_TOKEN=<token> python scripts/bulk_create_kanka_entities.py --sheet towns --execute
    KANKA_TOKEN=<token> python scripts/bulk_create_kanka_entities.py --sheet landmarks --execute
"""

import argparse
import os
import sys
import time

import requests

CAMPAIGN = 347078
BASE_URL = f"https://api.kanka.io/1.0/campaigns/{CAMPAIGN}"
TOKEN = os.environ.get("KANKA_TOKEN", "")
RATE_DELAY = 2.0
RETRY_DELAY = 60

SHEET_URLS = {
    "towns": (
        "https://script.google.com/macros/s/"
        "AKfycbzSbU-10nUV-odUN1kUZLquAa5olKwfa9vR5791-DJeSbz3lpEkKzJtCdtdIxC7RWPmqg/exec"
    ),
    "landmarks": (
        "https://script.google.com/macros/s/"
        "AKfycbx6IJwr3GuU0V2-vHywpL3bgLxcjdiOze6G2IhH42MOmnNzI4qm4okca76tlw3v0o1BBw/exec"
    ),
}

# ---------------------------------------------------------------------------
# HTTP helpers (same pattern as migrate_sheets_to_kanka.py)
# ---------------------------------------------------------------------------

_session = requests.Session()


def _kanka_request(method: str, endpoint: str, **kwargs) -> dict:
    _session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    for attempt in range(5):
        time.sleep(RATE_DELAY)
        r = getattr(_session, method)(f"{BASE_URL}{endpoint}", **kwargs)
        if r.status_code == 429:
            wait = RETRY_DELAY * (attempt + 1)
            print(f"    429 rate limit -- waiting {wait}s before retry...")
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


def fetch_sheet(sheet: str) -> list[dict]:
    url = SHEET_URLS[sheet]
    print(f"Fetching Sheet data ({sheet})...")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    print(f"  {len(data)} rows returned")
    return data


def fetch_all_kanka_locations() -> dict[str, dict]:
    print("Fetching all Kanka location entities...")
    locations = {}
    page = 1
    while True:
        data = kanka_get("/locations", {"page": page, "per_page": 50})
        for loc in data.get("data", []):
            name = (loc.get("name") or "").strip()
            if name:
                locations[name.lower()] = loc
        if not data.get("links", {}).get("next"):
            break
        page += 1
    print(f"  {len(locations)} Kanka locations loaded")
    return locations


# ---------------------------------------------------------------------------
# Attribute builders (identical to migrate_sheets_to_kanka.py)
# ---------------------------------------------------------------------------

def _str(val) -> str:
    return str(val).strip() if val is not None else ""


def build_town_attrs(row: dict) -> dict[str, str]:
    attrs = {}
    lat = _str(row.get("lat") or row.get("Lat") or row.get("latitude") or "")
    lon = _str(row.get("lon") or row.get("Lon") or row.get("longitude") or "")
    if not lat or not lon:
        return {}
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


def build_landmark_attrs(row: dict) -> dict[str, str]:
    attrs = build_town_attrs(row) or {}

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
    "landmarks": build_landmark_attrs,
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bulk-create missing Kanka Location entities from Google Sheet")
    parser.add_argument("--sheet", required=True, choices=list(SHEET_URLS), help="Which Sheet tab to process")
    parser.add_argument("--execute", action="store_true", help="Write to Kanka (default is dry run)")
    args = parser.parse_args()

    if not TOKEN:
        print("ERROR: KANKA_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"\n=== bulk_create_kanka_entities  sheet={args.sheet}  mode={mode} ===\n")

    sheet_rows = fetch_sheet(args.sheet)
    kanka_locations = fetch_all_kanka_locations()
    build_attrs = ATTR_BUILDERS[args.sheet]

    created = 0
    skipped = 0
    no_coords = 0

    for row in sheet_rows:
        name = _str(row.get("name") or row.get("Name") or row.get("title") or "")
        if not name:
            continue

        if name.lower() in kanka_locations:
            skipped += 1
            continue

        desired_attrs = build_attrs(row)
        if not desired_attrs:
            print(f"  '{name}' -- no map coordinates in Sheet, skipping")
            no_coords += 1
            continue

        created += 1
        print(f"\n[{created}] CREATE '{name}'")
        for k, v in desired_attrs.items():
            print(f"    {k} = {v!r}")

        if args.execute:
            # Create the location entity
            resp = kanka_post("/locations", {"name": name})
            new_loc = resp.get("data", {})
            entity_id = new_loc.get("entity_id")
            if not entity_id:
                print(f"    ERROR: no entity_id in response -- {resp}")
                continue

            # Write map attributes immediately
            for attr_name, attr_value in desired_attrs.items():
                kanka_post(
                    f"/entities/{entity_id}/attributes",
                    {"name": attr_name, "value": attr_value, "type": "", "is_private": False},
                )

    print(f"\n--- Summary ---")
    print(f"  Would create / Created: {created}")
    print(f"  Already in Kanka (skipped): {skipped}")
    if no_coords:
        print(f"  No map coordinates (skipped): {no_coords}")
    if not args.execute:
        print("\nThis was a dry run. Re-run with --execute to apply changes.")


if __name__ == "__main__":
    main()
