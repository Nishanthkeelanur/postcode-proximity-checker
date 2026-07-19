"""Core logic: for each postcode, is each service category within N minutes' drive?

Strategy per postcode:
  1. Geocode via postcodes.io (bulk).
  2. Per category, shortlist the nearest CANDIDATES POIs by straight-line distance
     (anything whose nearest candidate is beyond MAX_CROW_KM is an automatic No).
  3. One OSRM /table request per postcode gives drive times to all candidates;
     take the per-category minimum.

Usage (CLI test):  py checker.py "SW1A 1AA" "YO62 4LB"
"""

import math
import sys
import time
from pathlib import Path

import pandas as pd
import requests

DATA_FILE = Path(__file__).parent / "data" / "pois.csv"
OSRM_URL = "https://router.project-osrm.org"
POSTCODES_IO = "https://api.postcodes.io/postcodes"

CATEGORIES = ["gp", "hospital", "primary_school", "secondary_school", "supermarket"]
CATEGORY_LABELS = {
    "gp": "GP",
    "hospital": "Hospital",
    "primary_school": "Primary school",
    "secondary_school": "Secondary school",
    "supermarket": "Supermarket",
}

CANDIDATES = 5      # nearest-by-crow POIs per category sent to OSRM
MAX_CROW_KM = 45.0  # beyond this straight-line distance, can't be <=25 min drive

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "postcode-service-checker/0.1 (internal MVP)"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_pois():
    if not DATA_FILE.exists():
        raise FileNotFoundError(
            f"{DATA_FILE} not found - run 'py fetch_data.py' first to build the POI cache."
        )
    df = pd.read_csv(DATA_FILE)
    return {cat: df[df["category"] == cat].reset_index(drop=True) for cat in CATEGORIES}


def geocode(postcodes):
    """Bulk geocode via postcodes.io. Returns {input_postcode: (lat, lon) or None}."""
    out = {}
    pcs = [str(p).strip() for p in postcodes if str(p).strip()]
    for i in range(0, len(pcs), 100):
        chunk = pcs[i : i + 100]
        for attempt in range(3):
            try:
                r = SESSION.post(POSTCODES_IO, json={"postcodes": chunk}, timeout=60)
                r.raise_for_status()
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        for item in r.json()["result"]:
            res = item.get("result")
            if res and res.get("latitude") is not None:
                out[item["query"]] = (res["latitude"], res["longitude"])
            else:
                out[item["query"]] = None
    return out


def nearest_candidates(pois_cat, lat, lon, n=CANDIDATES):
    """Nearest n POIs of one category by straight-line distance."""
    d = pois_cat.apply(lambda r: haversine_km(lat, lon, r["lat"], r["lon"]), axis=1)
    idx = d.nsmallest(n).index
    return pois_cat.loc[idx], d.loc[idx]


def osrm_table(origin, destinations, retries=3):
    """Drive durations (seconds) from origin (lat, lon) to each destination. None on failure."""
    coords = ";".join(
        [f"{origin[1]:.6f},{origin[0]:.6f}"] + [f"{lon:.6f},{lat:.6f}" for lat, lon in destinations]
    )
    url = f"{OSRM_URL}/table/v1/driving/{coords}"
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params={"sources": "0", "annotations": "duration"}, timeout=60)
            r.raise_for_status()
            j = r.json()
            if j.get("code") == "Ok":
                return j["durations"][0][1:]
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def check_postcode(pois, lat, lon, threshold_min=25):
    """Returns {category: {"within": bool|None, "minutes": float|None, "nearest_name": str}}."""
    result = {}
    dest_coords, dest_meta = [], []  # meta: (category, name)
    for cat in CATEGORIES:
        cand, crow = nearest_candidates(pois[cat], lat, lon)
        if crow.min() > MAX_CROW_KM:
            result[cat] = {"within": False, "minutes": None, "nearest_name": ""}
            continue
        for _, row in cand.iterrows():
            dest_coords.append((row["lat"], row["lon"]))
            dest_meta.append((cat, row["name"]))

    if dest_coords:
        durations = osrm_table((lat, lon), dest_coords)
        if durations is None:
            for cat in CATEGORIES:
                if cat not in result:
                    result[cat] = {"within": None, "minutes": None, "nearest_name": "routing error"}
            return result
        best = {}
        for (cat, name), dur in zip(dest_meta, durations):
            if dur is not None and (cat not in best or dur < best[cat][0]):
                best[cat] = (dur, name)
        for cat in CATEGORIES:
            if cat in result:
                continue
            if cat in best:
                mins = best[cat][0] / 60.0
                result[cat] = {
                    "within": mins <= threshold_min,
                    "minutes": round(mins, 1),
                    "nearest_name": best[cat][1],
                }
            else:
                result[cat] = {"within": None, "minutes": None, "nearest_name": "unroutable"}
    return result


def run_batch(postcodes, threshold_min=25, progress_cb=None, pois=None):
    """Check a list of postcodes. Returns a DataFrame, one row per input postcode."""
    if pois is None:
        pois = load_pois()
    coords = geocode(postcodes)
    rows = []
    for i, pc in enumerate([str(p).strip() for p in postcodes if str(p).strip()]):
        c = coords.get(pc)
        row = {"postcode": pc}
        if c is None:
            row["status"] = "invalid postcode"
            for cat in CATEGORIES:
                row[CATEGORY_LABELS[cat]] = ""
                row[f"{CATEGORY_LABELS[cat]} (mins)"] = None
            row["all_within"] = ""
        else:
            res = check_postcode(pois, c[0], c[1], threshold_min)
            row["status"] = "ok"
            verdicts = []
            for cat in CATEGORIES:
                w = res[cat]["within"]
                row[CATEGORY_LABELS[cat]] = "Yes" if w else ("No" if w is False else "?")
                row[f"{CATEGORY_LABELS[cat]} (mins)"] = res[cat]["minutes"]
                verdicts.append(w)
            row["all_within"] = "Yes" if all(v is True for v in verdicts) else "No"
            time.sleep(0.5)  # be polite to the public OSRM server
        rows.append(row)
        if progress_cb:
            progress_cb(i + 1, pc)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pcs = sys.argv[1:] or ["SW1A 1AA"]
    df = run_batch(pcs, progress_cb=lambda i, pc: print(f"[{i}/{len(pcs)}] {pc}"))
    print(df.to_string(index=False))
