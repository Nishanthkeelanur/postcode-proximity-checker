# Service Proximity Checker (MVP)

Internal tool for bulk-checking UK postcodes: for each postcode, is a **GP,
hospital, primary school, secondary school and supermarket** each within a
drive-time threshold (default **25 minutes**)? Output is a Yes/No per category
plus the actual drive time to the nearest site, downloadable as CSV.

Built entirely on free/open data and services — no API keys, no paid tiers.

---

## Quick start

```
py -m pip install -r requirements.txt
py -m streamlit run app.py            # launch the web UI
```

A snapshot of the POI cache (`data/pois.csv`) ships with the repo, so this runs
out of the box. To refresh the data (~5 min, do monthly): `py fetch_data.py`.

Quick CLI check without the UI:

```
py checker.py "SW1A 1AA" "M1 1AE" "YO62 4LB"
```

## Project structure

| File | Purpose |
|---|---|
| `fetch_data.py` | Downloads the open datasets and builds the local POI cache (`data/pois.csv`). Run once, then re-run monthly to refresh. |
| `checker.py` | Core logic: geocoding, candidate shortlisting, OSRM drive times, Yes/No verdicts. Importable module + CLI. |
| `app.py` | Streamlit web UI: paste postcodes or upload a CSV, adjust the threshold, view/download results. |
| `data/pois.csv` | POI cache snapshot (`category,name,lat,lon`), committed so deployments ship with data. Refresh with `fetch_data.py`. |

## How a check works (pipeline)

For each input postcode:

1. **Geocode** — postcodes are sent in bulk (100 per request) to
   [postcodes.io](https://postcodes.io) (free, no key). Invalid postcodes are
   flagged in the output rather than failing the batch.
2. **Shortlist** — for each of the 5 categories, the nearest **5 sites by
   straight-line (haversine) distance** are picked from the local POI cache.
   If even the nearest site is over **45 km** straight-line, the category is an
   automatic **No** (you cannot drive 45 km in 25 min), skipping the routing call.
3. **Route** — one request to the OSRM `/table` endpoint returns drive times
   from the postcode to all shortlisted sites (up to 25) at once.
4. **Verdict** — the per-category minimum drive time is compared to the
   threshold. `all_within` = Yes only if every category passes.

The shortlisting step is what makes bulk checking cheap: we never route against
all 39k POIs, only ~25 candidates per postcode.

## Data sources (all free/open)

| Category | Source | Notes |
|---|---|---|
| Primary schools | DfE **Get Information about Schools** (GIAS) daily CSV | Open establishments; phases Primary, Middle-deemed-primary, All-through. Easting/Northing converted to lat/lon (`bng_latlon`). |
| Secondary schools | Same GIAS file | Phases Secondary, Middle-deemed-secondary, All-through. |
| GP practices | NHS **ODS Data Search & Export** `epraccur` report | The legacy `files.digital.nhs.uk/.../epraccur.zip` is retired (403). New endpoint: `https://www.odsdatasearchandexport.nhs.uk/api/getReport?report=epraccur`. Filtered to status `ACTIVE` + role `RO76` (GP practice) ⇒ ~6.5k practices. Postcodes geocoded via postcodes.io. |
| Hospitals | OpenStreetMap via Overpass (`amenity=hospital`) | Any hospital site, not A&E specifically. |
| Supermarkets | OpenStreetMap via Overpass (`shop=supermarket`) | Geolytix Retail Points is a cleaner alternative if OSM quality becomes an issue. |

Cache counts at last build (Jul 2026): 16,831 primary schools, 3,409 secondary
schools, 6,568 GPs, 1,829 hospitals, 10,390 supermarkets = **39,027 POIs**.

## Routing

Drive times come from the **public OSRM demo server**
(`router.project-osrm.org`) — free-flow traffic (no live-traffic modelling),
no SLA, community rate limits. The checker sleeps 0.5 s between postcodes to be
polite, so throughput is roughly **2 postcodes/second** — fine for batches of
tens to a few hundred.

All external calls (postcodes.io, Overpass, OSRM) have retry-with-backoff; a
routing failure marks the affected postcode `?` rather than killing the batch.

## Key design decisions

- **Local POI cache, remote routing** — POI data changes slowly (monthly
  refresh is plenty) so it's cached to CSV; drive times are computed on demand.
- **Straight-line prefilter before routing** — turns an O(postcodes × 39k POIs)
  problem into O(postcodes × 25), which is what makes the free OSRM server viable.
- **45 km crow-flies cutoff** — generous upper bound for a 25-min drive
  (~108 km/h average); avoids wasted routing calls in remote areas.
- **Free-flow times, not rush hour** — acceptable for a threshold question; if
  stakeholders need "25 min in traffic", that's the one thing requiring a paid
  API (TravelTime / Google Routes).

## Verified behaviour

Tested end-to-end via CLI and the web UI:

- **SW1A 1AA** (central London) / **M1 1AE** (Manchester) → all categories Yes.
- **LL23 7YF** (rural Wales) / **IV27 4HH** (Scottish Highlands) → correctly
  fail one or more categories.
- **Invalid postcode** → flagged `invalid postcode` in the results, batch continues.

## Known limitations

- **England-only GP and school registers.** GIAS and ODS epraccur cover
  England; Scottish/Welsh/NI postcodes will under-report GPs and schools
  (hospitals and supermarkets via OSM are UK-wide). Equivalent registers exist
  for the devolved nations if needed.
- **"Hospital" is broad** — any OSM `amenity=hospital` site, including
  community/specialist hospitals. Agree the definition (e.g. A&E only) with
  stakeholders; NHS ODS data can support a stricter filter.
- **Public OSRM demo server** — no SLA, free-flow only, rate-limited. The main
  thing to replace for production.
- **Drive times are free-flow**, so real-world peak times will be longer; use a
  lower threshold (e.g. 22 min) if you want a conservative margin.

## Production roadmap

1. **Self-host OSRM** (Docker + Great Britain OSM extract, ~2 GB). Point
   `OSRM_URL` in `checker.py` at it — removes the rate limit and the external
   dependency; batches run orders of magnitude faster.
2. **Add Scotland/Wales/NI** school and GP registers for full UK coverage.
3. **Precompute** results for all ~1.8M UK postcodes overnight (feasible once
   OSRM is local) so every lookup becomes instant and offline.
4. **Offline geocoding** via the ONS Postcode Directory to drop the
   postcodes.io dependency.
5. **Scheduled data refresh** (monthly) for the POI cache.
