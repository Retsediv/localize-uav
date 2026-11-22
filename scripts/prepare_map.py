#!/usr/bin/env python3
"""Merge, crop, and downsample multi-tile DOP/DSM GeoTIFF maps into a fast
cached single-file pair (DOP <= 6000x6000 @ 0.2 m/px + DSM) so OrthoTrack uses
its single-file GeoTIFFHandler with full RAM preload() instead of MultiTileGeoTIFFHandler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pyproj
import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge as rio_merge


def _load_map_meta(map_dir: Path) -> Dict[str, Any]:
    meta_file = map_dir / "map_meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _tiles_in_dir(d: Path) -> List[Path]:
    if not d.exists():
        return []
    return sorted(list(d.glob("*.tif")) + list(d.glob("*.tiff")))


def _intersecting_tiles(
    tifs: List[Path],
    bounds: Optional[Tuple[float, float, float, float]],
) -> Tuple[List[Path], Tuple[float, float, float, float], str, float]:
    """Return (matching_tifs, union_bounds, crs_str, native_res)."""
    selected: List[Path] = []
    u_left = u_bottom = float("inf")
    u_right = u_top = float("-inf")
    crs_str = ""
    native_res = 0.0

    for p in tifs:
        with rasterio.open(p) as src:
            if not crs_str and src.crs:
                crs_str = str(src.crs)
            if native_res <= 0:
                native_res = float(src.res[0])
            b = src.bounds
            if bounds is not None:
                bl, bb, br, bt = bounds
                if b.right <= bl or b.left >= br or b.top <= bb or b.bottom >= bt:
                    continue
            selected.append(p)
            u_left = min(u_left, b.left)
            u_bottom = min(u_bottom, b.bottom)
            u_right = max(u_right, b.right)
            u_top = max(u_top, b.top)

    if not selected:
        # Fallback to all tiles if center was slightly outside
        return _intersecting_tiles(tifs, None)

    return selected, (u_left, u_bottom, u_right, u_top), crs_str, native_res


def _write_merged_tif(
    tifs: List[Path],
    out_path: Path,
    bounds: Tuple[float, float, float, float],
    res: float,
    is_dsm: bool,
) -> Tuple[int, int]:
    datasets = [rasterio.open(p) for p in tifs]
    try:
        resampling = Resampling.bilinear
        merged, transform = rio_merge(
            datasets,
            bounds=bounds,
            res=(res, res),
            resampling=resampling,
        )
        crs = datasets[0].crs
        nodata = datasets[0].nodata
    finally:
        for ds in datasets:
            ds.close()

    if is_dsm:
        arr = merged[:1].astype(np.float32, copy=False)
        dtype = "float32"
        count = 1
        compress = "deflate"
    else:
        if merged.shape[0] > 3:
            merged = merged[:3]
        elif merged.shape[0] == 1:
            merged = np.repeat(merged, 3, axis=0)
        if merged.dtype != np.uint8:
            arr = np.clip(merged, 0, 255).astype(np.uint8)
        else:
            arr = merged
        dtype = "uint8"
        count = arr.shape[0]
        compress = "lzw"
        nodata = None

    height, width = int(arr.shape[1]), int(arr.shape[2])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".tif.part")

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": count,
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "compress": compress,
        "BIGTIFF": "IF_SAFER",
    }
    if nodata is not None:
        profile["nodata"] = nodata

    with rasterio.open(tmp_path, "w", **profile) as dst:
        dst.write(arr)

    tmp_path.rename(out_path)
    return width, height


def prepare_single_file_map(
    map_dir: Path,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_m: Optional[float] = None,
    target_dop_gsd: float = 0.2,
    max_dop_side: int = 5980,
    log_fn=None,
) -> Dict[str, Any]:
    """Build or reuse cached single-file DOP (<= 6000px @ ~0.2m/px) and DSM GeoTIFFs.

    Returns dict with keys: dop_path, dsm_path, cached, dop_width, dop_height,
    dop_gsd, dsm_gsd, bounds, crs, elapsed_s.
    """
    t0 = time.perf_counter()
    map_dir = Path(map_dir).resolve()
    dop_all = _tiles_in_dir(map_dir / "dop")
    dsm_all = _tiles_in_dir(map_dir / "dsm")
    if not dop_all or not dsm_all:
        raise FileNotFoundError(f"Map directory {map_dir} must contain dop/*.tif and dsm/*.tif")

    meta = _load_map_meta(map_dir)
    if (lat is None or lon is None) and meta.get("center_wgs84"):
        lat = float(meta["center_wgs84"]["lat"])
        lon = float(meta["center_wgs84"]["lon"])
    if radius_m is None:
        radius_m = float(meta.get("radius_m") or 600.0)

    # Read CRS from first DOP tile
    with rasterio.open(dop_all[0]) as src:
        crs_str = str(src.crs)
        dop_native_res = float(src.res[0])
    with rasterio.open(dsm_all[0]) as src:
        dsm_native_res = float(src.res[0])

    # Determine desired crop bounds in map CRS
    req_bounds: Optional[Tuple[float, float, float, float]] = None
    if lat is not None and lon is not None:
        tr = pyproj.Transformer.from_crs("EPSG:4326", crs_str, always_xy=True)
        cx, cy = tr.transform(lon, lat)
        # Snap to 10m grid so nearby VLM queries hit the exact same cache!
        cx_s = round(cx / 10.0) * 10.0
        cy_s = round(cy / 10.0) * 10.0
        r_s = max(250.0, round(float(radius_m) / 10.0) * 10.0)
        # Keep ±600m right under OrthoTrack's 6000px preload() ceiling at exact 0.2m/px
        if abs(r_s - 600.0) < 1e-3 and target_dop_gsd == 0.2:
            r_s = 595.0
        req_bounds = (cx_s - r_s, cy_s - r_s, cx_s + r_s, cy_s + r_s)

    dop_tiles, dop_union, _, _ = _intersecting_tiles(dop_all, req_bounds)
    dsm_tiles, dsm_union, _, _ = _intersecting_tiles(dsm_all, req_bounds)

    # Clamp crop bounds to intersection of available DOP and DSM tiles
    avail_left = max(dop_union[0], dsm_union[0])
    avail_bottom = max(dop_union[1], dsm_union[1])
    avail_right = min(dop_union[2], dsm_union[2])
    avail_top = min(dop_union[3], dsm_union[3])

    if req_bounds is not None:
        bl = max(req_bounds[0], avail_left)
        bb = max(req_bounds[1], avail_bottom)
        br = min(req_bounds[2], avail_right)
        bt = min(req_bounds[3], avail_top)
        if br - bl < 100 or bt - bb < 100:
            crop_bounds = (avail_left, avail_bottom, avail_right, avail_top)
        else:
            crop_bounds = (bl, bb, br, bt)
    else:
        crop_bounds = (avail_left, avail_bottom, avail_right, avail_top)

    span_x = crop_bounds[2] - crop_bounds[0]
    span_y = crop_bounds[3] - crop_bounds[1]
    max_span = max(span_x, span_y)

    # Choose DOP GSD: >= target_dop_gsd (0.2m/px) AND ensures output <= max_dop_side (<= 6000px -> RAM preload!)
    dop_gsd = max(dop_native_res, target_dop_gsd)
    if max_span / dop_gsd > max_dop_side:
        dop_gsd = round(math.ceil((max_span / max_dop_side) * 1000.0) / 1000.0, 3)

    dsm_gsd = max(dsm_native_res, 0.5)

    # Deterministic cache key
    sig_obj = {
        "dop_tiles": [(p.name, int(p.stat().st_size)) for p in dop_tiles],
        "dsm_tiles": [(p.name, int(p.stat().st_size)) for p in dsm_tiles],
        "bounds": [round(x, 1) for x in crop_bounds],
        "dop_gsd": dop_gsd,
        "dsm_gsd": dsm_gsd,
    }
    cache_hash = hashlib.sha1(json.dumps(sig_obj, sort_keys=True).encode()).hexdigest()[:12]
    cache_dir = map_dir / "cache"
    dop_out = cache_dir / f"dop_{dop_gsd:.3f}m_{cache_hash}.tif"
    dsm_out = cache_dir / f"dsm_{dsm_gsd:.3f}m_{cache_hash}.tif"

    cached = dop_out.exists() and dsm_out.exists() and dop_out.stat().st_size > 0 and dsm_out.stat().st_size > 0
    if cached:
        with rasterio.open(dop_out) as src:
            dop_w, dop_h = src.width, src.height
        if log_fn:
            log_fn(f"[MapCache] Hit: {dop_out.name} ({dop_w}x{dop_h} @ {dop_gsd}m/px) + {dsm_out.name}")
    else:
        if log_fn:
            log_fn(
                f"[MapCache] Building single-file DOP ({len(dop_tiles)} tiles -> {dop_gsd}m/px) "
                f"& DSM ({len(dsm_tiles)} tiles -> {dsm_gsd}m/px) over {span_x:.0f}x{span_y:.0f}m..."
            )
        dop_w, dop_h = _write_merged_tif(dop_tiles, dop_out, crop_bounds, dop_gsd, is_dsm=False)
        _write_merged_tif(dsm_tiles, dsm_out, crop_bounds, dsm_gsd, is_dsm=True)
        if log_fn:
            log_fn(
                f"[MapCache] Built {dop_out.name} ({dop_w}x{dop_h}, {dop_out.stat().st_size / 1e6:.1f} MB) "
                f"+ {dsm_out.name} ({dsm_out.stat().st_size / 1e6:.1f} MB) in {time.perf_counter() - t0:.1f}s"
            )

    return {
        "dop_path": str(dop_out),
        "dsm_path": str(dsm_out),
        "cached": cached,
        "dop_width": dop_w,
        "dop_height": dop_h,
        "dop_gsd": dop_gsd,
        "dsm_gsd": dsm_gsd,
        "bounds": crop_bounds,
        "crs": crs_str,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build cached single-file 0.2m/px DOP + DSM GeoTIFFs for fast OrthoTrack preload.")
    parser.add_argument("--map-dir", type=Path, required=True, help="Map directory containing dop/ and dsm/")
    parser.add_argument("--lat", type=float, default=None, help="Center latitude (WGS84)")
    parser.add_argument("--lon", type=float, default=None, help="Center longitude (WGS84)")
    parser.add_argument("--radius-m", type=float, default=None, help="Crop half-width in meters (default: 600 or map_meta.json)")
    parser.add_argument("--dop-gsd", type=float, default=0.2, help="Target DOP GSD in m/px (default: 0.2)")
    args = parser.parse_args()

    info = prepare_single_file_map(
        args.map_dir,
        lat=args.lat,
        lon=args.lon,
        radius_m=args.radius_m,
        target_dop_gsd=args.dop_gsd,
        log_fn=print,
    )
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
