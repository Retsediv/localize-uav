#!/usr/bin/env python3
"""Download SWISSIMAGE (DOP 10cm) and swissSURFACE3D Raster (DSM 50cm) GeoTIFF tiles
from the public swisstopo STAC API around a given WGS84 (lat, lon) coordinate.

Outputs are stored in EPSG:2056 (Swiss LV95, meters) and can be passed directly to
OrthoTrack's `--dop <out>/dop --dsm <out>/dsm` flags.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import pyproj
import requests
from pystac_client import Client
from tqdm import tqdm

STAC_URL = "https://data.geo.admin.ch/api/stac/v1/"
DOP_COLLECTION = "ch.swisstopo.swissimage-dop10"
DSM_COLLECTION = "ch.swisstopo.swisssurface3d-raster"


def bbox_wgs84_from_center(lat: float, lon: float, radius_m: float) -> Tuple[List[float], Tuple[float, float]]:
    """Compute a WGS84 [min_lon, min_lat, max_lon, max_lat] bbox of ±radius_m in EPSG:2056."""
    to_lv95 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2056", always_xy=True)
    to_wgs84 = pyproj.Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)

    cx, cy = to_lv95.transform(lon, lat)
    min_lon, min_lat = to_wgs84.transform(cx - radius_m, cy - radius_m)
    max_lon, max_lat = to_wgs84.transform(cx + radius_m, cy + radius_m)
    return [min_lon, min_lat, max_lon, max_lat], (cx, cy)


def _tile_key(item_id: str) -> str:
    """Extract the 1x1 km LV95 tile coordinate key (e.g. '2683-1247') from a swisstopo STAC item ID."""
    m = re.search(r"(\d{4}-\d{4})", item_id)
    return m.group(1) if m else item_id


def _pick_best_tif_asset(item, prefer_gsd: str | None = None) -> Tuple[str, str] | None:
    """Select the highest-resolution GeoTIFF asset from a STAC item."""
    candidates = []
    for key, asset in item.assets.items():
        href = asset.href
        if not href.lower().endswith(".tif"):
            continue
        gsd = asset.extra_fields.get("eo:gsd") or asset.extra_fields.get("gsd")
        if gsd is None:
            m = re.search(r"_(\d+(?:\.\d+)?)_2056", href)
            gsd = float(m.group(1)) if m else 999.0
        else:
            gsd = float(gsd)
        candidates.append((gsd, key, href))

    if not candidates:
        return None

    if prefer_gsd is not None:
        target = float(prefer_gsd)
        candidates.sort(key=lambda x: (abs(x[0] - target), x[0]))
    else:
        candidates.sort(key=lambda x: x[0])

    _, best_key, best_href = candidates[0]
    return best_key, best_href


def query_latest_tiles(
    catalog: Client,
    collection_id: str,
    bbox_wgs84: List[float],
    prefer_gsd: str | None = None,
) -> List[Dict[str, str]]:
    """Query STAC for the latest GeoTIFF asset per 1x1 km tile covering bbox_wgs84."""
    search = catalog.search(collections=[collection_id], bbox=bbox_wgs84)
    latest_by_tile: Dict[str, Tuple[str, Dict[str, str]]] = {}

    for item in search.items():
        picked = _pick_best_tif_asset(item, prefer_gsd=prefer_gsd)
        if picked is None:
            continue
        asset_key, href = picked
        tkey = _tile_key(item.id)
        dt = item.properties.get("datetime") or item.properties.get("updated") or item.id
        prev = latest_by_tile.get(tkey)
        if prev is None or str(dt) > prev[0]:
            latest_by_tile[tkey] = (
                str(dt),
                {
                    "tile": tkey,
                    "item_id": item.id,
                    "datetime": str(dt),
                    "asset_key": asset_key,
                    "href": href,
                    "filename": Path(href).name,
                },
            )

    return [v[1] for _, v in sorted(latest_by_tile.items())]


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} (already exists)")
        return

    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(tmp, "wb") as f, tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"  {dest.name}",
            leave=True,
        ) as pbar:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))
    tmp.rename(dest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch SWISSIMAGE DOP + swissSURFACE3D DSM tiles from swisstopo STAC.")
    parser.add_argument("--lat", type=float, required=True, help="Center latitude (WGS84), e.g. 47.3717")
    parser.add_argument("--lon", type=float, required=True, help="Center longitude (WGS84), e.g. 8.5422")
    parser.add_argument("--radius-m", type=float, default=600.0, help="Half-width of search area in meters (default: 600)")
    parser.add_argument("--out", type=Path, required=True, help="Output map directory (creates dop/ and dsm/)")
    parser.add_argument("--dop-gsd", type=str, default="0.1", help="Preferred DOP GSD in m/px (default: 0.1)")
    parser.add_argument("--dry-run", action="store_true", help="List tiles without downloading")
    args = parser.parse_args()

    bbox_wgs84, (cx, cy) = bbox_wgs84_from_center(args.lat, args.lon, args.radius_m)
    print(f"Center WGS84: ({args.lat:.6f}, {args.lon:.6f}) -> LV95: ({cx:.1f}, {cy:.1f})")
    print(f"Query bbox WGS84: {[round(x, 6) for x in bbox_wgs84]} (±{args.radius_m:.0f} m)")

    catalog = Client.open(STAC_URL)
    dop_tiles = query_latest_tiles(catalog, DOP_COLLECTION, bbox_wgs84, prefer_gsd=args.dop_gsd)
    dsm_tiles = query_latest_tiles(catalog, DSM_COLLECTION, bbox_wgs84)

    print(f"\nFound {len(dop_tiles)} DOP tiles ({DOP_COLLECTION}):")
    for t in dop_tiles:
        print(f"  - {t['tile']}: {t['filename']} ({t['datetime']})")

    print(f"\nFound {len(dsm_tiles)} DSM tiles ({DSM_COLLECTION}):")
    for t in dsm_tiles:
        print(f"  - {t['tile']}: {t['filename']} ({t['datetime']})")

    if args.dry_run:
        return

    dop_dir = args.out / "dop"
    dsm_dir = args.out / "dsm"

    print("\nDownloading DOP tiles...")
    dop_paths = []
    for t in dop_tiles:
        dest = dop_dir / t["filename"]
        download_file(t["href"], dest)
        dop_paths.append(str(dest))

    print("\nDownloading DSM tiles...")
    dsm_paths = []
    for t in dsm_tiles:
        dest = dsm_dir / t["filename"]
        download_file(t["href"], dest)
        dsm_paths.append(str(dest))

    meta = {
        "source": "swisstopo STAC API",
        "crs": "EPSG:2056",
        "vertical_datum": "LN02 (Swiss orthometric height)",
        "center_wgs84": {"lat": args.lat, "lon": args.lon},
        "center_lv95": {"easting": cx, "northing": cy},
        "radius_m": args.radius_m,
        "bbox_wgs84": bbox_wgs84,
        "dop_dir": str(dop_dir),
        "dsm_dir": str(dsm_dir),
        "dop_tiles": dop_tiles,
        "dsm_tiles": dsm_tiles,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    meta_path = args.out / "map_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"\nSaved map metadata to {meta_path}")


if __name__ == "__main__":
    main()
