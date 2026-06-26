"""
One-off script: set map_start_year and map_end_year for the western-island towns.
"""
import os, time, requests

CAMPAIGN = 347078
BASE_URL  = f"https://api.kanka.io/1.0/campaigns/{CAMPAIGN}"
TOKEN     = os.environ["KANKA_TOKEN"]
RATE_DELAY = 2.0
RETRY_DELAY = 60

TOWNS = [
    # (entity_id, name, start_year, end_year)
    (9442538, "Encephia",   350,  2657),
    (9442462, "Ocring",     430,  2657),
    (9442476, "Duport",     520,  2657),
    (9442474, "Gopburn",    590,  2657),
    (9442478, "Solk",       650,  2657),
    (9442460, "Vregan",     710,  2657),
    (9442461, "Vroycaster", 780,  2657),
    (9442458, "Buhrora",    850,  2657),
    (9442463, "Khwek",      920,  2657),
    (9442464, "Khila",      990,  2657),
    (9442592, "Wrille",    1060,  2657),
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
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


for entity_id, name, start, end in TOWNS:
    print(f"\n{name} (entity {entity_id})")
    data = api("get", f"/entities/{entity_id}/attributes")
    existing = {a["name"]: a for a in data.get("data", [])}

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
