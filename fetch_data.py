"""Build the local POI cache (data/pois.csv) from open datasets.

Sources:
  - Schools (primary/secondary): DfE Get Information about Schools (GIAS)
  - GP practices: NHS ODS epraccur (postcodes geocoded via postcodes.io)
  - Hospitals & supermarkets: OpenStreetMap via Overpass API

Run:  py fetch_data.py
Output columns: category,name,lat,lon
"""

import csv
import io
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from bng_latlon import OSGB36toWGS84

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "postcode-service-checker/0.1 (internal MVP)"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
POSTCODES_IO = "https://api.postcodes.io/postcodes"


def fetch_gias_schools():
    """GIAS publishes a full daily CSV named with the date; try the last few days."""
    df = None
    for days_back in range(0, 7):
        d = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
        url = f"https://ea-edubase-api-prod.azurewebsites.net/edubase/downloads/public/edubasealldata{d}.csv"
        print(f"GIAS: trying {url}")
        r = SESSION.get(url, timeout=300)
        if r.status_code == 200 and len(r.content) > 1_000_000:
            df = pd.read_csv(io.BytesIO(r.content), encoding="cp1252", low_memory=False)
            break
    if df is None:
        raise RuntimeError("Could not download GIAS schools data")

    df = df[df["EstablishmentStatus (name)"] == "Open"]
    df = df.dropna(subset=["Easting", "Northing"])
    df = df[(df["Easting"] > 0) & (df["Northing"] > 0)]

    phase = df["PhaseOfEducation (name)"]
    primary = df[phase.isin(["Primary", "Middle deemed primary", "All-through"])]
    secondary = df[phase.isin(["Secondary", "Middle deemed secondary", "All-through"])]

    rows = []
    for cat, sub in [("primary_school", primary), ("secondary_school", secondary)]:
        for _, r_ in sub.iterrows():
            try:
                lat, lon = OSGB36toWGS84(float(r_["Easting"]), float(r_["Northing"]))
            except Exception:
                continue
            rows.append((cat, str(r_["EstablishmentName"]), round(lat, 6), round(lon, 6)))
    print(f"GIAS: {len(rows)} school rows ({len(primary)} primary, {len(secondary)} secondary)")
    return rows


def geocode_postcodes_bulk(postcodes):
    """postcodes.io bulk lookup: returns {postcode: (lat, lon)}."""
    out = {}
    pcs = list(postcodes)
    for i in range(0, len(pcs), 100):
        chunk = pcs[i : i + 100]
        for attempt in range(3):
            try:
                r = SESSION.post(POSTCODES_IO, json={"postcodes": chunk}, timeout=60)
                r.raise_for_status()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"postcodes.io retry after error: {e}")
                time.sleep(2 * (attempt + 1))
        for item in r.json()["result"]:
            res = item.get("result")
            if res and res.get("latitude") is not None:
                out[item["query"]] = (res["latitude"], res["longitude"])
        if (i // 100) % 10 == 0:
            print(f"geocoded {min(i + 100, len(pcs))}/{len(pcs)} postcodes")
    return out


def fetch_gp_practices():
    """NHS ODS DSE epraccur report: active GP practices (role RO76), geocoded by postcode."""
    url = "https://www.odsdatasearchandexport.nhs.uk/api/getReport?report=epraccur"
    print(f"GPs: downloading {url}")
    r = SESSION.get(url, timeout=600)
    r.raise_for_status()
    practices = []  # (name, postcode)
    for row in csv.reader(io.StringIO(r.content.decode("utf-8-sig", errors="replace"))):
        # DSE epraccur has no header; fixed positions:
        # 1=name, 9=postcode, 12=status (ACTIVE), 25=role codes ('|'-separated, RO76=GP practice)
        if len(row) > 25 and row[12] == "ACTIVE" and "RO76" in row[25].split("|"):
            practices.append((row[1].title(), row[9].strip().upper()))
    print(f"GPs: {len(practices)} active practices, geocoding postcodes...")
    coords = geocode_postcodes_bulk({pc for _, pc in practices})
    rows = []
    for name, pc in practices:
        if pc in coords:
            lat, lon = coords[pc]
            rows.append(("gp", name, lat, lon))
    print(f"GPs: {len(rows)} geocoded")
    return rows


def fetch_overpass(category, osm_filter):
    """Fetch POIs for Great Britain from Overpass."""
    query = f"""
    [out:json][timeout:600];
    area["ISO3166-1"="GB"][admin_level=2]->.uk;
    nwr[{osm_filter}](area.uk);
    out center;
    """
    print(f"{category}: querying Overpass ({osm_filter})...")
    for attempt in range(3):
        try:
            r = SESSION.post(OVERPASS_URL, data={"data": query}, timeout=900)
            r.raise_for_status()
            elements = r.json()["elements"]
            break
        except Exception as e:
            if attempt == 2:
                raise
            print(f"Overpass retry after error: {e}")
            time.sleep(30)
    rows = []
    for el in elements:
        if "lat" in el:
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        name = el.get("tags", {}).get("name", "(unnamed)")
        rows.append((category, name, round(lat, 6), round(lon, 6)))
    print(f"{category}: {len(rows)} rows")
    return rows


def main():
    all_rows = []
    all_rows += fetch_gias_schools()
    all_rows += fetch_gp_practices()
    all_rows += fetch_overpass("hospital", '"amenity"="hospital"')
    all_rows += fetch_overpass("supermarket", '"shop"="supermarket"')

    out = DATA_DIR / "pois.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "name", "lat", "lon"])
        w.writerows(all_rows)
    print(f"\nWrote {len(all_rows)} POIs to {out}")
    df = pd.DataFrame(all_rows, columns=["category", "name", "lat", "lon"])
    print(df["category"].value_counts())


if __name__ == "__main__":
    main()
