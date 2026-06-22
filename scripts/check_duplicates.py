import requests
from collections import Counter

TOWNS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbzSbU-10nUV-odUN1kUZLquAa5olKwfa9vR5791-DJeSbz3lpEkKzJtCdtdIxC7RWPmqg/exec"
)

r = requests.get(TOWNS_URL, timeout=30)
rows = r.json()
names = [str(row.get("name") or row.get("Name") or "").strip() for row in rows]
dups = {n: c for n, c in Counter(names).items() if c > 1 and n}

print("Duplicate names in towns sheet (%d found):" % len(dups))
for name in sorted(dups):
    matches = [row for row in rows if str(row.get("name") or row.get("Name") or "").strip() == name]
    print("\n  %r (%dx):" % (name, len(matches)))
    for m in matches:
        print("    lat=%-15s lon=%-15s start=%-6s end=%s" % (
            m.get("lat", ""), m.get("lon", ""),
            m.get("startYear", ""), m.get("endYear", "")
        ))
