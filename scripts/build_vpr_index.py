#!/usr/bin/env python3
"""Build the offline Mini-Switzerland Cross-View VPR Index for MegaLoc and/or Sample4Geo.

Indexes:
  - Local high-res Swiss maps in data/maps/ (zurich_limmat, lucerne, zurich_mav — excluding airport10)
  - Plus 8 major Swiss cities via lightweight swisstopo 2.0 m/px SWISSIMAGE tiles (~215-250 km² total)

Tiles are sliced at 300m (150m stride) and 600m (300m stride) across 4 rotations (0/90/180/270°)
and encoded into L2-normalized float16 embeddings under data/vpr_index/.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pyproj
import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge as rio_merge
from PIL import Image
from pystac_client import Client
from tqdm import tqdm

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import MAPS_DIR
from app.vpr import SWISS_CITIES, VPR_INDEX_DIR, VPR_SWISS_2M_DIR, VPREncoder
from scripts.fetch_swisstopo import (
    DOP_COLLECTION,
    STAC_URL,
    bbox_wgs84_from_center,
    download_file,
    query_latest_tiles,
)

EXCLUDED_REGIONS = {"airport10", "movingdrone_airport10"}


def fetch_swiss_2m_cities(cities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Download lightweight 2.0 m/px SWISSIMAGE DOP tiles for expansion Swiss cities."""
    catalog = Client.open(STAC_URL)
    city_sources: List[Dict[str, Any]] = []

    for c in cities:
        out_dir = VPR_SWISS_2M_DIR / c["id"] / "dop"
        out_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(out_dir.glob("*.tif"))
        if len(existing) >= 9:
            print(f"[Swiss2m] {c['name']}: {len(existing)} cached 2m/px tiles in {out_dir.relative_to(ROOT_DIR)}")
        else:
            bbox_wgs84, _ = bbox_wgs84_from_center(c["lat"], c["lon"], c["radius_m"])
            tiles = query_latest_tiles(catalog, DOP_COLLECTION, bbox_wgs84, prefer_gsd="2.0")
            print(f"[Swiss2m] {c['name']}: downloading {len(tiles)} tiles (2.0 m/px)...")

            def _dl(t):
                dest = out_dir / t["filename"]
                if dest.exists() and dest.stat().st_size > 0:
                    return
                import requests
                tmp = dest.with_suffix(".tif.part")
                with requests.get(t["href"], stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 128):
                            if chunk:
                                f.write(chunk)
                tmp.rename(dest)

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                list(tqdm(ex.map(_dl, tiles), total=len(tiles), desc=f"  {c['id']}", leave=False))

        city_sources.append({
            "region": c["id"],
            "name": c["name"],
            "map_dir": None,
            "source_dop_dir": str(out_dir.relative_to(ROOT_DIR)),
            "center_lat": c["lat"],
            "center_lon": c["lon"],
        })
    return city_sources


def collect_regions(include_swiss_cities: bool = True) -> List[Dict[str, Any]]:
    """Gather all local Swiss maps (excluding airport10) + optional Top-10 Swiss 2m/px cities."""
    regions: List[Dict[str, Any]] = []
    if MAPS_DIR.exists():
        for d in sorted(MAPS_DIR.iterdir()):
            if not d.is_dir() or d.name.lower() in EXCLUDED_REGIONS or d.name.startswith("auto_"):
                continue
            dop_dir = d / "dop"
            if not dop_dir.exists() or not list(dop_dir.glob("*.tif")):
                continue
            meta_file = d / "map_meta.json"
            c_lat = c_lon = None
            if meta_file.exists():
                m = json.loads(meta_file.read_text(encoding="utf-8"))
                if m.get("center_wgs84"):
                    c_lat = float(m["center_wgs84"]["lat"])
                    c_lon = float(m["center_wgs84"]["lon"])
            regions.append({
                "region": d.name,
                "name": d.name.replace("_", " ").title(),
                "map_dir": str(d.relative_to(ROOT_DIR)),
                "source_dop_dir": str(dop_dir.relative_to(ROOT_DIR)),
                "center_lat": c_lat,
                "center_lon": c_lon,
            })

    if include_swiss_cities:
        existing_ids = {r["region"] for r in regions}
        to_fetch = [c for c in SWISS_CITIES if c["id"] not in existing_ids]
        regions.extend(fetch_swiss_2m_cities(to_fetch))

    return regions


def load_region_mosaic_2m(dop_dir: Path, target_gsd: float = 2.0) -> Tuple[np.ndarray, rasterio.Affine, str]:
    """Read all DOP GeoTIFFs in dop_dir into one uint8 RGB mosaic at target_gsd (m/px)."""
    tifs = sorted(list(dop_dir.glob("*.tif")) + list(dop_dir.glob("*.tiff")))
    datasets = [rasterio.open(p) for p in tifs]
    try:
        crs_str = str(datasets[0].crs)
        merged, transform = rio_merge(
            datasets,
            res=(target_gsd, target_gsd),
            resampling=Resampling.bilinear,
        )
    finally:
        for ds in datasets:
            ds.close()

    if merged.shape[0] > 3:
        merged = merged[:3]
    elif merged.shape[0] == 1:
        merged = np.repeat(merged, 3, axis=0)
    rgb = np.transpose(np.clip(merged, 0, 255).astype(np.uint8), (1, 2, 0))
    return rgb, transform, crs_str


def slice_region_crops(
    region_info: Dict[str, Any],
    scales_m: Tuple[int, ...] = (300, 600),
    rotations: Tuple[int, ...] = (0, 90, 180, 270),
    target_gsd: float = 2.0,
) -> Tuple[List[Dict[str, Any]], List[Image.Image]]:
    """Slice a region's 2m/px mosaic into overlapping multi-scale, multi-rotation crops."""
    dop_dir = ROOT_DIR / region_info["source_dop_dir"]
    rgb, transform, crs_str = load_region_mosaic_2m(dop_dir, target_gsd=target_gsd)
    H, W = rgb.shape[:2]
    to_wgs84 = pyproj.Transformer.from_crs(crs_str, "EPSG:4326", always_xy=True)

    metas: List[Dict[str, Any]] = []
    images: List[Image.Image] = []

    for size_m in scales_m:
        win_px = int(round(size_m / target_gsd))
        step_px = max(1, win_px // 2)  # 50% overlap
        if H < win_px or W < win_px:
            continue

        ys = list(range(0, H - win_px + 1, step_px))
        if ys and ys[-1] != H - win_px:
            ys.append(H - win_px)
        xs = list(range(0, W - win_px + 1, step_px))
        if xs and xs[-1] != W - win_px:
            xs.append(W - win_px)

        for y0 in ys:
            for x0 in xs:
                crop = rgb[y0 : y0 + win_px, x0 : x0 + win_px]
                # Reject nodata / black border crops (>15% near-black pixels)
                non_black = (crop.max(axis=2) > 4).mean()
                if non_black < 0.85:
                    continue

                cx_px = x0 + win_px / 2.0
                cy_px = y0 + win_px / 2.0
                cx = float(transform.c + cx_px * transform.a + cy_px * transform.b)
                cy = float(transform.f + cx_px * transform.d + cy_px * transform.e)
                lon, lat = to_wgs84.transform(cx, cy)

                for rot_deg in rotations:
                    if rot_deg == 90:
                        im_arr = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
                    elif rot_deg == 180:
                        im_arr = cv2.rotate(crop, cv2.ROTATE_180)
                    elif rot_deg == 270:
                        im_arr = cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    else:
                        im_arr = crop

                    images.append(Image.fromarray(im_arr))
                    metas.append({
                        "region": region_info["region"],
                        "region_name": region_info["name"],
                        "map_dir": region_info["map_dir"],
                        "source_dop_dir": region_info["source_dop_dir"],
                        "crs": crs_str,
                        "center_x": round(cx, 2),
                        "center_y": round(cy, 2),
                        "center_lat": round(float(lat), 6),
                        "center_lon": round(float(lon), 6),
                        "size_m": int(size_m),
                        "rot_deg": int(rot_deg),
                    })
    return metas, images


def build_index(
    models: List[str],
    include_swiss_cities: bool = True,
    scales_m: Tuple[int, ...] = (300, 600),
    rotations: Tuple[int, ...] = (0, 90, 180, 270),
    batch_size: int = 64,
) -> None:
    import torch

    VPR_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    regions = collect_regions(include_swiss_cities=include_swiss_cities)
    print(f"\n[VPR Build] Regions ({len(regions)}): {[r['region'] for r in regions]}")

    # Encode region-by-region so peak RAM stays tiny even for 50k tiles!
    for model_key in models:
        print(f"\n{'=' * 60}\n[VPR Build] Encoding Mini-Switzerland with '{model_key}'...\n{'=' * 60}")
        t0 = time.perf_counter()
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

        encoder = VPREncoder(model_key=model_key)
        all_metas: List[Dict[str, Any]] = []
        all_embs: List[np.ndarray] = []
        region_counts: Dict[str, int] = {}

        for reg in regions:
            t_r = time.perf_counter()
            metas, pils = slice_region_crops(reg, scales_m=scales_m, rotations=rotations, target_gsd=2.0)
            if not pils:
                continue
            embs = encoder.encode_pils(pils, batch_size=batch_size).astype(np.float16)
            all_embs.append(embs)
            for m in metas:
                m["tile_id"] = len(all_metas)
                all_metas.append(m)
            region_counts[reg["region"]] = len(metas)
            print(f"  {reg['region']:16s}: {len(metas):5d} tiles encoded in {time.perf_counter() - t_r:.1f}s")

        emb_matrix = np.concatenate(all_embs, axis=0)
        elapsed_s = round(time.perf_counter() - t0, 2)
        peak_vram_mb = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1) if torch.cuda.is_available() else 0.0

        emb_path = VPR_INDEX_DIR / f"{model_key}_embeddings.npy"
        np.save(emb_path, emb_matrix)
        size_mb = round(emb_path.stat().st_size / (1024 * 1024), 2)

        meta_path = VPR_INDEX_DIR / "tiles_meta.json"
        meta_path.write_text(json.dumps(all_metas), encoding="utf-8")

        info = {
            "model": model_key,
            "num_tiles": int(emb_matrix.shape[0]),
            "dim": int(emb_matrix.shape[1]),
            "dtype": "float16",
            "scales_m": list(scales_m),
            "rotations": list(rotations),
            "regions": region_counts,
            "build_time_s": elapsed_s,
            "peak_vram_mb": peak_vram_mb,
            "index_size_mb": size_mb,
        }
        (VPR_INDEX_DIR / f"{model_key}_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(
            f"[VPR Build] Saved {emb_path.name}: shape={emb_matrix.shape}, "
            f"size={size_mb} MB, time={elapsed_s}s, peak_vram={peak_vram_mb} MB"
        )

        del encoder
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline Mini-Switzerland VPR indices (MegaLoc & Sample4Geo).")
    parser.add_argument(
        "--models",
        nargs="+",
        default=["megaloc", "sample4geo"],
        choices=["megaloc", "sample4geo"],
        help="Models to index (default: both megaloc and sample4geo)",
    )
    parser.add_argument("--local-only", action="store_true", help="Only index local data/maps/* without 2m/px Swiss cities")
    parser.add_argument("--batch-size", type=int, default=64, help="GPU encoding batch size (default: 64)")
    args = parser.parse_args()

    build_index(
        models=args.models,
        include_swiss_cities=not args.local_only,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
