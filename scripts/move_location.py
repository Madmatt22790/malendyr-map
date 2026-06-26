"""One-off script to move a location's map coordinates in Kanka."""
import os, sys, time, requests

CAMPAIGN    = 347078
LOCATION_ID = int(sys.argv[1])
NEW_LAT     = float(sys.argv[2])
NEW_LON     = float(sys.argv[3])
TOKEN       = os.environ.get("KANKA_TOKEN", "")

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

print(f"Fetching location {LOCATION_ID}...")
loc = api_get(f"{BASE}/locations/{LOCATION_ID}")
entity_id = loc["data"]["entity_id"]
name = loc["data"]["name"]
print(f"  {name} — entity_id = {entity_id}")

print("Fetching attributes...")
attrs = api_get(f"{BASE}/entities/{entity_id}/attributes", {"per_page": 100})

targets = {"map_lat": str(NEW_LAT), "map_lon": str(NEW_LON)}
for attr in attrs["data"]:
    if attr.get("name") in targets:
        new_val = targets[attr["name"]]
        print(f"  {attr['name']}: {attr.get('value')} -> {new_val}")
        api_patch(f"{BASE}/entities/{entity_id}/attributes/{attr['id']}", {"value": new_val, "name": attr["name"]})
        print(f"  Done.")

print(f"{name} coordinates updated to ({NEW_LAT}, {NEW_LON}).")
