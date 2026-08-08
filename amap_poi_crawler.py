#!/usr/bin/env python3
"""Resume-safe city/district POI crawler for AMap Web Service API.

Features:
- sequential API-key failover at the exact failed request;
- QPS backoff without wasting the next key;
- grid pagination and recursive splitting near the 200-record API cap;
- durable JSONL/CSV/state output, POI-id de-duplication and adcode filtering;
- optional GCJ-02 to WGS84 CSV export.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DISTRICT_URL = "https://restapi.amap.com/v3/config/district"
POI_URL = "https://restapi.amap.com/v5/place/polygon"
PAGE_SIZE = 25
MAX_PAGES = 8
BEIJING_TZ = timezone(timedelta(hours=8))

FIELDS = [
    "id", "name", "type", "typecode", "address", "location", "lon", "lat",
    "pname", "cityname", "adname", "adcode", "business_area", "tel",
    "parent", "category_name", "category_code", "source_cell", "fetched_at",
]


@dataclass(frozen=True)
class Cell:
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    depth: int = 0


class AMapError(RuntimeError):
    def __init__(self, info: str, infocode: str = "") -> None:
        super().__init__(f"AMap API error {infocode}: {info}")
        self.info = info
        self.infocode = infocode


class RotatingClient:
    def __init__(
        self, keys: list[str], sleep_seconds: float, retries: int, timeout: float,
        max_requests: int | None,
    ) -> None:
        if not keys:
            raise ValueError("at least one AMap Web Service key is required")
        self.keys = keys
        self.key_index = 0
        self.sleep_seconds = sleep_seconds
        self.retries = retries
        self.timeout = timeout
        self.max_requests = max_requests
        self.request_count = 0

    def _once(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.max_requests is not None and self.request_count >= self.max_requests:
            raise RuntimeError(f"max request limit reached: {self.max_requests}")
        query = dict(params)
        query["key"] = self.keys[self.key_index]
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(query, safe=',|')}",
            headers={"User-Agent": "amap-poi-crawler/1.0"},
        )
        self.request_count += 1
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if str(data.get("status")) != "1":
            raise AMapError(str(data.get("info", "UNKNOWN")), str(data.get("infocode", "")))
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        return data

    def get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        transient = 0
        while self.key_index < len(self.keys):
            try:
                return self._once(url, params)
            except AMapError as exc:
                if "USER_DAILY_QUERY_OVER_LIMIT" in exc.info:
                    self.key_index += 1
                    transient = 0
                    if self.key_index >= len(self.keys):
                        raise RuntimeError("all supplied AMap keys exhausted daily quota") from exc
                    print(
                        f"Daily quota reached; retrying the same request with key "
                        f"#{self.key_index + 1}.", flush=True,
                    )
                    continue
                if exc.infocode == "10021" or any(
                    marker in exc.info for marker in ("USER_QPS_LIMIT", "CUQPS_HAS_EXCEEDED")
                ):
                    transient += 1
                    delay = min(60.0, max(2.0, 2.0 ** min(transient, 5)))
                    print(f"QPS limited; retrying the same key/request in {delay:.0f}s.", flush=True)
                    time.sleep(delay)
                    continue
                raise
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
                if transient >= self.retries:
                    raise
                transient += 1
                time.sleep(min(8.0, max(0.5, 0.5 * 2 ** transient)))
        raise RuntimeError("no usable AMap key remains")


def parse_types(raw: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in raw.split(","):
        if not item.strip():
            continue
        separator = ":" if ":" in item else "="
        name, code = item.split(separator, 1)
        if not name.strip() or not code.strip():
            raise ValueError(f"bad type specification: {item}")
        result.append((name.strip(), code.strip()))
    if not result:
        raise ValueError("at least one POI type is required")
    return result


def parse_polyline(raw: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for ring in raw.split("|"):
        for item in ring.split(";"):
            if item.strip():
                lon, lat = item.split(",", 1)
                points.append((float(lon), float(lat)))
    if not points:
        raise RuntimeError("district response did not contain a boundary")
    return points


def fetch_district(client: RotatingClient, keyword: str) -> tuple[str, str, tuple[float, float, float, float]]:
    data = client.get(DISTRICT_URL, {
        "keywords": keyword, "subdistrict": "0", "extensions": "all", "output": "json",
    })
    districts = data.get("districts") or []
    if not districts:
        raise RuntimeError(f"district not found: {keyword}")
    district = districts[0]
    points = parse_polyline(str(district.get("polyline") or ""))
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return (
        str(district.get("name") or keyword), str(district.get("adcode") or ""),
        (min(lons), min(lats), max(lons), max(lats)),
    )


def generate_cells(bbox: tuple[float, float, float, float], size: float) -> list[Cell]:
    min_lon, min_lat, max_lon, max_lat = bbox
    cells = [
        Cell(
            min_lon + i * size, min_lat + j * size,
            min(min_lon + (i + 1) * size, max_lon),
            min(min_lat + (j + 1) * size, max_lat),
        )
        for i in range(math.ceil((max_lon - min_lon) / size))
        for j in range(math.ceil((max_lat - min_lat) / size))
    ]
    center = ((min_lon + max_lon) / 2, (min_lat + max_lat) / 2)
    cells.sort(key=lambda c: ((c.min_lon + c.max_lon) / 2 - center[0]) ** 2 +
                             ((c.min_lat + c.max_lat) / 2 - center[1]) ** 2)
    return cells


def split_cell(c: Cell) -> list[Cell]:
    x, y, depth = (c.min_lon + c.max_lon) / 2, (c.min_lat + c.max_lat) / 2, c.depth + 1
    return [
        Cell(c.min_lon, c.min_lat, x, y, depth), Cell(x, c.min_lat, c.max_lon, y, depth),
        Cell(c.min_lon, y, x, c.max_lat, depth), Cell(x, y, c.max_lon, c.max_lat, depth),
    ]


def cell_key(c: Cell) -> str:
    return f"{c.min_lon:.6f},{c.min_lat:.6f},{c.max_lon:.6f},{c.max_lat:.6f},d{c.depth}"


def polygon(c: Cell) -> str:
    points = [(c.min_lon, c.min_lat), (c.max_lon, c.min_lat), (c.max_lon, c.max_lat),
              (c.min_lon, c.max_lat), (c.min_lon, c.min_lat)]
    return "|".join(f"{x:.6f},{y:.6f}" for x, y in points)


def fetch_page(client: RotatingClient, cell: Cell, type_code: str, page: int) -> dict[str, Any]:
    return client.get(POI_URL, {
        "polygon": polygon(cell), "types": type_code, "page_size": PAGE_SIZE,
        "page_num": page, "show_fields": "business", "output": "json",
    })


def collect_cell(client: RotatingClient, cell: Cell, type_code: str, can_split: bool) -> tuple[list[dict[str, Any]], int | None, bool]:
    first = fetch_page(client, cell, type_code, 1)
    try:
        total = int(first.get("count"))
    except (TypeError, ValueError):
        total = None
    pois = list(first.get("pois") or [])
    saturated = (total is not None and total >= 200) or len(pois) >= 200
    if saturated and can_split:
        return pois, total, True
    pages = min(MAX_PAGES, math.ceil(total / PAGE_SIZE)) if total is not None else (
        MAX_PAGES if len(pois) == PAGE_SIZE else 1
    )
    for page in range(2, pages + 1):
        batch = list(fetch_page(client, cell, type_code, page).get("pois") or [])
        if not batch:
            break
        pois.extend(batch)
    return pois, total, (total is not None and total >= 200) or len(pois) >= 200


def poi_id(poi: dict[str, Any]) -> str:
    if poi.get("id"):
        return str(poi["id"])
    raw = json.dumps(
        {key: poi.get(key) for key in ("name", "typecode", "location", "address")},
        ensure_ascii=False, sort_keys=True,
    )
    return "hash:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def adcode_matches(poi: dict[str, Any], target: str) -> bool:
    code = str(poi.get("adcode") or "")
    if target.endswith("0000"):
        return code.startswith(target[:2])
    if target.endswith("00"):
        return code.startswith(target[:4])
    return code == target


def flatten(poi: dict[str, Any], name: str, code: str, cell: Cell) -> dict[str, str]:
    location = str(poi.get("location") or "")
    lon, lat = (location.split(",", 1) + [""])[:2] if "," in location else ("", "")
    business = poi.get("business") if isinstance(poi.get("business"), dict) else {}
    def text(value: Any) -> str:
        if value is None or value == [] or value == {}:
            return ""
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, (list, dict)) else str(value)
    return {
        "id": poi_id(poi), "name": text(poi.get("name")), "type": text(poi.get("type")),
        "typecode": text(poi.get("typecode")), "address": text(poi.get("address")),
        "location": location, "lon": lon.strip(), "lat": lat.strip(),
        "pname": text(poi.get("pname")), "cityname": text(poi.get("cityname")),
        "adname": text(poi.get("adname")), "adcode": text(poi.get("adcode")),
        "business_area": text(poi.get("business_area") or business.get("business_area")),
        "tel": text(poi.get("tel")), "parent": text(poi.get("parent")),
        "category_name": name, "category_code": code, "source_cell": cell_key(cell),
        "fetched_at": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.4 * (attempt + 1))


def read_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                poi = record.get("poi", record)
                if isinstance(poi, dict):
                    seen.add(poi_id(poi))
            except json.JSONDecodeError:
                continue
    return seen


def task(cell: Cell, name: str, code: str) -> dict[str, Any]:
    return {"cell": asdict(cell), "category_name": name, "category_code": code}


def task_cell(item: dict[str, Any]) -> Cell:
    return Cell(**item["cell"])


PI, A, EE = 3.1415926535897932384626, 6378245.0, 0.00669342162296594323


def _lat(x: float, y: float) -> float:
    value = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x))
    value += (20*math.sin(6*x*PI) + 20*math.sin(2*x*PI))*2/3
    value += (20*math.sin(y*PI) + 40*math.sin(y/3*PI))*2/3
    return value + (160*math.sin(y/12*PI) + 320*math.sin(y*PI/30))*2/3


def _lon(x: float, y: float) -> float:
    value = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x))
    value += (20*math.sin(6*x*PI) + 20*math.sin(2*x*PI))*2/3
    value += (20*math.sin(x*PI) + 40*math.sin(x/3*PI))*2/3
    return value + (150*math.sin(x/12*PI) + 300*math.sin(x/30*PI))*2/3


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    dlat, dlon, rad = _lat(lon-105, lat-35), _lon(lon-105, lat-35), lat/180*PI
    magic = 1 - EE * math.sin(rad) ** 2
    dlat = dlat*180 / ((A*(1-EE))/(magic*math.sqrt(magic))*PI)
    dlon = dlon*180 / (A/math.sqrt(magic)*math.cos(rad)*PI)
    return lon-dlon, lat-dlat


def export_wgs84(source: Path) -> Path:
    target = source.with_name(source.stem + "_WGS84.csv")
    with source.open("r", encoding="utf-8-sig", newline="") as src, target.open(
        "w", encoding="utf-8-sig", newline=""
    ) as dst:
        reader = csv.DictReader(src)
        fields = list(reader.fieldnames or [])
        fields[5:8] = ["location_wgs84", "lon_wgs84", "lat_wgs84"]
        writer = csv.DictWriter(dst, fieldnames=fields)
        writer.writeheader()
        for row in reader:
            lon, lat = gcj02_to_wgs84(float(row["lon"]), float(row["lat"]))
            values = list(row.values())
            values[5:8] = [f"{lon:.8f},{lat:.8f}", f"{lon:.8f}", f"{lat:.8f}"]
            writer.writerow(dict(zip(fields, values)))
    return target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--district-keyword", required=True, help="district name, citycode or adcode")
    parser.add_argument("--types", required=True, help="comma-separated name:code pairs")
    parser.add_argument("--out-dir", default="output/amap_poi")
    parser.add_argument("--output-prefix", default="amap_poi")
    parser.add_argument("--grid-size", type=float, default=.04)
    parser.add_argument("--min-grid-size", type=float, default=.00125)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--sleep", type=float, default=.35)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--export-wgs84", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    keys = [key.strip() for key in os.getenv("AMAP_KEYS", os.getenv("AMAP_KEY", "")).split(";") if key.strip()]
    client = RotatingClient(keys, args.sleep, args.retries, args.timeout, args.max_requests)
    categories = parse_types(args.types)
    district_name, target_adcode, bbox = fetch_district(client, args.district_keyword)
    cells = generate_cells(bbox, args.grid_size)
    print(json.dumps({"district": district_name, "adcode": target_adcode, "bbox": bbox,
                      "initial_cells": len(cells), "categories": categories}, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path, raw_path, state_path = (
        out / f"{args.output_prefix}.csv", out / f"{args.output_prefix}_raw.jsonl",
        out / f"{args.output_prefix}_state.json",
    )
    signature = {"district": args.district_keyword, "types": categories, "grid_size": args.grid_size,
                 "min_grid_size": args.min_grid_size, "max_depth": args.max_depth}
    resume = not args.no_resume and state_path.exists()
    if resume:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("signature") != signature:
            raise RuntimeError("state configuration differs; use another output directory or --no-resume")
    else:
        state = {"signature": signature, "pending": [task(c, n, code) for c in cells for n, code in categories],
                 "completed": 0, "saturated": []}
    seen = read_seen(raw_path) if resume else set()
    csv_exists = resume and csv_path.exists() and csv_path.stat().st_size > 0
    stats = {"written": 0, "duplicates": 0, "filtered_out": 0, "split": 0}

    with raw_path.open("a", encoding="utf-8", newline="") as raw, csv_path.open(
        "a", encoding="utf-8-sig", newline=""
    ) as table:
        writer = csv.DictWriter(table, fieldnames=FIELDS)
        if not csv_exists:
            writer.writeheader()
        while state["pending"]:
            item = state["pending"][0]
            cell, name, code = task_cell(item), item["category_name"], item["category_code"]
            can_split = cell.depth < args.max_depth and (cell.max_lon-cell.min_lon) > args.min_grid_size
            pois, total, saturated = collect_cell(client, cell, code, can_split)
            if saturated and can_split:
                state["pending"][:1] = [task(child, name, code) for child in split_cell(cell)]
                stats["split"] += 1
            else:
                if saturated:
                    state["saturated"].append({"category_code": code, "cell": cell_key(cell), "count": total})
                for poi in pois:
                    if not adcode_matches(poi, target_adcode):
                        stats["filtered_out"] += 1
                        continue
                    identity = poi_id(poi)
                    if identity in seen:
                        stats["duplicates"] += 1
                        continue
                    row = flatten(poi, name, code, cell)
                    raw.write(json.dumps({"poi": poi, "category_name": name, "category_code": code,
                                          "source_cell": cell_key(cell)}, ensure_ascii=False) + "\n")
                    writer.writerow(row)
                    seen.add(identity)
                    stats["written"] += 1
                state["pending"].pop(0)
                state["completed"] += 1
            raw.flush(); table.flush(); save_state(state_path, state)
            if state["completed"] and state["completed"] % 50 == 0:
                print(json.dumps({**stats, "completed": state["completed"],
                                  "remaining": len(state["pending"]), "requests": client.request_count}), flush=True)

    wgs = export_wgs84(csv_path) if args.export_wgs84 else None
    print(json.dumps({**stats, "completed": state["completed"], "remaining": 0,
                      "saturated_cells": len(state["saturated"]), "requests": client.request_count,
                      "csv_gcj02": str(csv_path.resolve()), "csv_wgs84": str(wgs.resolve()) if wgs else None,
                      "raw_jsonl": str(raw_path.resolve()), "state": str(state_path.resolve())},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

