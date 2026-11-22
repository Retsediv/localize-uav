#!/usr/bin/env python3
"""Convert OrthoTrack `results.json` into a clean WGS84 `trajectory.json`
ready for CesiumJS / Google Photorealistic 3D Tiles playback and evaluation.

Handles:
- Automatic CRS detection from `map_meta.json` or DOP GeoTIFF headers via rasterio
- Projected (e.g. EPSG:2056 LV95 or UTM) -> WGS84 (lat, lon, alt_m)
- Camera-to-world rotation quaternion (OpenCV camera axes -> local ENU) into:
    - forward_enu / up_enu unit vectors (exact for CesiumJS camera.setView)
    - heading_deg (0=North, 90=East), pitch_deg (-90=nadir, 0=horizon), roll_deg
    - course_deg (ground track direction between consecutive localized frames)
- Optional vertical offset correction (e.g. LN02/orthometric -> WGS84 ellipsoidal height)
- Summary statistics (coverage, keyframe rate, latency, optional GT error metrics)
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import pyproj
import rasterio
from scipy.spatial.transform import Rotation


def detect_crs_and_offset(
    crs_arg: Optional[str],
    map_dir: Optional[Path],
    dop_path: Optional[Path],
    sequence_dir: Optional[Path],
) -> Tuple[str, str, Tuple[float, float, float]]:
    """Return (crs_string, vertical_datum_label, (ox, oy, oz) origin offset)."""
    offset = (0.0, 0.0, 0.0)

    if sequence_dir is not None:
        meta_file = sequence_dir / "meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            utm_zone = meta.get("utm_zone")
            utm_offset = meta.get("utm_offset")
            if utm_offset and len(utm_offset) == 3:
                # MovingDrone x,y are relative to utm_offset[0:2], while z is already absolute altitude.
                offset = (float(utm_offset[0]), float(utm_offset[1]), 0.0)
            if crs_arg:
                return crs_arg, "map_z", offset
            if utm_zone is not None:
                return f"EPSG:{32600 + int(utm_zone)}", "DHHN2016 / UTM Z", offset

    if crs_arg:
        return crs_arg, "map_z", offset

    if map_dir is not None:
        meta_file = map_dir / "map_meta.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("crs"):
                return str(meta["crs"]), str(meta.get("vertical_datum", "map_z")), offset
        dop_candidates = list((map_dir / "dop").glob("*.tif")) if (map_dir / "dop").exists() else list(map_dir.rglob("*.tif"))
        if dop_candidates:
            with rasterio.open(dop_candidates[0]) as src:
                if src.crs:
                    return str(src.crs), "map_z", offset

    if dop_path is not None:
        if dop_path.is_dir():
            tifs = sorted(list(dop_path.rglob("*.tif")) + list(dop_path.rglob("*.tiff")))
            if tifs:
                with rasterio.open(tifs[0]) as src:
                    if src.crs:
                        return str(src.crs), "map_z", offset
        elif dop_path.is_file():
            with rasterio.open(dop_path) as src:
                if src.crs:
                    return str(src.crs), "map_z", offset

    raise ValueError("Could not determine map CRS. Pass --crs (e.g. EPSG:2056), --map-dir, --sequence-dir, or --dop.")


def probe_video_fps(video_path: Optional[Path], fallback_fps: float) -> float:
    if video_path is None or not video_path.exists():
        return fallback_fps
    cap = cv2.VideoCapture(str(video_path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps and fps > 0.1 and math.isfinite(fps):
            return float(fps)
    finally:
        cap.release()
    return fallback_fps


def quat_to_enu_vectors_and_hpr(
    qw: float, qx: float, qy: float, qz: float
) -> Dict[str, Any]:
    """Convert OrthoTrack R_c2w quaternion (OpenCV cam -> ENU world) to Cesium-friendly vectors and HPR.

    OpenCV camera frame: +X right, +Y down, +Z forward.
    Local ENU frame:     +X East,  +Y North, +Z Up.
    """
    R_c2w = Rotation.from_quat([qx, qy, qz, qw]).as_matrix()

    right_enu = R_c2w[:, 0]
    up_enu = -R_c2w[:, 1]
    forward_enu = R_c2w[:, 2]

    # Normalize defensively
    forward_enu = forward_enu / max(np.linalg.norm(forward_enu), 1e-12)
    up_enu = up_enu / max(np.linalg.norm(up_enu), 1e-12)
    right_enu = right_enu / max(np.linalg.norm(right_enu), 1e-12)

    # Heading: azimuth clockwise from North in degrees [0, 360)
    horiz = math.hypot(float(forward_enu[0]), float(forward_enu[1]))
    if horiz > 1e-4:
        heading_deg = (math.degrees(math.atan2(float(forward_enu[0]), float(forward_enu[1]))) + 360.0) % 360.0
    else:
        # Near-nadir: derive heading from camera-up vector projected horizontally
        heading_deg = (math.degrees(math.atan2(float(up_enu[0]), float(up_enu[1]))) + 360.0) % 360.0

    # Pitch: -90 = straight down (nadir), 0 = horizon, +90 = straight up
    pitch_deg = math.degrees(math.asin(max(-1.0, min(1.0, float(forward_enu[2])))))

    # Roll: angle between camera right axis and horizontal right axis perpendicular to forward
    world_up = np.array([0.0, 0.0, 1.0])
    horiz_right = np.cross(forward_enu, world_up)
    hr_norm = np.linalg.norm(horiz_right)
    if hr_norm > 1e-4:
        horiz_right /= hr_norm
        horiz_up = np.cross(horiz_right, forward_enu)
        roll_deg = math.degrees(math.atan2(float(np.dot(right_enu, horiz_up)), float(np.dot(right_enu, horiz_right))))
    else:
        roll_deg = 0.0

    return {
        "forward_enu": [round(float(v), 6) for v in forward_enu],
        "up_enu": [round(float(v), 6) for v in up_enu],
        "heading_deg": round(heading_deg, 2),
        "pitch_deg": round(pitch_deg, 2),
        "roll_deg": round(roll_deg, 2),
    }


def _finite(v: Any) -> bool:
    return v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export OrthoTrack results.json to WGS84 trajectory.json.")
    parser.add_argument("--results", type=Path, required=True, help="Path to OrthoTrack results.json")
    parser.add_argument("--out", type=Path, required=True, help="Output trajectory.json path")
    parser.add_argument("--crs", type=str, default=None, help="Map CRS (e.g. EPSG:2056). Auto-detected if omitted.")
    parser.add_argument("--map-dir", type=Path, default=None, help="Map directory (with map_meta.json or dop/*.tif)")
    parser.add_argument("--sequence-dir", type=Path, default=None, help="MovingDrone sequence directory (with meta.json)")
    parser.add_argument("--dop", type=Path, default=None, help="DOP GeoTIFF file or directory for CRS detection")
    parser.add_argument("--video", type=Path, default=None, help="Source video path (used to probe exact FPS)")
    parser.add_argument("--fps", type=float, default=24.0, help="Fallback FPS if --video is not provided")
    parser.add_argument(
        "--geoid-offset-m",
        type=float,
        default=None,
        help="Optional constant offset added to map Z to obtain WGS84 ellipsoidal height for Cesium 3D Tiles "
             "(auto: +48.0 m for Switzerland EPSG:2056, +40.0 m for Berlin EPSG:32633 if omitted).",
    )
    args = parser.parse_args()

    raw = json.loads(args.results.read_text(encoding="utf-8"))
    crs_str, vertical_datum, (ox, oy, oz) = detect_crs_and_offset(args.crs, args.map_dir, args.dop, args.sequence_dir)
    fps = probe_video_fps(args.video, args.fps)

    # Approximate geoid undulation (orthometric -> WGS84 ellipsoid) for Cesium Google 3D Tiles.
    if args.geoid_offset_m is not None:
        geoid_offset_m = args.geoid_offset_m
    elif "2056" in crs_str:
        geoid_offset_m = 48.0
    elif "32633" in crs_str:
        geoid_offset_m = 40.0
    else:
        geoid_offset_m = 0.0

    to_wgs84 = pyproj.Transformer.from_crs(crs_str, "EPSG:4326", always_xy=True)

    frames_out: List[Dict[str, Any]] = []
    prev_xy: Optional[Tuple[float, float]] = None

    pos_errors: List[float] = []
    horiz_errors: List[float] = []
    vert_errors: List[float] = []
    rot_errors: List[float] = []
    proc_times: List[float] = []

    raw_frames = raw.get("frames", [])
    for idx, f in enumerate(raw_frames):
        frame_id = int(f.get("frame_id", idx))
        timestamp_s = round(frame_id / fps, 4)
        method = str(f.get("method", "none"))
        success = bool(f.get("success", False))

        if success and method == "keyframe":
            status = "LOCKED"
        elif success and method == "tracked":
            status = "TRACKING"
        elif method == "predicted":
            status = "PREDICTED"
        else:
            status = "LOST"

        rec: Dict[str, Any] = {
            "frame_id": frame_id,
            "timestamp_s": timestamp_s,
            "status": status,
            "method": method,
            "is_keyframe": bool(f.get("is_keyframe", False)),
            "success": success,
            "num_inliers": int(f.get("num_inliers") or 0),
            "num_tracked_points": int(f.get("num_tracked_points") or 0),
            "reproj_error_px": round(float(f["reproj_error"]), 3) if _finite(f.get("reproj_error")) else None,
            "processing_time_s": round(float(f["processing_time"]), 4) if _finite(f.get("processing_time")) else None,
            "kf_reason": f.get("kf_reason") or None,
        }

        if _finite(f.get("processing_time")):
            proc_times.append(float(f["processing_time"]))

        if _finite(f.get("est_x")) and _finite(f.get("est_y")) and _finite(f.get("est_z")):
            ex, ey, ez = float(f["est_x"]) + ox, float(f["est_y"]) + oy, float(f["est_z"]) + oz
            lon, lat = to_wgs84.transform(ex, ey)
            course_deg = None
            if prev_xy is not None:
                dx = ex - prev_xy[0]
                dy = ey - prev_xy[1]
                if math.hypot(dx, dy) > 0.25:
                    course_deg = round((math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0, 2)
            prev_xy = (ex, ey)

            rec["est"] = {
                "lat": round(float(lat), 7),
                "lon": round(float(lon), 7),
                "alt_map_m": round(ez, 2),
                "alt_wgs84_m": round(ez + geoid_offset_m, 2),
                "map_x": round(ex, 3),
                "map_y": round(ey, 3),
                "course_deg": course_deg,
            }
            if all(_finite(f.get(k)) for k in ("est_qw", "est_qx", "est_qy", "est_qz")):
                rec["est"].update(
                    quat_to_enu_vectors_and_hpr(
                        float(f["est_qw"]),
                        float(f["est_qx"]),
                        float(f["est_qy"]),
                        float(f["est_qz"]),
                    )
                )
        else:
            rec["est"] = None

        if _finite(f.get("gt_x")) and _finite(f.get("gt_y")) and _finite(f.get("gt_z")):
            gx, gy, gz = float(f["gt_x"]) + ox, float(f["gt_y"]) + oy, float(f["gt_z"]) + oz
            glon, glat = to_wgs84.transform(gx, gy)
            rec["gt"] = {
                "lat": round(float(glat), 7),
                "lon": round(float(glon), 7),
                "alt_map_m": round(gz, 2),
                "alt_wgs84_m": round(gz + geoid_offset_m, 2),
            }
            if all(_finite(f.get(k)) for k in ("gt_qw", "gt_qx", "gt_qy", "gt_qz")):
                rec["gt"].update(
                    quat_to_enu_vectors_and_hpr(
                        float(f["gt_qw"]),
                        float(f["gt_qx"]),
                        float(f["gt_qy"]),
                        float(f["gt_qz"]),
                    )
                )
        else:
            rec["gt"] = None

        err: Dict[str, Optional[float]] = {}
        for src_k, dst_k, bucket in (
            ("position_error", "pos_3d_m", pos_errors),
            ("horizontal_error", "horiz_m", horiz_errors),
            ("vertical_error", "vert_m", vert_errors),
            ("rotation_error", "rot_deg", rot_errors),
        ):
            if _finite(f.get(src_k)):
                val = float(f[src_k])
                err[dst_k] = round(val, 3)
                bucket.append(val)
        rec["error"] = err if err else None

        frames_out.append(rec)

    n_total = len(frames_out)
    n_localized = sum(1 for r in frames_out if r["success"] and r["est"] is not None)

    summary: Dict[str, Any] = {
        "total_frames": n_total,
        "localized_frames": n_localized,
        "coverage_pct": round(100.0 * n_localized / n_total, 1) if n_total else 0.0,
        "keyframes": int(raw.get("keyframes", 0)),
        "tracked": int(raw.get("tracked", 0)),
        "predicted": int(raw.get("predicted", 0)),
        "median_frame_time_s": round(float(np.median(proc_times)), 4) if proc_times else None,
        "effective_fps": round(1.0 / float(np.mean(proc_times)), 2) if proc_times and np.mean(proc_times) > 0 else None,
    }
    if horiz_errors:
        summary["metrics"] = {
            "median_horiz_m": round(float(np.median(horiz_errors)), 2),
            "p95_horiz_m": round(float(np.percentile(horiz_errors, 95)), 2),
            "median_vert_m": round(float(np.median(vert_errors)), 2) if vert_errors else None,
            "median_3d_m": round(float(np.median(pos_errors)), 2) if pos_errors else None,
            "median_rot_deg": round(float(np.median(rot_errors)), 2) if rot_errors else None,
        }

    payload = {
        "schema_version": "1.0",
        "source_crs": crs_str,
        "vertical_datum": vertical_datum,
        "geoid_offset_m": geoid_offset_m,
        "fps": round(fps, 4),
        "intrinsics": raw.get("intrinsics"),
        "device": raw.get("device"),
        "summary": summary,
        "frames": frames_out,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Exported {n_total} frames ({n_localized} localized, {summary['coverage_pct']}%) -> {args.out}")
    if "metrics" in summary:
        m = summary["metrics"]
        print(
            f"  GT accuracy: median horiz={m['median_horiz_m']} m, "
            f"p95 horiz={m['p95_horiz_m']} m, median vert={m['median_vert_m']} m, "
            f"median rot={m['median_rot_deg']} deg"
        )


if __name__ == "__main__":
    main()
