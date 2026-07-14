"""
Build map_data.json and road_graph.json from Kanka entities.

Usage:
    KANKA_TOKEN=<token> python scripts/build_locations.py

Reads Kanka location entities and organisations in campaign 347078 and writes:
  data/map_data.json   — locations, roads, landmarks, parties for the Leaflet map
  data/road_graph.json — routing graph (nodes + edges) for Dijkstra's

Kanka attribute conventions (set on the entity's Attributes tab):
  Point locations / landmarks:
    map_lat            float (required)
    map_lon            float (required)
    map_layer          string  "landmarks" routes to landmarks layer; omit for towns
    map_icon           string  e.g. "port_city"  (default: "town")
    map_color          string  e.g. "purple"      (default: "")
    map_plane          string  "overworld" / "underdark"  (default: "overworld")
    map_start_year     int     (default: omitted = no timeline)
    map_end_year       int     (default: omitted = no timeline)
    map_type           string  "town" / "marker" / "polyline" / "polygon"
    map_visible_to_players  "true" / "false"  (default: "true")

  Timeline-ranged positions (for locations that move over time):
    map_lat_STARTYEAR_ENDYEAR  float  e.g. map_lat_1186_2505 = -14.651474
    map_lon_STARTYEAR_ENDYEAR  float  e.g. map_lon_1186_2505 = -83.446655
    When any ranged pair is present, the plain map_lat/map_lon is ignored and
    one timeline-controlled entry is emitted per pair. Same layering/icon/color
    attributes apply to all positions.

  Road / polyline entities:
    map_coords         string  "lat,lon,lat,lon,..."  (required)
    map_road_type      string  "land" / "sea"         (required)
    map_layer          string  "landmarks" routes polyline to landmarks layer
    map_color          string  e.g. "#8B6914"
    map_start_year     int
    map_end_year       int
    map_line_weight    int  (default: 2)

  Organisations (party locations):
    map_lat / map_lon  float (required to appear on map)
    map_icon, map_color, map_start_year, map_end_year  same as above
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CAMPAIGN = 347078
BASE_URL = f"https://api.kanka.io/1.0/campaigns/{CAMPAIGN}"
TOKEN = os.environ.get("KANKA_TOKEN", "")
RATE_DELAY = 1.5           # seconds between API calls
RETRY_DELAY = 60           # seconds to wait after a 429 before retrying
SNAP_TOL = 0.5             # coordinate units for road-to-road endpoint snapping
SNAP_WARN_TOL = 1.0        # flag endpoints within this distance but not snapped
TOWN_SNAP_TOL = 5.0        # snap road endpoints to nearby towns within this distance

OUTPUT_DIR = Path(__file__).parent.parent / "data"

# Colour → road_type auto-classification (hex fragments or colour names)
# Overridden by explicit map_road_type attribute.
SEA_COLOUR_HINTS = ["00f", "0af", "07f", "08f", "09f", "blue", "teal", "cyan", "aqua"]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})


def api_get(endpoint: str, params: dict | None = None) -> dict:
    for attempt in range(5):
        time.sleep(RATE_DELAY)
        r = _session.get(f"{BASE_URL}{endpoint}", params=params or {})
        if r.status_code == 429:
            wait = RETRY_DELAY * (attempt + 1)
            print(f"    429 rate limit — waiting {wait}s before retry...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def fetch_all_locations() -> list[dict]:
    """Return all location entities (paginated)."""
    locations = []
    page = 1
    while True:
        data = api_get("/locations", {"page": page, "per_page": 50})
        batch = data.get("data", [])
        locations.extend(batch)
        links = data.get("links", {})
        if not links.get("next"):
            break
        page += 1
        print(f"  fetched page {page - 1} ({len(locations)} locations so far)")
    return locations


def fetch_attributes(entity_id: int) -> dict[str, str]:
    """Return {name: value} dict for a Kanka entity's attributes."""
    try:
        data = api_get(f"/entities/{entity_id}/attributes")
        return {a["name"]: str(a.get("value") or "") for a in data.get("data", [])}
    except Exception as e:
        print(f"    warning: could not fetch attributes for entity {entity_id}: {e}")
        return {}


def fetch_all_organisations() -> list[dict]:
    """Return all organisation entities (paginated)."""
    orgs = []
    page = 1
    while True:
        data = api_get("/organisations", {"page": page, "per_page": 50})
        batch = data.get("data", [])
        orgs.extend(batch)
        links = data.get("links", {})
        if not links.get("next"):
            break
        page += 1
        print(f"  fetched page {page - 1} ({len(orgs)} organisations so far)")
    return orgs


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in metres (treating fictional coords as lat/lon)."""
    R = 6_371_000
    rl1, rl2 = math.radians(lat1), math.radians(lat2)
    drlat = math.radians(lat2 - lat1)
    drlon = math.radians(lon2 - lon1)
    a = math.sin(drlat / 2) ** 2 + math.cos(rl1) * math.cos(rl2) * math.sin(drlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def parse_coords_string(s: str) -> list[tuple[float, float]]:
    """Parse 'lat,lon,lat,lon,...' string into [(lat,lon), ...] pairs."""
    vals = [v.strip() for v in s.split(",")]
    pts = []
    for i in range(0, len(vals) - 1, 2):
        try:
            pts.append((float(vals[i]), float(vals[i + 1])))
        except ValueError:
            pass
    return pts


def colour_is_sea(colour: str) -> bool:
    """Heuristically classify a colour string as sea/water."""
    c = colour.lower().replace("#", "").replace(" ", "")
    return any(hint in c for hint in SEA_COLOUR_HINTS)


# ---------------------------------------------------------------------------
# Road graph builder
# ---------------------------------------------------------------------------

def snap_or_create(pt: tuple[float, float], nodes: list[dict]) -> int:
    """Return index of existing node within SNAP_TOL, or create new one."""
    for i, n in enumerate(nodes):
        if abs(n["lat"] - pt[0]) < SNAP_TOL and abs(n["lon"] - pt[1]) < SNAP_TOL:
            return i
    idx = len(nodes)
    nodes.append({"id": idx, "lat": pt[0], "lon": pt[1]})
    return idx


def build_road_graph(
    roads: list[dict],
    town_lookup: dict[str, tuple[float, float]] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """
    Build nodes + edges from road coordinate chains.

    town_lookup: optional {lower_name: (lat, lon)} used to snap road endpoints
    to their named towns (e.g. "Gora - Birch" → start snaps to Gora's coords).
    This is more reliable than distance-based snapping because towns can be very
    close together.

    Returns (nodes, edges, warnings).
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    warnings: list[str] = []

    def name_snap(road_name: str, is_start: bool) -> tuple[float, float] | None:
        """Return exact town coords for the start/end of a named road, or None."""
        if not town_lookup:
            return None
        parts = [p.strip() for p in road_name.split(" - ")]
        key = (parts[0] if is_start else parts[-1]).lower()
        return town_lookup.get(key)

    for road in roads:
        raw = road.get("coords", "")
        if not raw:
            continue
        pts = parse_coords_string(raw) if isinstance(raw, str) else [(p[0], p[1]) for p in raw]
        if len(pts) < 2:
            warnings.append(f"Road '{road.get('name')}' has fewer than 2 coordinate points — skipped")
            continue

        road_name = road.get("name", "")
        from_town = road.get("from", "")
        to_town   = road.get("to", "")

        # Snap endpoints to named town positions when available.
        # Prefer explicit from/to fields; fall back to name-based parsing for old-style roads.
        if from_town and town_lookup:
            start_pt = town_lookup.get(from_town.lower()) or pts[0]
        else:
            start_pt = name_snap(road_name, is_start=True) or pts[0]

        if to_town and town_lookup:
            to_coords = town_lookup.get(to_town.lower())
            if to_coords:
                end_pt = to_coords
            else:
                # Junction/waypoint not in town_lookup: pick whichever endpoint
                # is farther from start_pt. For a correctly-drawn road pts[-1] is
                # farther; for a reversed draw pts[0] is farther. This avoids the
                # previous threshold check which false-positively merged sea
                # junctions that sit within SNAP_TOL of their parent town.
                dist_first = abs(pts[0][0] - start_pt[0]) + abs(pts[0][1] - start_pt[1])
                dist_last  = abs(pts[-1][0] - start_pt[0]) + abs(pts[-1][1] - start_pt[1])
                end_pt = pts[0] if dist_last < dist_first else pts[-1]
        else:
            end_pt = name_snap(road_name, is_start=False) or pts[-1]

        start_id = snap_or_create(start_pt, nodes)
        end_id = snap_or_create(end_pt, nodes)

        # Compute total polyline length in Haversine metres
        total_m = sum(haversine_m(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                      for i in range(len(pts) - 1))

        # Normalise stored coords to from→to direction so the route-drawing
        # reverse-check in index.html works correctly for all roads regardless
        # of which direction they were drawn on the map.
        coord_pts = pts
        dist0 = abs(pts[0][0] - start_pt[0]) + abs(pts[0][1] - start_pt[1])
        distN = abs(pts[-1][0] - start_pt[0]) + abs(pts[-1][1] - start_pt[1])
        if distN < dist0:
            coord_pts = pts[::-1]
        raw_coords = ", ".join(f"{p[0]}, {p[1]}" for p in coord_pts)

        edges.append({
            "from": start_id,
            "to": end_id,
            "distance": round(total_m, 1),
            "road_type": road.get("road_type", "land"),
            "road_name": road.get("name", ""),
            "coords": raw_coords,
        })

    # Connectivity report: flag endpoints that have no snap partner within 2×SNAP_TOL
    for i, node in enumerate(nodes):
        # Count how many edges touch this node
        degree = sum(1 for e in edges if e["from"] == i or e["to"] == i)
        if degree == 1:
            # Check if any other node is within SNAP_WARN_TOL (might be an unsnapped junction)
            close = [j for j, n in enumerate(nodes)
                     if j != i and abs(n["lat"] - node["lat"]) < SNAP_WARN_TOL
                     and abs(n["lon"] - node["lon"]) < SNAP_WARN_TOL]
            if close:
                warnings.append(
                    f"Possible unsnapped endpoint at ({node['lat']:.4f}, {node['lon']:.4f}) "
                    f"— {len(close)} node(s) nearby within {SNAP_WARN_TOL} units"
                )

    return nodes, edges, warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_town_kanka_links(raw_locations: list[dict]) -> None:
    """
    For each *.geojson in data/towns/, write a <town>_links.json mapping
    building names to their Kanka location URL.  Matching is case-insensitive
    on the building's 'name' property against all Kanka location entity names.
    """
    towns_dir = OUTPUT_DIR / "towns"
    if not towns_dir.exists():
        return

    # Build lookup from ALL Kanka locations (not just map-visible ones)
    name_to_kanka: dict[str, dict] = {}
    for loc in raw_locations:
        child = loc.get("child") or {}
        name = (loc.get("name") or child.get("name") or "").strip()
        if not name:
            continue
        loc_id = child.get("id") or loc.get("id")
        name_to_kanka[name.lower()] = {
            "name": name,
            "kanka_url": f"https://kanka.io/en-US/campaign/{CAMPAIGN}/locations/{loc_id}",
        }

    for geojson_path in sorted(towns_dir.glob("*.geojson")):
        town_name = geojson_path.stem
        try:
            with open(geojson_path, encoding="utf-8") as f:
                gj = json.load(f)
        except Exception as e:
            print(f"  warning: could not read {geojson_path.name}: {e}")
            continue

        links: dict[str, dict] = {}
        for feature in gj.get("features", []):
            props = feature.get("properties") or {}
            if (props.get("type") or "").upper() != "BUILDING":
                continue
            bname = (props.get("name") or "").strip()
            if bname and bname.lower() in name_to_kanka:
                links[bname] = name_to_kanka[bname.lower()]

        out_path = towns_dir / f"{town_name}_links.json"
        out_path.write_text(json.dumps(links, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {town_name}: {len(links)} building(s) linked to Kanka")


def load_static_forests() -> list[dict]:
    """Return forest polygon objects from data/forests_static.json, or [] if absent."""
    path = OUTPUT_DIR / "forests_static.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_static_roads() -> list[dict]:
    """Return road objects from data/roads_static.json, or [] if the file doesn't exist."""
    path = OUTPUT_DIR / "roads_static.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    if not TOKEN:
        print("ERROR: KANKA_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    print("Fetching Kanka locations...")
    raw_locations = fetch_all_locations()
    print(f"Found {len(raw_locations)} location entities. Fetching attributes...")

    print("\nBuilding town Kanka links and index...")
    build_town_kanka_links(raw_locations)
    towns_dir = OUTPUT_DIR / "towns"
    if towns_dir.exists():
        town_names = sorted(p.stem for p in towns_dir.glob("*.geojson"))
        (towns_dir / "index.json").write_text(
            json.dumps(town_names, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  Wrote towns/index.json ({len(town_names)} town(s): {', '.join(town_names)})")

    locations_out = []
    roads_out = []
    landmarks_out = []
    parties_out = []

    for loc in raw_locations:
        entity_id = loc.get("entity_id") or loc.get("id")
        child = loc.get("child") or {}
        name = loc.get("name") or child.get("name") or "Unknown"
        loc_id = child.get("id") or loc.get("id")

        print(f"  [{entity_id}] {name}")
        attrs = fetch_attributes(entity_id)

        # Detect year-ranged position attributes: map_lat_STARTYEAR_ENDYEAR
        ranged_positions = []
        for attr_name, attr_value in attrs.items():
            m = re.match(r'^map_lat_(\d+)_(\d+)$', attr_name)
            if m:
                r_start, r_end = int(m.group(1)), int(m.group(2))
                lon_key = f"map_lon_{r_start}_{r_end}"
                if lon_key in attrs:
                    try:
                        ranged_positions.append((float(attr_value), float(attrs[lon_key]), r_start, r_end))
                    except ValueError:
                        print(f"    warning: invalid ranged lat/lon for '{name}' ({attr_name}) — skipped")

        has_ranged_point = len(ranged_positions) > 0
        # Suppress plain map_lat/map_lon if ranged versions exist to avoid double display
        has_point = ("map_lat" in attrs and "map_lon" in attrs) and not has_ranged_point
        has_road = "map_coords" in attrs

        if not has_point and not has_ranged_point and not has_road:
            # No map data — skip
            continue

        # Shared fields
        try:
            start_year = int(attrs["map_start_year"]) if "map_start_year" in attrs else None
        except ValueError:
            start_year = None
        try:
            end_year = int(attrs["map_end_year"]) if "map_end_year" in attrs else None
        except ValueError:
            end_year = None

        colour = attrs.get("map_color", "")
        kanka_url = f"https://kanka.io/en-US/campaign/{CAMPAIGN}/locations/{loc_id}"
        image_url = child.get("image_full") or child.get("image_thumb") or ""
        entry_html = child.get("entry_parsed") or child.get("entry") or ""

        map_layer = attrs.get("map_layer", "").strip().lower()
        is_landmark = map_layer in ("landmarks", "landmark")

        try:
            line_weight = int(attrs.get("map_line_weight", "1"))
        except ValueError:
            line_weight = 1

        try:
            weight_start = float(attrs["map_weight_start"]) if "map_weight_start" in attrs else None
        except (ValueError, TypeError):
            weight_start = None
        try:
            weight_end = float(attrs["map_weight_end"]) if "map_weight_end" in attrs else None
        except (ValueError, TypeError):
            weight_end = None

        if has_ranged_point:
            for r_lat, r_lon, r_start, r_end in sorted(ranged_positions, key=lambda x: x[2]):
                obj = {
                    "name": name,
                    "lat": r_lat,
                    "lon": r_lon,
                    "icon": attrs.get("map_icon", "town"),
                    "color": colour,
                    "plane": attrs.get("map_plane", "overworld"),
                    "map_type": attrs.get("map_type", "town"),
                    "image_url": image_url,
                    "entry_html": entry_html,
                    "kanka_url": kanka_url,
                    "visible_to_players": attrs.get("map_visible_to_players", "true").lower() != "false",
                    "startYear": r_start,
                    "endYear": r_end,
                }
                locations_out.append(obj)

        if has_point:
            try:
                lat = float(attrs["map_lat"])
                lon = float(attrs["map_lon"])
            except ValueError:
                print(f"    warning: invalid map_lat/map_lon for '{name}' — skipped")
                continue

            if is_landmark:
                obj = {
                    "name": name,
                    "type": attrs.get("map_type", "marker"),
                    "coords": f"{lat},{lon}",
                    "color": colour,
                    "icon": attrs.get("map_icon", "town"),
                    "line_weight": line_weight,
                    "description": entry_html,
                    "url": image_url,
                    "image_url": image_url,
                    "kanka_url": kanka_url,
                }
                if start_year is not None:
                    obj["startYear"] = start_year
                if end_year is not None:
                    obj["endYear"] = end_year
                landmarks_out.append(obj)
            else:
                obj = {
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "icon": attrs.get("map_icon", "town"),
                    "color": colour,
                    "plane": attrs.get("map_plane", "overworld"),
                    "map_type": attrs.get("map_type", "town"),
                    "image_url": image_url,
                    "entry_html": entry_html,
                    "kanka_url": kanka_url,
                    "visible_to_players": attrs.get("map_visible_to_players", "true").lower() != "false",
                }
                if start_year is not None:
                    obj["startYear"] = start_year
                if end_year is not None:
                    obj["endYear"] = end_year
                locations_out.append(obj)

        if has_road:
            coords_raw = attrs["map_coords"]
            road_type = attrs.get("map_road_type", "")
            if not road_type:
                road_type = "sea" if colour_is_sea(colour) else "land"

            if is_landmark:
                obj = {
                    "name": name,
                    "type": attrs.get("map_type", "polyline"),
                    "coords": coords_raw,
                    "color": colour,
                    "line_weight": line_weight,
                    "description": entry_html,
                    "url": image_url,
                    "image_url": image_url,
                    "kanka_url": kanka_url,
                }
                if weight_start is not None:
                    obj["weight_start"] = weight_start
                if weight_end is not None:
                    obj["weight_end"] = weight_end
                if start_year is not None:
                    obj["startYear"] = start_year
                if end_year is not None:
                    obj["endYear"] = end_year
                landmarks_out.append(obj)
            else:
                obj = {
                    "name": name,
                    "road_type": road_type,
                    "color": colour,
                    "coords": coords_raw,
                    "line_weight": line_weight,
                    "kanka_url": kanka_url,
                }
                if start_year is not None:
                    obj["startYear"] = start_year
                if end_year is not None:
                    obj["endYear"] = end_year
                roads_out.append(obj)

    # Merge static forests
    forests_out = load_static_forests()
    if forests_out:
        print(f"\nLoaded {len(forests_out)} forest(s) from data/forests_static.json")

    # Merge static roads (geometry-only roads stored in the repo, not Kanka)
    static_roads = load_static_roads()
    if static_roads:
        roads_out.extend(static_roads)
        print(f"\nLoaded {len(static_roads)} road(s) from data/roads_static.json")

    # Fetch organisations for party positions
    print("\nFetching Kanka organisations for party positions...")
    raw_orgs = fetch_all_organisations()
    print(f"Found {len(raw_orgs)} organisation entities. Fetching attributes...")

    for org in raw_orgs:
        entity_id = org.get("entity_id") or org.get("id")
        child = org.get("child") or {}
        name = org.get("name") or child.get("name") or "Unknown"
        org_id = child.get("id") or org.get("id")

        print(f"  [{entity_id}] {name}")
        attrs = fetch_attributes(entity_id)

        if "map_lat" not in attrs or "map_lon" not in attrs:
            continue

        try:
            lat = float(attrs["map_lat"])
            lon = float(attrs["map_lon"])
        except ValueError:
            print(f"    warning: invalid map_lat/map_lon for organisation '{name}' — skipped")
            continue

        try:
            start_year = int(attrs["map_start_year"]) if "map_start_year" in attrs else None
        except ValueError:
            start_year = None
        try:
            end_year = int(attrs["map_end_year"]) if "map_end_year" in attrs else None
        except ValueError:
            end_year = None

        image_url = child.get("image_full") or child.get("image_thumb") or ""
        entry_html = child.get("entry_parsed") or child.get("entry") or ""
        kanka_url = f"https://kanka.io/en-US/campaign/{CAMPAIGN}/organisations/{org_id}"

        party_obj = {
            "name": name,
            "lat": lat,
            "lon": lon,
            "icon": attrs.get("map_icon", "town"),
            "color": attrs.get("map_color", ""),
            "description": entry_html,
            "image_url": image_url,
            "kanka_url": kanka_url,
        }
        if start_year is not None:
            party_obj["startYear"] = start_year
        if end_year is not None:
            party_obj["endYear"] = end_year
        parties_out.append(party_obj)

    print(f"  {len(parties_out)} organisation(s) with map coordinates")

    # Build routing graph — pass town positions so road endpoints snap by name
    town_lookup = {loc["name"].lower(): (loc["lat"], loc["lon"]) for loc in locations_out}
    print(f"\nBuilding road graph from {len(roads_out)} road entities ({len(town_lookup)} towns for name-snapping)...")
    nodes, edges, warnings = build_road_graph(roads_out, town_lookup=town_lookup)
    print(f"  {len(nodes)} nodes, {len(edges)} edges")

    if warnings:
        print(f"\nConnectivity warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  ! {w}")
    else:
        print("  No connectivity issues detected.")

    # Write outputs
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    map_data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "locations": locations_out,
        "roads": roads_out,
        "landmarks": landmarks_out,
        "parties": parties_out,
        "forests": forests_out,
    }
    (OUTPUT_DIR / "map_data.json").write_text(
        json.dumps(map_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"\nWrote data/map_data.json "
        f"({len(locations_out)} locations, {len(roads_out)} roads, "
        f"{len(landmarks_out)} landmarks, {len(parties_out)} parties)"
    )

    road_graph = {"nodes": nodes, "edges": edges}
    (OUTPUT_DIR / "road_graph.json").write_text(
        json.dumps(road_graph, indent=2), encoding="utf-8"
    )
    print(f"Wrote data/road_graph.json ({len(nodes)} nodes, {len(edges)} edges)")


if __name__ == "__main__":
    main()
