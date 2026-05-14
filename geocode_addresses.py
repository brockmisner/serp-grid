"""
Geocode a SERP grid CSV from street addresses → lat/lng using OSM Nominatim.
Free, no API key, but rate-limited (~1.1 s per request).

Usage:
    python3 geocode_addresses.py input.csv [output.csv] [failed.csv]

Input  CSV columns: keyword, label  (label = street address)
Output CSV columns: keyword, lat, lng, label
Failed CSV columns: keyword, label, reason

If the full address can't be matched, falls back to the "City, ST" tail.
"""

import csv
import re
import sys
import time

import requests

INPUT  = sys.argv[1] if len(sys.argv) > 1 else "serp_grid_template.csv"
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else "serp_grid_geocoded.csv"
FAILED = sys.argv[3] if len(sys.argv) > 3 else "serp_grid_failed.csv"

sess = requests.Session()
sess.headers["User-Agent"] = "serp-grid-geocoder/1.0"


def geocode(query: str):
    r = sess.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
        timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    return (float(j[0]["lat"]), float(j[0]["lon"])) if j else None


def city_state_tail(address: str):
    """Extract 'City, ST' from the end of an address line."""
    m = re.search(r",\s*([^,]+),\s*([A-Z]{2})\s*$", address.strip())
    return f"{m.group(1).strip()}, {m.group(2)}" if m else None


with open(INPUT) as f:
    rows = list(csv.DictReader(f))

print(f"Geocoding {len(rows)} addresses (~{len(rows)*1.1/60:.1f} min)...")
ok, miss = [], []

for i, r in enumerate(rows):
    addr = (r.get("label") or "").strip()
    coords, why = None, ""

    try:
        coords = geocode(addr)
        time.sleep(1.1)
        if not coords:
            cs = city_state_tail(addr)
            if cs:
                coords = geocode(cs)
                time.sleep(1.1)
                why = "fell back to city/state"
    except Exception as e:
        miss.append({"keyword": r.get("keyword", ""), "label": addr, "reason": str(e)})
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(rows)}  hits={len(ok)} miss={len(miss)}")
        continue

    if coords:
        ok.append({
            "keyword": r.get("keyword", ""),
            "lat": coords[0],
            "lng": coords[1],
            "label": addr + (f"  [{why}]" if why else ""),
        })
    else:
        miss.append({"keyword": r.get("keyword", ""), "label": addr, "reason": "no match"})

    if (i + 1) % 25 == 0:
        print(f"  {i+1}/{len(rows)}  hits={len(ok)} miss={len(miss)}")

with open(OUTPUT, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["keyword", "lat", "lng", "label"])
    w.writeheader()
    w.writerows(ok)

if miss:
    with open(FAILED, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["keyword", "label", "reason"])
        w.writeheader()
        w.writerows(miss)

print(f"\n✓ {len(ok)}/{len(rows)} geocoded → {OUTPUT}")
print(f"✗ {len(miss)} failed       → {FAILED if miss else '(none)'}")
