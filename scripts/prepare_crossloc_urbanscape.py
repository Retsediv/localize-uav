#!/usr/bin/env python3
"""Convert a CrossLoc Urbanscape real drone flight (EPFL / Ecublens, Switzerland)
into Groundframe-ready assets:
  - H.264 MP4 video (720x480)
  - OrthoTrack-compatible poses.csv (EPSG:2056 LV95 + LN02 + OpenCV-to-LV95 quaternion)
  - intrinsics.json (fx=480, fy=480, cx=360, cy=240, fov_vertical=53.13 deg)
  - DJI-compatible .SRT telemetry sidecar (WGS84 lat/lon + LN02 abs_alt)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np
import pyproj
import rasterio
from scipy.spatial.transform import Rotation

# CrossLoc Urbanscape ECEF origin (EPFL campus, Ecublens/Lausanne)
URBANSCAPE_ORIGIN_LON = 6.5668
URBANSCAPE_ORIGIN_LAT = 46.5191
URBANSCAPE_ORIGIN_ALT_WGS84 = 390.0


def ecef_to_lv95_rotation(
    ecef_pt: np.ndarray,
    to_wgs: pyproj.Transformer,
    to_lv95: pyproj.Transformer,
) -> Tuple[np.ndarray, float, float, float, float, float]:
    """Compute exact local ECEF -> LV95-ENU rotation matrix (accounting for grid convergence)
    and return (R_ecef2lv95, lv_e, lv_n, lat, lon, alt_wgs84)."""
    lat, lon, alt_wgs = to_wgs.transform(float(ecef_pt[0]), float(ecef_pt[1]), float(ecef_pt[2]))
    lv_e, lv_n = to_lv95.transform(lat, lon)

    sl, cl = math.sin(math.radians(lat)), math.cos(math.radians(lat))
    so, co = math.sin(math.radians(lon)), math.cos(math.radians(lon))
    R_ecef2enu = np.array(
        [
            [-so, co, 0.0],
            [-sl * co, -sl * so, cl],
            [cl * co, cl * so, sl],
        ],
        dtype=np.float64,
    )

    # Measure local grid convergence between geographic ENU and LV95 (E, N)
    p_east = ecef_pt + R_ecef2enu.T @ np.array([10.0, 0.0, 0.0])
    lat_e, lon_e, _ = to_wgs.transform(float(p_east[0]), float(p_east[1]), float(p_east[2]))
    e2, n2 = to_lv95.transform(lat_e, lon_e)
    dE, dN = e2 - lv_e, n2 - lv_n
    conv_rad = math.atan2(dN, dE)  # rotation from geographic East to LV95 East
    cc, sc = math.cos(conv_rad), math.sin(conv_rad)
    R_enu2lv95 = np.array(
        [
            [cc, -sc, 0.0],
            [sc, cc, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return R_enu2lv95 @ R_ecef2enu, float(lv_e), float(lv_n), float(lat), float(lon), float(alt_wgs)


def estimate_geoid_offset_from_dsm(
    split_dir: Path,
    flight_prefix: str,
    dsm_path: Path,
    ori_xyz: np.ndarray,
    to_wgs: pyproj.Transformer,
    to_lv95: pyproj.Transformer,
) -> float:
    """Measure exact (WGS84 ellipsoidal - swisstopo LN02) vertical offset from init 3D ground points."""
    import torch

    init_files = sorted((split_dir / "init").glob(f"{flight_prefix}_*.dat"))
    if not init_files or not dsm_path.exists():
        return 49.96

    diffs: List[np.ndarray] = []
    with rasterio.open(dsm_path) as src:
        for inf in init_files[:5]:
            pc = torch.load(inf, weights_only=False).numpy()
            mask = pc[0] != -1
            pts = pc[:, mask].T
            if len(pts) == 0:
                continue
            pts_ecef = pts + ori_xyz
            lats, lons, alts_wgs = to_wgs.transform(pts_ecef[:, 0], pts_ecef[:, 1], pts_ecef[:, 2])
            lvx, lvy = to_lv95.transform(lats, lons)
            dsm_vals = np.array([v[0] for v in src.sample(zip(lvx, lvy))], dtype=np.float64)
            valid = np.isfinite(dsm_vals) & (dsm_vals > 300.0) & (dsm_vals < 700.0)
            if np.any(valid):
                diffs.append(alts_wgs[valid] - dsm_vals[valid])
    if not diffs:
        return 49.96
    return float(np.median(np.concatenate(diffs)))


def _fmt_srt_time(sec: float) -> str:
    ms_total = int(round(max(0.0, sec) * 1000))
    ms = ms_total % 1000
    s_total = ms_total // 1000
    s = s_total % 60
    m = (s_total // 60) % 60
    h = s_total // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare CrossLoc Urbanscape flight for Groundframe.")
    parser.add_argument(
        "--split-dir",
        type=Path,
        default=Path("data/crossloc/urbanscape-testing-set/urbanscape/test_drone_real"),
    )
    parser.add_argument("--flight", type=str, default="urbanscape-2020-09-24-planned")
    parser.add_argument("--max-frames", type=int, default=100)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--map-dir", type=Path, default=Path("data/maps/epfl_urbanscape"))
    parser.add_argument("--out-dir", type=Path, default=Path("data/videos/epfl_urbanscape"))
    parser.add_argument("--stem", type=str, default="epfl_urbanscape_oblique60_100f")
    args = parser.parse_args()

    rgb_files = sorted((args.split_dir / "rgb").glob(f"{args.flight}_*.png"))
    if not rgb_files:
        raise FileNotFoundError(f"No PNGs matching {args.flight}_*.png in {args.split_dir / 'rgb'}")
    rgb_files = rgb_files[: args.max_frames]

    to_ecef = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978")
    to_wgs = pyproj.Transformer.from_crs("EPSG:4978", "EPSG:4979")
    to_lv95 = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:2056")

    ori_xyz = np.array(
        to_ecef.transform(URBANSCAPE_ORIGIN_LAT, URBANSCAPE_ORIGIN_LON, URBANSCAPE_ORIGIN_ALT_WGS84),
        dtype=np.float64,
    )

    dsm_candidates = sorted((args.map_dir / "cache").glob("dsm_*.tif"))
    dsm_path = dsm_candidates[0] if dsm_candidates else Path()
    geoid_offset_m = estimate_geoid_offset_from_dsm(
        args.split_dir, args.flight, dsm_path, ori_xyz, to_wgs, to_lv95
    )
    print(f"Estimated geoid offset (CrossLoc WGS84 - swisstopo LN02): {geoid_offset_m:.3f} m")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = args.out_dir / f"{args.stem}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*"):
        old.unlink()

    # CrossLoc names real JPEG drone images with a .png suffix; detect actual image format
    magic = rgb_files[0].read_bytes()[:4]
    frame_ext = ".jpg" if magic[:2] == b"\xff\xd8" else ".png"

    sample_img = cv2.imread(str(rgb_files[0]))
    h, w = sample_img.shape[:2]
    f_val = float((args.split_dir / "calibration" / f"{rgb_files[0].stem}.txt").read_text().strip())
    fov_v = math.degrees(2.0 * math.atan((h / 2.0) / f_val))

    intrinsics = {
        "fov_vertical": round(fov_v, 4),
        "fx": f_val,
        "fy": f_val,
        "cx": w / 2.0,
        "cy": h / 2.0,
        "width": w,
        "height": h,
    }
    intr_path = args.out_dir / f"{args.stem}_intrinsics.json"
    intr_path.write_text(json.dumps(intrinsics, indent=2), encoding="utf-8")

    poses_csv = args.out_dir / f"{args.stem}_poses.csv"
    srt_path = args.out_dir / f"{args.stem}.srt"
    srt_blocks: List[str] = []

    with open(poses_csv, "w", newline="", encoding="utf-8") as cf:
        writer = csv.writer(cf)
        writer.writerow(["frame_id", "x", "y", "z", "qw", "qx", "qy", "qz", "latitude", "longitude", "altitude", "fov_vertical"])

        for fid, rgb_p in enumerate(rgb_files):
            pose_p = args.split_dir / "poses" / f"{rgb_p.stem}.txt"
            M = np.loadtxt(pose_p)  # 4x4 cam-to-world (OpenCV cam -> shifted ECEF)
            ecef_cam = M[:3, 3] + ori_xyz
            R_e2lv, lv_e, lv_n, lat, lon, alt_wgs = ecef_to_lv95_rotation(ecef_cam, to_wgs, to_lv95)
            alt_ln02 = alt_wgs - geoid_offset_m

            R_c2lv = R_e2lv @ M[:3, :3]
            q_xyzw = Rotation.from_matrix(R_c2lv).as_quat()
            qw, qx, qy, qz = float(q_xyzw[3]), float(q_xyzw[0]), float(q_xyzw[1]), float(q_xyzw[2])

            writer.writerow([
                fid,
                f"{lv_e:.6f}",
                f"{lv_n:.6f}",
                f"{alt_ln02:.6f}",
                f"{qw:.8f}",
                f"{qx:.8f}",
                f"{qy:.8f}",
                f"{qz:.8f}",
                f"{lat:.8f}",
                f"{lon:.8f}",
                f"{alt_ln02:.4f}",
                f"{fov_v:.4f}",
            ])

            t0 = fid / args.fps
            t1 = (fid + 1) / args.fps
            srt_blocks.append(
                f"{fid + 1}\n"
                f"{_fmt_srt_time(t0)} --> {_fmt_srt_time(t1)}\n"
                f"FrameCnt: {fid + 1} [latitude: {lat:.7f}] [longitude: {lon:.7f}] [abs_alt: {alt_ln02:.2f}]\n"
            )

            # Symlink sequentially with true extension for ffmpeg encoding
            dst_frame = frames_dir / f"frame_{fid:05d}{frame_ext}"
            dst_frame.symlink_to(rgb_p.resolve())

    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")

    mp4_path = args.out_dir / f"{args.stem}.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(args.fps),
            "-i",
            str(frames_dir / f"frame_%05d{frame_ext}"),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(mp4_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Clean up temporary frame symlinks
    for old in frames_dir.glob("*"):
        old.unlink()
    frames_dir.rmdir()

    print(f"Prepared {len(rgb_files)} frames ({w}x{h}, fov_v={fov_v:.2f} deg, {args.fps} fps):")
    print(f"  Video:      {mp4_path}")
    print(f"  GT poses:   {poses_csv}")
    print(f"  GT SRT:     {srt_path}")
    print(f"  Intrinsics: {intr_path}")
    print(f"  Geoid ΔZ:   +{geoid_offset_m:.2f} m (use --geoid-offset-m {geoid_offset_m:.2f} in export_trajectory.py)")


if __name__ == "__main__":
    main()
