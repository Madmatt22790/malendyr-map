"""One-off script to move Buhrora's map coordinates to Oakway Junction point."""
import os, time, requests

CAMPAIGN   = 347078
LOCATION_ID = 2127450
NEW_LAT    = -44.707531
NEW_LON    = 67.758974
TOKEN      = os.environ.get("KANKA_TOKEN", "")

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})

def api_get(url, params=None):
    for attempt in range(5):
        time.sleep(1.5)
        r = session.get(url, params=params or {})
        if r.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"  429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()

def api_patch(url, payload):
    for attempt in range(5):
        time.sleep(1.5)
        r = session.patch(url, json=payload)
        if r.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"  429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()

BASE = f"https://api.kanka.io/1.0/campaigns/{CAMPAIGN}"

# Get entity_id for this location
print("Fetching location...")
loc = api_get(f"{BASE}/locations/{LOCATION_ID}")
entity_id = loc["data"]["entity_id"]
print(f"  entity_id = {entity_id}")

# Fetch attributes
print("Fetching attributes...")
attrs = api_get(f"{BASE}/entities/{entity_id}/attributes", {"per_page": 100})
attr_list = attrs["data"]

targets = {"map_lat": str(NEW_LAT), "map_lon": str(NEW_LON)}
for attr in attr_list:
    name = attr.get("name", "")
    if name in targets:
        attr_id = attr["id"]
        new_val = targets[name]
        print(f"  Updating {name} (id {attr_id}): {attr.get('value')} -> {new_val}")
        api_patch(f"{BASE}/entities/{entity_id}/attributes/{attr_id}", {"value": new_val, "name": name})
        print(f"  Done.")

print("Buhrora coordinates updated.")
