"""
convert_roads_to_static.py — Convert Google Sheet roads to data/roads_static.json.

Reads road coordinate data from the Google Apps Script roads endpoint and writes
a static JSON file that build_locations.py merges into map_data.json. Roads are
stored as a static file rather than Kanka entities to keep the Kanka Locations
list uncluttered (roads are geometry-only, not lore).

Usage:
    python scripts/convert_roads_to_static.py
"""

import json
import sys
from pathlib import Path

import requests

ROADS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbzkUU1f0NlWKuCRou6Rlfi5okoKGiHxvrFFviLVrc3zNMd_WSwMSr5JbK-xrQgToRPTbQ/exec"
)

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "roads_static.json"

SEA_COLOUR_HINTS = ["00f", "0af", "07f", "08f", "09f", "blue", "teal", "cyan", "aqua"]


def colour_is_sea(colour: str) -> bool:
    c = colour.lower().replace("#", "").replace(" ", "")
    return any(hint in c for hint in SEA_COLOUR_HINTS)


def _str(val) -> str:
    return str(val).strip() if val is not None else ""


def build_road(row: dict) -> dict | None:
    coords = _str(row.get("coords") or row.get("Coords") or "")
    if not coords:
        return None

    name = _str(row.get("name") or row.get("Name") or row.get("title") or "")
    color = _str(row.get("color") or row.get("Color") or "")

    road_type = _str(
        row.get("road_type") or row.get("roadType") or row.get("type") or row.get("Type") or ""
    ).lower()
    if not road_type:
        road_type = "sea" if colour_is_sea(color) else "land"

    lw_raw = _str(row.get("line_weight") or row.get("lineWeight") or row.get("weight") or "2")
    try:
        line_weight = int(lw_raw)
    except ValueError:
        line_weight = 2

    road: dict = {
        "name": name,
        "road_type": road_type,
        "color": color,
        "coords": coords,
        "line_weight": line_weight,
    }

    start = _str(row.get("startYear") or row.get("startyear") or row.get("start") or "")
    if start:
        try:
            road["startYear"] = int(start)
        except ValueError:
            pass

    end = _str(row.get("endYear") or row.get("endyear") or row.get("end") or "")
    if end:
        try:
            road["endYear"] = int(end)
        except ValueError:
            pass

    return road


def main():
    print("Fetching roads from Google Apps Script...")
    try:
        r = requests.get(ROADS_URL, timeout=30)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"ERROR fetching roads: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(rows)} rows returned")

    roads = []
    skipped = 0
    for row in rows:
        road = build_road(row)
        if road is None:
            skipped += 1
            continue
        roads.append(road)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(roads, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {len(roads)} road(s) to {OUTPUT_PATH}")
    if skipped:
        print(f"  Skipped {skipped} row(s) with no coordinates")


if __name__ == "__main__":
    main()