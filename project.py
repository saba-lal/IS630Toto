import csv
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pdfplumber
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
SUPP_DIR = DATA_DIR / "supplementary"
OUT_DIR = DATA_DIR / "analysis_ready"

RAW_DIR.mkdir(parents=True, exist_ok=True)
SUPP_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

SP_BASE_URL = "https://www.singaporepools.com.sg"
SP_TOTO_URL = f"{SP_BASE_URL}/en/product/Pages/toto_wo.aspx"
GRA_PDF_URL = "https://www.gra.gov.sg/docs/default-source/betting-operations--lottery-and-game-of-chance/list-of-approved-singapore-pools-gambling-venuesf0fae72f-58d2-433f-abbe-2da61f8b6f22.pdf"
ONEMAP_SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

API_HEADERS = {"User-Agent": "SMU-IS630-Project/1.0"}

SUPPLEMENTARY_DATASETS = [
    {"name": "URA Planning Area Boundary", "dataset_id": "d_4765db0e87b9c86336792efe8a1f7a66", "filename": "planning_area_boundary.geojson"},
    {"name": "HDB Existing Building", "dataset_id": "d_16b157c52ed637edd6ba1232e026258d", "filename": "hdb_existing_building.geojson"},
    {"name": "URA Master Plan 2019 Land Use", "dataset_id": "d_90d86daa5bfaa371668b84fa5f01424f", "filename": "master_plan_land_use.geojson"},
    {"name": "Census 2020 Population by Planning Area (Dwelling Type)", "dataset_id": "d_7f243956483d5901f237e6f87b096636", "filename": "census2020_pop_by_dwelling.csv"},
]

MANUAL_GEOCODES = {
    "Singapore Pools Choa Chu Kang Branch": {"latitude": 1.3846968155873416, "longitude": 103.74378458764835, "planning_area": "CHOA CHU KANG"},
    "Singapore Pools Woodlands Centre": {"latitude": 1.443578459984722, "longitude": 103.77085901032147, "planning_area": "WOODLANDS"},
    "Singapore Pools Rochor Centre Branch": {"latitude": 1.3052787317021182, "longitude": 103.85491101759817, "planning_area": "ROCHOR"},
    "Cheers Woodlands Centre": {"latitude": 1.4416756560120672, "longitude": 103.77002767602397, "planning_area": "WOODLANDS"},
}

LU_CATEGORY = {
    "RESIDENTIAL": "residential", "RESIDENTIAL / INSTITUTION": "residential",
    "RESIDENTIAL WITH COMMERCIAL AT 1ST STOREY": "residential",
    "COMMERCIAL & RESIDENTIAL": "mixed",
    "COMMERCIAL": "commercial", "COMMERCIAL / INSTITUTION": "commercial",
    "HOTEL": "commercial", "BUSINESS 1": "commercial", "BUSINESS 1 - WHITE": "commercial",
    "BUSINESS 2": "commercial", "BUSINESS 2 - WHITE": "commercial",
    "BUSINESS PARK": "commercial", "BUSINESS PARK - WHITE": "commercial",
    "CIVIC & COMMUNITY INSTITUTION": "institutional", "EDUCATIONAL INSTITUTION": "institutional",
    "PLACE OF WORSHIP": "institutional", "HEALTH & MEDICAL CARE": "institutional",
    "OPEN SPACE": "open", "PARK": "open", "SPORTS & RECREATION": "open", "BEACH AREA": "open",
    "ROAD": "infrastructure", "TRANSPORT FACILITIES": "infrastructure",
    "LIGHT RAPID TRANSIT": "infrastructure", "MASS RAPID TRANSIT": "infrastructure",
    "UTILITY": "infrastructure", "WATERBODY": "infrastructure", "PORT / AIRPORT": "infrastructure",
    "RESERVE SITE": "other", "SPECIAL USE": "other", "WHITE": "other",
    "AGRICULTURE": "other", "CEMETERY": "other",
}

RADII = [500, 1000, 1500]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def polygon_centroid(geom):
    pts = []
    if geom["type"] == "Polygon":
        pts = geom["coordinates"][0]
    elif geom["type"] == "MultiPolygon":
        for poly in geom["coordinates"]:
            pts.extend(poly[0])
    if not pts:
        return None, None
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def load_pa_index():
    """Load PA polygons, bounding boxes, centroids, and regions. Returns (pa_features, pa_bounds, pa_centroids, pa_regions)."""
    pa_features, pa_bounds, pa_centroids, pa_regions = [], {}, {}, {}
    pa_path = SUPP_DIR / "planning_area_boundary.geojson"
    if not pa_path.exists():
        return pa_features, pa_bounds, pa_centroids, pa_regions
    with open(pa_path, encoding="utf-8") as f:
        for feature in json.load(f)["features"]:
            props = feature["properties"]
            name = props["PLN_AREA_N"]
            pa_regions[name] = props["REGION_N"]
            pa_features.append((name, feature))
            geom = feature.get("geometry")
            pts = []
            if geom["type"] == "Polygon":
                pts = geom["coordinates"][0]
            elif geom["type"] == "MultiPolygon":
                for poly in geom["coordinates"]:
                    pts.extend(poly[0])
            if pts:
                pa_bounds[name] = (min(p[1] for p in pts), max(p[1] for p in pts),
                                   min(p[0] for p in pts), max(p[0] for p in pts))
            lat, lon = polygon_centroid(geom)
            if lat is not None:
                pa_centroids[name] = (lat, lon)
    return pa_features, pa_bounds, pa_centroids, pa_regions


def point_in_polygon(lon, lat, ring):
    inside, n, j = False, len(ring), len(ring) - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def find_pa(lat, lon, pa_features, pa_bounds, pa_centroids):
    """Point-in-polygon PA lookup with nearest-centroid fallback."""
    for pa_name, (min_lat, max_lat, min_lon, max_lon) in pa_bounds.items():
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            feature = next(f for n, f in pa_features if n == pa_name)
            geom = feature["geometry"]
            if geom["type"] == "Polygon":
                if point_in_polygon(lon, lat, geom["coordinates"][0]):
                    return pa_name
            elif geom["type"] == "MultiPolygon":
                if any(point_in_polygon(lon, lat, poly[0]) for poly in geom["coordinates"]):
                    return pa_name
    # fallback: nearest centroid for coastal/edge outlets
    best_pa, best_d = "", float("inf")
    for pn, (clat, clon) in pa_centroids.items():
        d = haversine_m(lat, lon, clat, clon)
        if d < best_d:
            best_d, best_pa = d, pn
    return best_pa if best_d < 5000 else ""
    

def download_dataset(dataset_id, output_path):
    if output_path.exists():
        print(f"  [SKIP] {output_path.name} ({output_path.stat().st_size:,} bytes)")
        return True
    print(f"  Downloading {output_path.name}...")
    time.sleep(2)
    url = f"https://api-open.data.gov.sg/v1/public/api/datasets/{dataset_id}/poll-download"
    try:
        with urlopen(Request(url, headers=API_HEADERS), timeout=30) as resp:
            data = json.loads(resp.read().decode())
            dl_url = data.get("data", {}).get("url")
            if dl_url:
                with urlopen(Request(dl_url, headers=API_HEADERS), timeout=600) as dl:
                    content = dl.read()
                    with open(output_path, "wb") as f:
                        f.write(content)
                    print(f"  Saved ({len(content):,} bytes)")
                    return True
    except (HTTPError, URLError, json.JSONDecodeError):
        pass
    print(f"  [MANUAL] https://data.gov.sg/datasets/{dataset_id}/view")
    return False


def parse_time_to_hours(s):
    s = s.strip().lower()
    m = re.match(r'(\d{1,2})[.:](\d{2})\s*(am|pm)', s)
    if not m:
        return -1.0
    h, mins, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return h + mins / 60.0


def format_time(hours_val):
    if hours_val < 0:
        return ""
    h = int(hours_val)
    m = int(round((hours_val - h) * 60))
    ampm = "am" if h < 12 else "pm"
    dh = h if h <= 12 else h - 12
    if dh == 0:
        dh = 12
    return f"{dh}.{m:02d} {ampm}"


def step1_scrape_aggregate():
    output_path = RAW_DIR / "outlets_list.csv"
    if output_path.exists():
        print(f"  [SKIP] {output_path.name}")
        with open(output_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    resp = requests.get(SP_TOTO_URL, headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    with open(RAW_DIR / "toto_wo_page.html", "w", encoding="utf-8") as f:
        f.write(resp.text)

    soup = BeautifulSoup(resp.text, "lxml")
    outlets = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 3:
                link = cells[0].find("a")
                if link:
                    name = link.get_text(strip=True)
                    href = link.get("href", "")
                    if href and not href.startswith("http"):
                        href = SP_BASE_URL + href
                    counts = []
                    for cell in cells[1:]:
                        text = cell.get_text(strip=True).replace(",", "")
                        if text.isdigit():
                            counts.append(int(text))
                    g1 = counts[0] if len(counts) >= 1 else 0
                    g2 = counts[1] if len(counts) >= 2 else 0
                    outlets.append({"outlet_name": name, "detail_url": href, "group1_wins": g1, "group2_wins": g2, "combined_wins": counts[2] if len(counts) >= 3 else g1 + g2})

    if not outlets:
        for link in soup.find_all("a", href=re.compile(r"lo_details\.aspx|sppl=")):
            name = link.get_text(strip=True)
            if not name or len(name) < 3:
                continue
            href = link.get("href", "")
            if href and not href.startswith("http"):
                href = SP_BASE_URL + href
            parent = link.find_parent(["tr", "div", "li", "span"])
            counts = [int(t) for t in re.findall(r'\b(\d{1,5})\b', parent.get_text())] if parent else []
            outlets.append({"outlet_name": name, "detail_url": href, "group1_wins": counts[0] if len(counts) >= 2 else 0, "group2_wins": counts[1] if len(counts) >= 2 else 0, "combined_wins": counts[2] if len(counts) >= 3 else 0})

    seen, unique = set(), []
    for o in outlets:
        if o["outlet_name"] not in seen:
            seen.add(o["outlet_name"])
            unique.append(o)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["outlet_name", "detail_url", "group1_wins", "group2_wins", "combined_wins"])
        w.writeheader()
        w.writerows(unique)
    print(f"  Saved {len(unique)} outlets")
    return unique


def step2_scrape_details(outlet_list):
    win_path = RAW_DIR / "outlet_win_history.csv"
    addr_path = RAW_DIR / "outlets_with_addresses.csv"
    if win_path.exists() and addr_path.exists():
        print(f"  [SKIP] win history + addresses exist")
        with open(win_path, newline="", encoding="utf-8") as f:
            all_wins = list(csv.DictReader(f))
        with open(addr_path, newline="", encoding="utf-8") as f:
            all_outlets = list(csv.DictReader(f))
        return all_wins, all_outlets

    all_wins, all_outlets, done_names = [], [], set()
    if addr_path.exists():
        with open(addr_path, newline="", encoding="utf-8") as f:
            all_outlets = list(csv.DictReader(f))
            done_names = {o["outlet_name"] for o in all_outlets}
    if win_path.exists():
        with open(win_path, newline="", encoding="utf-8") as f:
            all_wins = list(csv.DictReader(f))

    remaining = [o for o in outlet_list if o["outlet_name"] not in done_names]
    print(f"  Done: {len(done_names)}, remaining: {len(remaining)}")
    if not remaining:
        return all_wins, all_outlets

    for i, outlet in enumerate(remaining):
        name, url = outlet["outlet_name"], outlet.get("detail_url", "")
        if not url or "lo_details" not in url:
            continue
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException:
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        address, postal_code = "", ""
        for tag in soup.find_all(["div", "span", "p", "td"]):
            text = tag.get_text(strip=True)
            if re.search(r'Singapore\s+\d{6}', text) and len(text) < 200:
                address = text
                m = re.search(r'Singapore\s+(\d{6})', text)
                if m:
                    postal_code = m.group(1)
                break

        wins = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            header = rows[0].get_text(strip=True).lower()
            if "draw date" not in header and "draw no" not in header:
                continue
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all("td")]
                if len(cells) < 4 or not re.match(r'\d{2}/\d{2}/\d{4}', cells[1]) or not cells[2].isdigit():
                    continue
                amount = 0.0
                am = re.search(r'S?\$?([\d,]+)', cells[3])
                if am:
                    amount = float(am.group(1).replace(",", ""))
                pg = re.search(r'Group\s+(\d)', cells[3])
                bt = re.search(r'Group\s+\d\s+(.*?)\)', cells[3])
                wins.append({"outlet_name": name, "draw_date": cells[1], "draw_number": int(cells[2]), "prize_amount": amount, "bet_type": bt.group(1).strip() if bt else "", "prize_group": int(pg.group(1)) if pg else 0})

        all_outlets.append({"outlet_name": name, "address": address, "postal_code": postal_code, "detail_url": url})
        all_wins.extend(wins)
        if (i + 1) % 50 == 0:
            _save_details(all_wins, all_outlets)
            print(f"  [{i+1}/{len(remaining)}]...")
        time.sleep(2.0)

    _save_details(all_wins, all_outlets)
    return all_wins, all_outlets


def _save_details(all_wins, all_outlets):
    if all_wins:
        with open(RAW_DIR / "outlet_win_history.csv", "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=["outlet_name", "draw_date", "draw_number", "prize_amount", "bet_type", "prize_group"]).writeheader()
            csv.DictWriter(f, fieldnames=["outlet_name", "draw_date", "draw_number", "prize_amount", "bet_type", "prize_group"]).writerows(all_wins)
    if all_outlets:
        with open(RAW_DIR / "outlets_with_addresses.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["outlet_name", "address", "postal_code", "detail_url"])
            w.writeheader()
            w.writerows(all_outlets)


def step3_parse_gra():
    output_path = RAW_DIR / "gra_outlets.csv"
    if output_path.exists():
        print(f"  [SKIP] {output_path.name}")
        with open(output_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    pdf_path = RAW_DIR / "gra_approved_outlets.pdf"
    if not pdf_path.exists():
        try:
            resp = requests.get(GRA_PDF_URL, headers=BROWSER_HEADERS, timeout=30)
            resp.raise_for_status()
            with open(pdf_path, "wb") as f:
                f.write(resp.content)
        except requests.RequestException:
            return []

    outlets = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or all(c is None for c in row):
                        continue
                    cells = [str(c).strip() if c else "" for c in row]
                    if any(h in cells[0].lower() for h in ["s/n", "serial", "no.", "building"]):
                        continue
                    if cells[0] and re.match(r'^\d+$', cells[0]):
                        sn = int(cells[0])
                        if len(cells) >= 6:
                            building, street, unit, oname, pc = cells[1], cells[2], cells[3], cells[4], cells[5].replace(" ", "")
                        elif len(cells) >= 5:
                            building, street, unit, oname, pc = cells[1], cells[2], "", cells[3], cells[4].replace(" ", "")
                        else:
                            continue
                        pm = re.search(r'(\d{6})', pc)
                        pc = pm.group(1) if pm else pc
                        full_addr = " ".join(p for p in [building, street, unit] if p)
                        combined = (oname + " " + full_addr).lower()
                        ot = "Authorised Retailer"
                        if "livewire" in combined: ot = "Livewire"
                        elif "betting centre" in combined or "ocb" in combined: ot = "Betting Centre"
                        elif "branch" in combined: ot = "Branch"
                        elif "lobby" in combined: ot = "Lottery Lobby"
                        elif "7-eleven" in combined or "7 eleven" in combined: ot = "Authorised Retailer (7-Eleven)"
                        elif "fairprice" in combined or "ntuc" in combined: ot = "Authorised Retailer (FairPrice)"
                        outlets.append({"sn": sn, "outlet_name": oname, "full_address": full_addr, "building": building, "street": street, "unit": unit, "postal_code": pc, "outlet_type": ot})

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sn", "outlet_name", "full_address", "building", "street", "unit", "postal_code", "outlet_type"])
        w.writeheader()
        w.writerows(outlets)
    print(f"  Extracted {len(outlets)} GRA outlets")
    return outlets


def step4_download_supplementary():
    for ds in SUPPLEMENTARY_DATASETS:
        download_dataset(ds["dataset_id"], SUPP_DIR / ds["filename"])


def step5_merge(outlet_list, scraped_outlets, gra_outlets):
    scraped_by_name = {s["outlet_name"].strip(): s for s in scraped_outlets}
    gra_by_postal = {g["postal_code"].strip(): g for g in gra_outlets if re.match(r'^\d{6}$', g.get("postal_code", "").strip())}
    merged, used = {}, set()
    for o in outlet_list:
        name = o["outlet_name"].strip()
        postal = scraped_by_name.get(name, {}).get("postal_code", "").strip()
        gra = gra_by_postal.get(postal, {})
        if gra:
            used.add(postal)
        merged[name] = {"outlet_name": name, "postal_code": postal, "outlet_type": gra.get("outlet_type", ""), "group1_wins": int(o.get("group1_wins", 0)), "group2_wins": int(o.get("group2_wins", 0)), "combined_wins": int(o.get("group1_wins", 0)) + int(o.get("group2_wins", 0)), "source": "matched" if gra else "scraped"}
    for g in gra_outlets:
        gp = g.get("postal_code", "").strip()
        gname = g.get("outlet_name", "").strip()[:80]
        if gp in used or not gname:
            continue
        merged[gname] = {"outlet_name": gname, "postal_code": gp, "outlet_type": g.get("outlet_type", ""), "group1_wins": 0, "group2_wins": 0, "combined_wins": 0, "source": "gra_only"}
    physical = [o for o in merged.values() if "account betting" not in o["outlet_name"].lower() and "itoto" not in o["outlet_name"].lower()]
    with open(DATA_DIR / "outlets_raw.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["outlet_name", "postal_code", "outlet_type", "group1_wins", "group2_wins", "combined_wins", "source"])
        w.writeheader()
        w.writerows(physical)
    print(f"  {len(physical)} physical outlets")
    return physical


def step6_geocode():
    output_path = DATA_DIR / "outlets_geocoded.csv"
    if output_path.exists():
        print(f"  [SKIP] {output_path.name}")
        with open(output_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    with open(DATA_DIR / "outlets_raw.csv", newline="", encoding="utf-8") as f:
        outlets = list(csv.DictReader(f))
    geocoded = []
    for i, outlet in enumerate(outlets):
        name, postal = outlet.get("outlet_name", "").strip(), outlet.get("postal_code", "").strip()
        if name in MANUAL_GEOCODES:
            m = MANUAL_GEOCODES[name]
            geocoded.append({**outlet, "latitude": m["latitude"], "longitude": m["longitude"], "onemap_address": "manual", "planning_area": m["planning_area"], "x_svy21": "", "y_svy21": "", "geocode_status": "OK"})
            continue
        result = None
        if postal and len(postal) == 6 and postal.isdigit():
            for attempt in range(3):
                try:
                    with urlopen(Request(f"{ONEMAP_SEARCH_URL}?searchVal={postal}&returnGeom=Y&getAddrDetails=Y", headers=API_HEADERS), timeout=30) as resp:
                        data = json.loads(resp.read().decode())
                        if data.get("found", 0) > 0:
                            r = data["results"][0]
                            result = {"latitude": float(r["LATITUDE"]), "longitude": float(r["LONGITUDE"]), "onemap_address": r.get("ADDRESS", ""), "x_svy21": float(r.get("X", 0)), "y_svy21": float(r.get("Y", 0))}
                    break
                except (URLError, TimeoutError):
                    time.sleep(2)
        if result:
            geocoded.append({**outlet, **result, "planning_area": "", "geocode_status": "OK"})
        else:
            geocoded.append({**outlet, "latitude": "", "longitude": "", "onemap_address": "", "planning_area": "", "x_svy21": "", "y_svy21": "", "geocode_status": "FAILED"})
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(outlets)}]...")
        time.sleep(1.0)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(geocoded[0].keys()))
        w.writeheader()
        w.writerows(geocoded)
    ok = sum(1 for r in geocoded if r["geocode_status"] == "OK")
    print(f"  {ok} OK, {len(geocoded) - ok} failed")
    return geocoded


def step7_extract_centroids():
    lu_cache, hdb_cache = SUPP_DIR / "land_use_centroids.csv", SUPP_DIR / "hdb_block_centroids.csv"
    lu_centroids = []
    if lu_cache.exists():
        with open(lu_cache, newline="", encoding="utf-8") as f:
            lu_centroids = [(row["lu_category"], float(row["latitude"]), float(row["longitude"]), float(row["area_sqm"])) for row in csv.DictReader(f)]
        print(f"  {len(lu_centroids)} cached land use centroids")
    elif (SUPP_DIR / "master_plan_land_use.geojson").exists():
        print(f"  Parsing master_plan_land_use.geojson (~166MB)...")
        with open(SUPP_DIR / "master_plan_land_use.geojson", encoding="utf-8") as f:
            gj = json.load(f)
        for feature in gj["features"]:
            props = feature["properties"]
            cat = LU_CATEGORY.get((props.get("LU_DESC") or "").strip(), "other")
            area = float(props.get("SHAPE.AREA", 0) or 0)
            geom = feature.get("geometry")
            if geom is None: continue
            lat, lon = polygon_centroid(geom)
            if lat is not None:
                lu_centroids.append((cat, lat, lon, area))
        with open(lu_cache, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["lu_category", "latitude", "longitude", "area_sqm"])
            w.writerows(lu_centroids)
        print(f"  Extracted {len(lu_centroids)}")
        del gj

    hdb_blocks = []
    if hdb_cache.exists():
        with open(hdb_cache, newline="", encoding="utf-8") as f:
            hdb_blocks = [(float(row["latitude"]), float(row["longitude"])) for row in csv.DictReader(f)]
        print(f"  {len(hdb_blocks)} cached HDB block centroids")
    elif (SUPP_DIR / "hdb_existing_building.geojson").exists():
        print(f"  Parsing hdb_existing_building.geojson (~54MB)...")
        with open(SUPP_DIR / "hdb_existing_building.geojson", encoding="utf-8") as f:
            gj = json.load(f)
        for feature in gj["features"]:
            geom = feature.get("geometry")
            if geom is None: continue
            lat, lon = polygon_centroid(geom)
            if lat is not None:
                hdb_blocks.append((lat, lon))
        with open(hdb_cache, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["latitude", "longitude"])
            w.writerows(hdb_blocks)
        print(f"  Extracted {len(hdb_blocks)}")
        del gj
    return lu_centroids, hdb_blocks


def step8_build_geodata(geocoded_outlets, lu_centroids, hdb_blocks):
    pa_features, pa_bounds, pa_centroids, pa_regions = load_pa_index()

    outlets = []
    for o in geocoded_outlets:
        if o.get("geocode_status") != "OK":
            continue
        try:
            lat, lon = float(o["latitude"]), float(o["longitude"])
        except (ValueError, KeyError):
            continue
        pa = (o.get("planning_area") or "").strip().upper()
        if not pa:
            pa = find_pa(lat, lon, pa_features, pa_bounds, pa_centroids)
        outlets.append({
            "outlet_name": o["outlet_name"], "postal_code": o.get("postal_code", ""),
            "outlet_type": o.get("outlet_type", ""), "group1_wins": int(o.get("group1_wins", 0)),
            "group2_wins": int(o.get("group2_wins", 0)), "combined_wins": int(o.get("combined_wins", 0)),
            "source": o.get("source", ""), "latitude": lat, "longitude": lon,
            "onemap_address": o.get("onemap_address", ""), "planning_area": pa,
            "region": pa_regions.get(pa, ""), "geocode_status": "OK",
        })

    deg_box = max(RADII) / 111320.0 * 1.15
    for idx, outlet in enumerate(outlets):
        olat, olon = outlet["latitude"], outlet["longitude"]
        nearby_lu = [(cat, clat, clon, area) for cat, clat, clon, area in lu_centroids
                     if abs(clat - olat) <= deg_box and abs(clon - olon) <= deg_box]
        lu_dists = [(cat, area, haversine_m(olat, olon, clat, clon)) for cat, clat, clon, area in nearby_lu]
        nearby_hdb = [haversine_m(olat, olon, blat, blon) for blat, blon in hdb_blocks
                      if abs(blat - olat) <= deg_box and abs(blon - olon) <= deg_box]
        for radius in RADII:
            abc = defaultdict(float)
            for cat, area, dist in lu_dists:
                if dist <= radius:
                    abc[cat] += area
            outlet[f"res_area_{radius}m"] = round(abc.get("residential", 0))
            outlet[f"com_area_{radius}m"] = round(abc.get("commercial", 0))
            outlet[f"mixed_area_{radius}m"] = round(abc.get("mixed", 0))
            outlet[f"inst_area_{radius}m"] = round(abc.get("institutional", 0))
            outlet[f"open_area_{radius}m"] = round(abc.get("open", 0))
            outlet[f"hdb_blocks_{radius}m"] = sum(1 for d in nearby_hdb if d <= radius)
            res_t = abc.get("residential", 0) + abc.get("mixed", 0) * 0.5
            com_t = abc.get("commercial", 0) + abc.get("mixed", 0) * 0.5
            denom = res_t + com_t
            outlet[f"rc_ratio_{radius}m"] = round(res_t / denom, 4) if denom > 0 else 0.5
        rc, hdb = outlet["rc_ratio_1000m"], outlet["hdb_blocks_1000m"]
        outlet["neighborhood_type"] = "residential" if rc >= 0.65 and hdb >= 5 else ("commercial" if rc <= 0.35 else "mixed")
        a1k = {"res": outlet["res_area_1000m"], "com": outlet["com_area_1000m"],
               "mixed": outlet["mixed_area_1000m"], "inst": outlet["inst_area_1000m"],
               "open": outlet["open_area_1000m"]}
        outlet["dominant_landuse_1000m"] = max(a1k, key=a1k.get) if any(a1k.values()) else "unknown"
        outlet["landuse_diversity_1000m"] = sum(1 for v in a1k.values() if v > 0)
        hdb1k, wins = outlet["hdb_blocks_1000m"], outlet["combined_wins"]
        outlet["win_rate_hdb_1000m"] = round(wins / hdb1k, 6) if hdb1k > 0 and wins > 0 else 0.0
        if (idx + 1) % 100 == 0:
            print(f"  [{idx+1}/{len(outlets)}]...")
    return outlets


def step9_earliest_win_years():
    path = RAW_DIR / "outlet_win_history.csv"
    if not path.exists():
        return {}
    earliest = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m = re.search(r'(\d{4})$', row.get("draw_date", ""))
            if m:
                year = int(m.group(1))
                name = row["outlet_name"]
                if name not in earliest or year < earliest[name]:
                    earliest[name] = year
    print(f"  {len(earliest)} outlets ({min(earliest.values())}-{max(earliest.values())})")
    return earliest


def step10_scrape_hours():
    cache_path = RAW_DIR / "outlet_operating_hours.csv"
    done = {}
    if cache_path.exists():
        with open(cache_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done[row["outlet_name"]] = row
    with open(RAW_DIR / "outlets_list.csv", newline="", encoding="utf-8") as f:
        outlet_list = list(csv.DictReader(f))
    remaining = [o for o in outlet_list if o["outlet_name"] not in done]
    if not remaining:
        print(f"  [SKIP] All {len(done)} scraped")
        return done
    print(f"  Remaining: {len(remaining)} (~{len(remaining) * 2 / 60:.0f} min)")
    for i, outlet in enumerate(remaining):
        name, url = outlet["outlet_name"], outlet.get("detail_url", "")
        if not url or "lo_details" not in url:
            done[name] = _empty_hours(name)
            continue
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
            resp.raise_for_status()
            done[name] = _extract_hours(BeautifulSoup(resp.text, "lxml"), name)
        except requests.RequestException:
            done[name] = _empty_hours(name)
        if (i + 1) % 50 == 0:
            _save_hours(done)
            print(f"  [{i+1}/{len(remaining)}]...")
        time.sleep(2.0)
    _save_hours(done)
    return done


def _extract_hours(soup, name):
    for table in soup.find_all("table"):
        opening_row, closing_row = None, None
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if not cells: continue
            if cells[0].lower() == "opening": opening_row = cells[1:]
            elif cells[0].lower() == "closing": closing_row = cells[1:]
        if opening_row and closing_row:
            ot = [parse_time_to_hours(t) for t in opening_row[:7]]
            ct = [parse_time_to_hours(t) for t in closing_row[:7]]
            while len(ot) < 7: ot.append(-1.0)
            while len(ct) < 7: ct.append(-1.0)
            daily = [c - o + (24 if c - o < 0 else 0) for o, c in zip(ot, ct) if o >= 0 and c >= 0]
            avg = sum(daily) / len(daily) if daily else 0.0
            vo = [o for o in ot if o >= 0]
            vc = [c for c in ct if c >= 0]
            varying = len(set(round(o, 2) for o in vo)) > 1 or len(set(round(c, 2) for c in vc)) > 1
            mo = max(set(vo), key=vo.count) if vo else -1
            mc = max(set(vc), key=vc.count) if vc else -1
            result = {"outlet_name": name}
            for j, day in enumerate(DAYS):
                result[f"open_{day}"] = format_time(ot[j])
                result[f"close_{day}"] = format_time(ct[j])
            result["open_time"] = format_time(mo)
            result["close_time"] = format_time(mc)
            result["open_hours_daily"] = round(avg, 2)
            result["has_varying_hours"] = "1" if varying else "0"
            return result
    return _empty_hours(name)


def _empty_hours(name):
    result = {"outlet_name": name}
    for day in DAYS:
        result[f"open_{day}"] = ""
        result[f"close_{day}"] = ""
    result.update({"open_time": "", "close_time": "", "open_hours_daily": "", "has_varying_hours": ""})
    return result


def _save_hours(done):
    rows = list(done.values())
    if not rows: return
    with open(RAW_DIR / "outlet_operating_hours.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

def step11a_load_population(): #extract population per PA
    path = SUPP_DIR / "census2020_pop_by_dwelling.csv"
    pop_by_pa = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pa_raw = row.get("Number", "").strip()
            if not pa_raw.endswith("- Total"):
                continue
            pa_name = pa_raw.replace("- Total", "").strip().upper()
            total = row.get("Total", "").strip().replace(",", "")
            if total.isdigit():
                pop_by_pa[pa_name] = int(total)
    print(f"  {len(pop_by_pa)} planning areas with population data")
    print(list(pop_by_pa.items())[:5])
    return pop_by_pa


def step11b_res_area_by_pa():
    cache_path = SUPP_DIR / "land_use_centroids_with_pa.csv"
    if cache_path.exists():
        print(f"  [SKIP] {cache_path.name}")
        res_area_pa = defaultdict(float)
        with open(cache_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cat = row["lu_category"]
                if cat not in ("residential", "mixed"):
                    continue
                pa = row["planning_area"]
                area = float(row["area_sqm"])
                weight = 1.0 if cat == "residential" else 0.5
                res_area_pa[pa] += area * weight
        print(f"  {len(res_area_pa)} planning areas with residential area")
        return res_area_pa

    lu_cache = SUPP_DIR / "land_use_centroids.csv"
    if not lu_cache.exists():
        print("  [WARN] land_use_centroids.csv not found — run step7 first")
        return defaultdict(float)

    pa_features, pa_bounds, pa_centroids, _ = load_pa_index()

    with open(lu_cache, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"  Assigning {len(rows)} centroids to planning areas...")

    assigned = []
    res_area_pa = defaultdict(float)
    for i, row in enumerate(rows):
        lat, lon = float(row["latitude"]), float(row["longitude"])
        pa = find_pa(lat, lon, pa_features, pa_bounds, pa_centroids)
        assigned.append({**row, "planning_area": pa})
        cat = row["lu_category"]
        if cat in ("residential", "mixed") and pa:
            weight = 1.0 if cat == "residential" else 0.5
            res_area_pa[pa] += float(row["area_sqm"]) * weight
        if (i + 1) % 10000 == 0:
            print(f"  [{i+1}/{len(rows)}] assigned...")

    with open(cache_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["lu_category", "latitude", "longitude", "area_sqm", "planning_area"])
        writer.writeheader()
        writer.writerows(assigned)
    print(f"  {len(res_area_pa)} planning areas with residential area")
    return res_area_pa

def step12_save(outlets, hours, earliest_years):
    # --- Deduplicate by postal code: sum wins, keep current outlet ---
    from collections import defaultdict
    postal_groups = defaultdict(list)
    for o in outlets:
        pc = o.get("postal_code", "").strip()
        if pc:
            postal_groups[pc].append(o)
        
    deduped = []
    for pc, group in postal_groups.items():
        if len(group) == 1:
            deduped.append(group[0])
            continue
        # determine current outlet: prefer one with hours, then later earliest_win_year
        def outlet_score(o):
            name = o["outlet_name"]
            has_hours = 1 if hours.get(name, {}).get("open_hours_daily", "") != "" else 0
            win_year = earliest_years.get(name, 0) or 0
            return (has_hours, win_year)
        group.sort(key=outlet_score, reverse=True)
        current = group[0]
        total_g1 = sum(int(o["group1_wins"]) for o in group)
        total_g2 = sum(int(o["group2_wins"]) for o in group)
        current["group1_wins"] = total_g1
        current["group2_wins"] = total_g2
        current["combined_wins"] = total_g1 + total_g2
        names = [o["outlet_name"] for o in group]
        print(f"  [DEDUP] {pc}: merged {names} → '{current['outlet_name']}' (wins: {current['combined_wins']})")
        deduped.append(current)
    
    # handle outlets with no postal code (pass through unchanged)
    for o in outlets:
        if not o.get("postal_code", "").strip():
            deduped.append(o)

    outlets = deduped
    print(f"  After dedup: {len(outlets)} outlets")
    
    print("\n[11/12] Load population & compute density")
    pop_by_pa = step11a_load_population()
    res_area_pa = step11b_res_area_by_pa()

    for outlet in outlets:
        name = outlet["outlet_name"]
        year = earliest_years.get(name)
        outlet["earliest_win_year"] = year if year else ""
        outlet["years_winning"] = 2026 - year if year else ""
        h = hours.get(name, {})
        outlet["open_time"] = h.get("open_time", "")
        outlet["close_time"] = h.get("close_time", "")
        outlet["open_hours_daily"] = h.get("open_hours_daily", "")
        outlet["has_varying_hours"] = h.get("has_varying_hours", "")
        pa = outlet.get("planning_area", "").strip().upper()
        pop = pop_by_pa.get(pa, 0)
        res_area = res_area_pa.get(pa, 0)
        density = pop / res_area if res_area > 0 else 0.0  # people per m²
        for radius in RADII:
            outlet[f"pop_{radius}m"] = round(outlet.get(f"res_area_{radius}m", 0) * density)

    fieldnames = [
        "outlet_name", "postal_code", "outlet_type",
        "group1_wins", "group2_wins", "combined_wins", "source",
        "latitude", "longitude", "onemap_address", "planning_area", "region", "geocode_status",
        "res_area_500m", "com_area_500m", "mixed_area_500m", "inst_area_500m", "open_area_500m", "hdb_blocks_500m", "rc_ratio_500m",
        "res_area_1000m", "com_area_1000m", "mixed_area_1000m", "inst_area_1000m", "open_area_1000m", "hdb_blocks_1000m", "rc_ratio_1000m",
        "res_area_1500m", "com_area_1500m", "mixed_area_1500m", "inst_area_1500m", "open_area_1500m", "hdb_blocks_1500m", "rc_ratio_1500m",
        "pop_500m", "pop_1000m", "pop_1500m","neighborhood_type", "dominant_landuse_1000m", "landuse_diversity_1000m", "win_rate_hdb_1000m",
        "earliest_win_year", "years_winning", "open_time", "close_time", "open_hours_daily", "has_varying_hours",
    ]
    with open(OUT_DIR / "outlets_geodata.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(outlets)
    print(f"  {len(outlets)} outlets x {len(fieldnames)} columns")


def main():
    start = time.time()

    print("\n[1/12] Scrape TOTO winning outlets")
    outlet_list = step1_scrape_aggregate()
    print(f"{len(outlet_list)} outlets")

    print("\n[2/12] Scrape per-outlet details")
    all_wins, scraped_outlets = step2_scrape_details(outlet_list)
    print(f"{len(scraped_outlets)} outlets, {len(all_wins)} win records")

    print("\n[3/12] Parse GRA PDF")
    gra_outlets = step3_parse_gra()
    print(f"{len(gra_outlets)} GRA outlets")

    print("\n[4/12] Download supplementary datasets")
    step4_download_supplementary()

    print("\n[5/12] Merge data sources")
    step5_merge(outlet_list, scraped_outlets, gra_outlets)

    print("\n[6/12] Geocode outlets")
    geocoded = step6_geocode()

    print("\n[7/12] Extract GeoJSON centroids")
    lu_centroids, hdb_blocks = step7_extract_centroids()

    print("\n[8/12] Build geospatial profiles")
    outlets = step8_build_geodata(geocoded, lu_centroids, hdb_blocks)
    print(f"{len(outlets)} outlets profiled")

    print("\n[9/12] Compute earliest win years")
    earliest_years = step9_earliest_win_years()

    print("\n[10/12] Scrape operating hours")
    hours = step10_scrape_hours()

    print("\n[12/12] Save final dataset")
    step12_save(outlets, hours, earliest_years)

    res = sum(1 for o in outlets if o["neighborhood_type"] == "residential")
    com = sum(1 for o in outlets if o["neighborhood_type"] == "commercial")
    mix = sum(1 for o in outlets if o["neighborhood_type"] == "mixed")
    print(f"\n{'='*70}")
    print(f"DONE in {time.time() - start:.0f}s")
    print(f"{len(outlets)} outlets: residential={res}, commercial={com}, mixed={mix}")
    print(f"Output: {OUT_DIR / 'outlets_geodata.csv'}")
    print(f"{'='*70}")

if __name__ == "__main__":
    main()
