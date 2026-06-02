# Data Collection Pipeline

## Quick Start

```bash
python3 toto_scrapper.py
```

Optional (enables planning area lookup via OneMap API):
```bash
export ONEMAP_EMAIL='your@email.com'
export ONEMAP_PASSWORD='yourpassword'
```

OneMap API: https://www.onemap.gov.sg/apidocs/register

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `requests` | HTTP requests to Singapore Pools, GRA |
| `beautifulsoup4` | HTML parsing of Singapore Pools pages |
| `lxml` | Fast HTML parser backend for BeautifulSoup |
| `pdfplumber` | PDF table extraction from GRA outlet list |

Install all dependencies:
```bash
pip install -r requirements.txt
```

Standard library modules used: `csv`, `json`, `math`, `os`, `re`, `sys`, `time`, `collections`, `difflib`, `pathlib`, `urllib`.

---

## Pipeline Steps

### Step 1: Scrape Winning Outlets Aggregate Page

**Source:** `https://www.singaporepools.com.sg/en/product/Pages/toto_wo.aspx`

**Output:** `data/raw/outlets_list.csv`

| Column | Description |
|--------|-------------|
| `outlet_name` | Display name from Singapore Pools |
| `detail_url` | Full URL to the outlet's detail page |
| `group1_wins` | Number of TOTO Group 1 (jackpot) wins |
| `group2_wins` | Number of TOTO Group 2 wins |
| `combined_wins` | Total wins (Group 1 + Group 2) |

---

### Step 2: Scrape Per-Outlet Winning History

**Source:** Individual outlet detail pages (`lo_details.aspx?sppl=...`)

**Outputs:**
- `data/raw/outlet_win_history.csv` — one row per win event (~19,000 records)
- `data/raw/outlets_with_addresses.csv` — one row per outlet with address and postal code

---

### Step 3: Parse GRA PDF

**Source:** Gambling Regulatory Authority official outlet directory PDF

**Output:** `data/raw/gra_outlets.csv` (~304 outlets)

| Column | Description |
|--------|-------------|
| `sn` | Serial number from PDF |
| `outlet_name` | Official registered name |
| `full_address` | Concatenated building + street + unit |
| `building` | Building name |
| `street` | Street address |
| `unit` | Unit number |
| `postal_code` | 6-digit Singapore postal code |
| `outlet_type` | Categorized outlet type |

---

### Step 4: Download Supplementary Datasets

**Source:** `data.gov.sg` open data API

**Outputs in `data/supplementary/`:**

| File | Description |
|------|-------------|------|
| `hdb_dwelling_units_by_town.csv` | HDB units by town, flat type, FY2008–2021
| `census2020_pop_by_dwelling.csv` | Census population by planning area & dwelling type |
| `census2020_pop_by_age_sex.csv` | Census population by planning area, age, sex |
| `planning_area_boundary.geojson` | URA Master Plan 2019 planning area polygons |
| `subzone_boundary.geojson` | URA Master Plan 2019 subzone polygons |

---

### Step 5: Merge Data Sources

Combines three raw sources into a single outlet list:

- `outlets_list.csv` — win counts per outlet (375 physical outlets)
- `outlets_with_addresses.csv` — postal codes (exact name match, 375/375)
- `gra_outlets.csv` — outlet type, enriched via postal code match only (no fuzzy matching)

Also appends GRA outlets that never appeared in the Singapore Pools winning list (zero wins, 9 outlets).

**Output:** `data/outlets_raw.csv` (~383 physical outlets)

| Column | Description |
|--------|-------------|
| `outlet_name` | Outlet display name |
| `postal_code` | 6-digit Singapore postal code |
| `outlet_type` | GRA-derived type (empty if no GRA match) |
| `group1_wins` | TOTO Group 1 (jackpot) wins |
| `group2_wins` | TOTO Group 2 wins |
| `combined_wins` | Total wins |
| `source` | `matched` (GRA postal match) / `scraped` / `gra_only` |

---

### Step 6: Geocode via OneMap API

Looks up coordinates for each outlet using Singapore's OneMap API, by postal code. 4 outlets not found in OneMap are resolved via manually verified coordinates.

**Source:** OneMap Singapore Geocoding API

**Output:** `data/outlets_geocoded.csv`

Added columns:

| Column | Description |
|--------|-------------|
| `latitude` | WGS84 latitude |
| `longitude` | WGS84 longitude |
| `onemap_address` | Standardized address from OneMap |
| `x_svy21` | SVY21 X coordinate |
| `y_svy21` | SVY21 Y coordinate |
| `geocode_status` | "OK" or "FAILED" |

**Manual geocodes (OneMap not found):**

| Outlet | Reason |
|--------|--------|
| Singapore Pools Choa Chu Kang Branch | Postal code not indexed in OneMap |
| Singapore Pools Woodlands Centre | Postal code not indexed in OneMap |
| Singapore Pools Rochor Centre Branch | Postal code not indexed in OneMap |
| Cheers Woodlands Centre | Postal code not indexed in OneMap |

---

### Step 7: Compute Proxy Volumes & Build Final Dataset

**Stesp:**

1. **Planning Area Assignment:** Assigns nearest URA planning area to each outlet via Haversine distance to planning area centroids (max 5km threshold).

2. **Region Assignment:** Maps each outlet's planning area to its URA region (e.g., "EAST REGION", "CENTRAL REGION") using GeoJSON properties.

3. **Area Type Classification:** Classifies outlet as `commercial` if its planning area is in a known commercial zone (Downtown Core, Orchard, Museum etc.), otherwise `residential`.

4. **HDB Block Count Proxy:** For each outlet, counts the number of HDB blocks within 4 radii (500m, 750m, 1km, 1.5km) using Haversine distance to each of the ~13,400 block centroids. This is used as a proxy for residential foot traffic and ticket sales exposure (λᵢ in the Poisson model). Note: proxy captures ~77% of Singapore's resident population (HDB residents); private residential areas are not reflected.

5. **Win Rate:** `win_rate_1000m = combined_wins / proxy_1000m` — wins per nearby HDB block within 1km.

**Output:** `data/analysis_ready/outlets_final.csv`

| Column | Type | Description |
|--------|------|-------------|
| `outlet_name` | str | Outlet display name |
| `postal_code` | str | 6-digit Singapore postal code |
| `outlet_type` | str | GRA-derived type (e.g., "Branch", "Authorised Retailer") |
| `group1_wins` | int | TOTO Group 1 (jackpot) wins |
| `group2_wins` | int | TOTO Group 2 wins |
| `combined_wins` | int | Total wins |
| `source` | str | Data source: `matched` / `scraped` / `gra_only` |
| `latitude` | float | WGS84 latitude |
| `longitude` | float | WGS84 longitude |
| `onemap_address` | str | Standardised address from OneMap |
| `planning_area` | str | URA planning area (uppercase) |
| `x_svy21` | float | SVY21 X coordinate |
| `y_svy21` | float | SVY21 Y coordinate |
| `geocode_status` | str | `OK` |
| `proxy_500m` | int | HDB blocks within 500m radius |
| `proxy_750m` | int | HDB blocks within 750m radius |
| `proxy_1000m` | int | HDB blocks within 1km radius |
| `proxy_1500m` | int | HDB blocks within 1.5km radius |
| `area_type` | str | `residential` or `commercial` |
| `region` | str | URA region (e.g., "EAST REGION") |
| `win_rate_1000m` | float | `combined_wins / proxy_1000m` |

---

## Known Limitations

- **HDB proxy only:** ~77% of Singapore's population lives in HDB. Outlets in private residential areas (River Valley, Orchard, Bukit Timah) or commercial/transit zones (HarbourFront, Changi Business Park) will have underestimated exposure, potentially inflating their apparent win rates.
- **No outlet opening dates:** Exposure period is not normalised by how long each outlet has been operating. Older outlets mechanically accumulate more wins. The `outlet_win_history.csv` earliest draw date can be used as a lower-bound proxy for opening date in downstream analysis.
- **Area type is binary:** Classification is residential vs commercial based on planning area only. Mixed-use outlets (e.g. NTUC in an HDB town centre adjacent to a mall) are not distinguished.
