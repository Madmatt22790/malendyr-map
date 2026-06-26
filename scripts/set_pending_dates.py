"""
One-off script: set map_start_year and map_end_year for towns still at year 1.
Covers the 7 new western-island towns, Weigh, Vloystin, Abterra, and Chitol.
"""
import os, time, requests

CAMPAIGN = 347078
BASE_URL  = f"https://api.kanka.io/1.0/campaigns/{CAMPAIGN}"
TOKEN     = os.environ["KANKA_TOKEN"]
RATE_DELAY  = 2.0
RETRY_DELAY = 60

# (name, start_year, end_year)
TOWNS = [
    # Western island -- new coastal
    ("Azloygend",                470, 2657),
    ("Zesledo",                  530, 2657),
    ("Visrora",                  600, 2657),
    ("Weigh",                    700, 2657),
    # Western island -- new inland
    ("Oakhaven",                 830, 2657),
    ("Acosey",                   960, 2657),
    ("Areshull",                1020, 2657),
    ("Ocknard",                 1130, 2657),
    # Other continents
    ("Vloystin",                2247, 2657),
    ("Abterra - The Purple City", 2206, 2657),
    ("Chitol",                  2340, 2657),
]

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})


def api(method, endpoint, **kwargs):
    for attempt in range(5):
        time.sleep(RATE_DELAY)
        r = getattr(session, method)(f"{BASE_URL}{endpoint}", **kwargs)
        if r.status_code == 429:
            wait = RETRY_DELAY * (attempt + 1)
            print(f"  429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            wait = 10 * (attempt + 1)
            print(f"  {r.status_code} server error — waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


# Build name -> entity_id lookup from all location pages
print("Building location lookup...")
name_to_entity = {}
page = 1
while True:
    data = api("get", "/locations", params={"page": page, "per_page": 45})
    for loc in data.get("data", []):
        name_to_entity[loc["name"]] = loc["entity_id"]
    if not data.get("links", {}).get("next"):
        break
    page += 1
    print(f"  fetched page {page - 1} ({len(name_to_entity)} so far)")
print(f"Found {len(name_to_entity)} locations.\n")

for name, start, end in TOWNS:
    entity_id = name_to_entity.get(name)
    if not entity_id:
        print(f"WARNING: '{name}' not found in Kanka, skipping.")
        continue

    print(f"{name} (entity {entity_id})")
    attrs = api("get", f"/entities/{entity_id}/attributes")
    existing = {a["name"]: a for a in attrs.get("data", [])}

    for attr_name, attr_val in [("map_start_year", str(start)), ("map_end_year", str(end))]:
        if attr_name in existing:
            current = str(existing[attr_name].get("value") or "").strip()
            if current == attr_val:
                print(f"  {attr_name} = {attr_val}  (already correct)")
            else:
                print(f"  UPDATE {attr_name}: {current!r} -> {attr_val!r}")
                api("patch", f"/entities/{entity_id}/attributes/{existing[attr_name]['id']}",
                    json={"value": attr_val})
        else:
            print(f"  CREATE {attr_name} = {attr_val}")
            api("post", f"/entities/{entity_id}/attributes",
                json={"name": attr_name, "value": attr_val, "type": "", "is_private": False})

print("\nDone.")
