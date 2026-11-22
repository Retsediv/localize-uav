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


def enu_vectors_to_hpr(
    forward_enu: np.ndarray,
    up_enu: np.ndarray,
) -> Tuple[Dict[str, Any], np.ndarray]:
    """Gram-Schmidt orthonormalize (forward_enu, up_enu) and compute HPR + R_c2w (OpenCV -> ENU)."""
    f = np.asarray(forward_enu, dtype=float)
    f = f / max(float(np.linalg.norm(f)), 1e-12)

    u = np.asarray(up_enu, dtype=float)
    u = u - float(np.dot(u, f)) * f
    u_norm = float(np.linalg.norm(u))
    if u_norm < 1e-6:
        ref = np.array([0.0, 0.0, 1.0]) if abs(float(f[2])) < 0.9 else np.array([0.0, 1.0, 0.0])
        u = ref - float(np.dot(ref, f)) * f
        u = u / max(float(np.linalg.norm(u)), 1e-12)
    else:
        u = u / u_norm

    r = np.cross(f, u)
    r = r / max(float(np.linalg.norm(r)), 1e-12)

    # Heading: azimuth clockwise from North in degrees [0, 360)
    horiz = math.hypot(float(f[0]), float(f[1]))
    if horiz > 1e-4:
        heading_deg = (math.degrees(math.atan2(float(f[0]), float(f[1]))) + 360.0) % 360.0
    else:
        # Near-nadir: derive heading from camera-up vector projected horizontally
        heading_deg = (math.degrees(math.atan2(float(u[0]), float(u[1]))) + 360.0) % 360.0

    # Pitch: -90 = straight down (nadir), 0 = horizon, +90 = straight up
    pitch_deg = math.degrees(math.asin(max(-1.0, min(1.0, float(f[2])))))

    # Roll: angle between camera right axis and horizontal right axis perpendicular to forward
    world_up = np.array([0.0, 0.0, 1.0])
    horiz_right = np.cross(f, world_up)
    hr_norm = float(np.linalg.norm(horiz_right))
    if hr_norm > 1e-4:
        horiz_right /= hr_norm
        horiz_up = np.cross(horiz_right, f)
        roll_deg = math.degrees(math.atan2(float(np.dot(r, horiz_up)), float(np.dot(r, horiz_right))))
    else:
        roll_deg = 0.0

    R_c2w = np.column_stack([r, -u, f])
    out = {
        "forward_enu": [round(float(v), 6) for v in f],
        "up_enu": [round(float(v), 6) for v in u],
        "heading_deg": round(heading_deg, 2),
        "pitch_deg": round(pitch_deg, 2),
        "roll_deg": round(roll_deg, 2),
    }
    return out, R_c2w


def quat_to_enu_vectors_and_hpr(
    qw: float, qx: float, qy: float, qz: float
) -> Dict[str, Any]:
    """Convert OrthoTrack R_c2w quaternion (OpenCV cam -> ENU world) to Cesium-friendly vectors and HPR.

    OpenCV camera frame: +X right, +Y down, +Z forward.
    Local ENU frame:     +X East,  +Y North, +Z Up.
    """
    R_c2w = Rotation.from_quat([qx, qy, qz, qw]).as_matrix()
    up_enu = -R_c2w[:, 1]
    forward_enu = R_c2w[:, 2]
    out, _ = enu_vectors_to_hpr(forward_enu, up_enu)
    return out


def _rts_smooth_1d(
    ts: np.ndarray,
    ys: np.ndarray,
    rs: np.ndarray,
    q: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Zero-phase Rauch-Tung-Striebel (RTS) constant-velocity Kalman smoother (1D).

    State x_k = [position, velocity]^T with continuous white-noise acceleration
    process spectral density `q` and per-frame measurement variance `rs[k]`.
    """
    n = len(ys)
    if n <= 1:
        return ys.astype(float).copy(), np.zeros(n, dtype=float)

    x_pred = np.zeros((n, 2), dtype=float)
    P_pred = np.zeros((n, 2, 2), dtype=float)
    x_filt = np.zeros((n, 2), dtype=float)
    P_filt = np.zeros((n, 2, 2), dtype=float)
    Fs = np.zeros((n, 2, 2), dtype=float)

    k_init = min(n - 1, 10)
    dt_init = float(ts[k_init] - ts[0])
    v0 = float(ys[k_init] - ys[0]) / dt_init if dt_init > 1e-6 else 0.0

    xp = np.array([float(ys[0]), v0], dtype=float)
    Pp = np.diag([max(float(rs[0]) * 10.0, 1e-3), max(abs(v0) * 10.0, 10.0) ** 2])

    for k in range(n):
        if k > 0:
            dt = max(float(ts[k] - ts[k - 1]), 1e-4)
            F = np.array([[1.0, dt], [0.0, 1.0]], dtype=float)
            Q = q * np.array([[dt**3 / 3.0, dt**2 / 2.0], [dt**2 / 2.0, dt]], dtype=float)
            Fs[k] = F
            xp = F @ x_filt[k - 1]
            Pp = F @ P_filt[k - 1] @ F.T + Q
        x_pred[k] = xp
        P_pred[k] = Pp

        R = max(float(rs[k]), 1e-8)
        S = float(Pp[0, 0]) + R
        K = Pp[:, 0] / S
        xf = xp + K * (float(ys[k]) - float(xp[0]))
        I_KH = np.array([[1.0 - K[0], 0.0], [-K[1], 1.0]], dtype=float)
        Pf = I_KH @ Pp @ I_KH.T + R * np.outer(K, K)
        x_filt[k] = xf
        P_filt[k] = Pf

    x_sm = x_filt.copy()
    for k in range(n - 2, -1, -1):
        F = Fs[k + 1]
        P_next = P_pred[k + 1]
        rhs = (P_filt[k] @ F.T).T
        try:
            G = np.linalg.solve(P_next.T, rhs).T
        except np.linalg.LinAlgError:
            G = (np.linalg.pinv(P_next.T) @ rhs).T
        x_sm[k] = x_filt[k] + G @ (x_sm[k + 1] - x_pred[k + 1])

    return x_sm[:, 0], x_sm[:, 1]


def _rts_irls_smooth_1d(
    ts: np.ndarray,
    ys: np.ndarray,
    rs: np.ndarray,
    q: float,
    huber_k: float = 1.5,
    iters: int = 2,
) -> Tuple[np.ndarray, np.ndarray]:
    """Two-pass Huber-reweighted zero-phase RTS smoother for outlier & KF-step resistance."""
    r_eff = rs.astype(float).copy()
    pos, vel = _rts_smooth_1d(ts, ys, r_eff, q)
    for _ in range(max(0, iters - 1)):
        resid = np.abs(ys - pos)
        sig = np.sqrt(np.maximum(rs, 1e-8))
        u = resid / sig
        scale = np.where(u > huber_k, u / huber_k, 1.0)
        r_eff = rs * np.clip(scale, 1.0, 25.0)
        pos, vel = _rts_smooth_1d(ts, ys, r_eff, q)
    return pos, vel


def _segment_quality_weights(segment_recs: List[Dict[str, Any]]) -> np.ndarray:
    """Compute adaptive observation variance multiplier per frame from num_inliers & reproj_error_px."""
    reps_valid = [
        float(r["reproj_error_px"])
        for r in segment_recs
        if _finite(r.get("reproj_error_px")) and float(r["reproj_error_px"]) > 0.0
    ]
    med_rep = float(np.median(reps_valid)) if reps_valid else 1.5

    weights = np.ones(len(segment_recs), dtype=float)
    for i, rec in enumerate(segment_recs):
        status = rec.get("status", "LOST")
        inliers = int(rec.get("num_inliers") or 0)
        rep = rec.get("reproj_error_px")

        if status == "PREDICTED" or inliers <= 0:
            w_inl = 40.0
            w_rep = 10.0
        else:
            w_inl = min(25.0, max(0.25, 1500.0 / max(inliers, 30)))
            rep_val = float(rep) if (_finite(rep) and float(rep) > 0.0) else med_rep
            w_rep = min(25.0, max(0.5, (rep_val / 1.5) ** 2))
        weights[i] = w_inl * w_rep
    return weights


def _rotation_geodesic_deg(R_a: np.ndarray, R_b: np.ndarray) -> float:
    tr = float(np.trace(R_a.T @ R_b))
    cos_ang = max(-1.0, min(1.0, (tr - 1.0) * 0.5))
    return math.degrees(math.acos(cos_ang))


def apply_temporal_smoothing(
    frames_out: List[Dict[str, Any]],
    raw_frames: List[Dict[str, Any]],
    to_wgs84: pyproj.Transformer,
    geoid_offset_m: float,
    offset: Tuple[float, float, float],
) -> Optional[Dict[str, Any]]:
    """Populate `est_smooth` (and `error_smooth` when GT exists) over contiguous localized segments.

    Returns `metrics_smooth` dict if GT is available, else None.
    """
    ox, oy, oz = offset

    # Base observation variances and continuous white-noise acceleration densities
    R_BASE_XY = 0.25          # (0.5 m)^2
    R_BASE_Z = 0.36           # (0.6 m)^2
    Q_XY = 2.0                # m^2 / s^3
    Q_Z = 0.35                # m^2 / s^3
    R_BASE_ROT = 0.004 ** 2   # unit-vector variance
    Q_ROT = 0.05              # rad^2 / s^3

    # Partition into contiguous non-LOST segments without stitching across gaps
    segments: List[List[int]] = []
    cur_seg: List[int] = []
    prev_ts: Optional[float] = None

    for idx, rec in enumerate(frames_out):
        est = rec.get("est")
        is_loc = est is not None and rec.get("status") != "LOST"
        if not is_loc:
            if cur_seg:
                segments.append(cur_seg)
                cur_seg = []
                prev_ts = None
            rec["est_smooth"] = None
            rec["error_smooth"] = None
            continue

        ts = float(rec["timestamp_s"])
        if prev_ts is not None and (ts <= prev_ts or (ts - prev_ts) > 2.0):
            segments.append(cur_seg)
            cur_seg = []
        cur_seg.append(idx)
        prev_ts = ts

    if cur_seg:
        segments.append(cur_seg)

    sm_pos_errors: List[float] = []
    sm_horiz_errors: List[float] = []
    sm_vert_errors: List[float] = []
    sm_rot_errors: List[float] = []

    for seg in segments:
        seg_recs = [frames_out[i] for i in seg]
        n = len(seg)
        ts = np.array([float(r["timestamp_s"]) for r in seg_recs], dtype=float)
        xs = np.array([float(r["est"]["map_x"]) for r in seg_recs], dtype=float)
        ys = np.array([float(r["est"]["map_y"]) for r in seg_recs], dtype=float)
        zs = np.array([float(r["est"]["alt_map_m"]) for r in seg_recs], dtype=float)

        w = _segment_quality_weights(seg_recs)

        sx, svx = _rts_irls_smooth_1d(ts, xs, R_BASE_XY * w, Q_XY)
        sy, svy = _rts_irls_smooth_1d(ts, ys, R_BASE_XY * w, Q_XY)
        sz, _ = _rts_irls_smooth_1d(ts, zs, R_BASE_Z * w, Q_Z)

        # Orientation smoothing over frames in this segment that have forward_enu & up_enu
        rot_sub_idx = [
            k for k, r in enumerate(seg_recs)
            if r["est"].get("forward_enu") is not None and r["est"].get("up_enu") is not None
        ]
        rot_smooth_map: Dict[int, Tuple[Dict[str, Any], np.ndarray]] = {}
        if rot_sub_idx:
            r_ts = ts[rot_sub_idx]
            r_w = w[rot_sub_idx]
            fwd_arr = np.array([seg_recs[k]["est"]["forward_enu"] for k in rot_sub_idx], dtype=float)
            up_arr = np.array([seg_recs[k]["est"]["up_enu"] for k in rot_sub_idx], dtype=float)
            sf = np.column_stack([
                _rts_irls_smooth_1d(r_ts, fwd_arr[:, j], R_BASE_ROT * r_w, Q_ROT)[0]
                for j in range(3)
            ])
            su = np.column_stack([
                _rts_irls_smooth_1d(r_ts, up_arr[:, j], R_BASE_ROT * r_w, Q_ROT)[0]
                for j in range(3)
            ])
            for m_idx, seg_k in enumerate(rot_sub_idx):
                rot_smooth_map[seg_k] = enu_vectors_to_hpr(sf[m_idx], su[m_idx])

        lons, lats = to_wgs84.transform(sx, sy)

        for k, global_idx in enumerate(seg):
            rec = frames_out[global_idx]
            ex, ey, ez = float(sx[k]), float(sy[k]), float(sz[k])
            lat, lon = float(lats[k]), float(lons[k])

            course_deg: Optional[float] = None
            if k > 0:
                dx = ex - float(sx[k - 1])
                dy = ey - float(sy[k - 1])
                if math.hypot(dx, dy) > 0.1:
                    course_deg = round((math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0, 2)
            if course_deg is None and math.hypot(float(svx[k]), float(svy[k])) > 0.4:
                course_deg = round((math.degrees(math.atan2(float(svx[k]), float(svy[k]))) + 360.0) % 360.0, 2)

            est_sm: Dict[str, Any] = {
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "alt_map_m": round(ez, 2),
                "alt_wgs84_m": round(ez + geoid_offset_m, 2),
                "map_x": round(ex, 3),
                "map_y": round(ey, 3),
                "course_deg": course_deg,
            }

            R_sm: Optional[np.ndarray] = None
            if k in rot_smooth_map:
                hpr_dict, R_sm = rot_smooth_map[k]
                est_sm.update(hpr_dict)
            rec["est_smooth"] = est_sm

            # Recompute GT error against smoothed pose when GT is present
            f_raw = raw_frames[global_idx]
            if _finite(f_raw.get("gt_x")) and _finite(f_raw.get("gt_y")) and _finite(f_raw.get("gt_z")):
                gx = float(f_raw["gt_x"]) + ox
                gy = float(f_raw["gt_y"]) + oy
                gz = float(f_raw["gt_z"]) + oz
                h_err = math.hypot(ex - gx, ey - gy)
                v_err = abs(ez - gz)
                p3_err = math.sqrt((ex - gx) ** 2 + (ey - gy) ** 2 + (ez - gz) ** 2)
                err_sm: Dict[str, Optional[float]] = {
                    "pos_3d_m": round(p3_err, 3),
                    "horiz_m": round(h_err, 3),
                    "vert_m": round(v_err, 3),
                }
                sm_pos_errors.append(p3_err)
                sm_horiz_errors.append(h_err)
                sm_vert_errors.append(v_err)

                if R_sm is not None and all(_finite(f_raw.get(q_k)) for q_k in ("gt_qw", "gt_qx", "gt_qy", "gt_qz")):
                    R_gt = Rotation.from_quat([
                        float(f_raw["gt_qx"]),
                        float(f_raw["gt_qy"]),
                        float(f_raw["gt_qz"]),
                        float(f_raw["gt_qw"]),
                    ]).as_matrix()
                    r_err = _rotation_geodesic_deg(R_sm, R_gt)
                    err_sm["rot_deg"] = round(r_err, 3)
                    sm_rot_errors.append(r_err)

                rec["error_smooth"] = err_sm
            else:
                rec["error_smooth"] = None

    if sm_horiz_errors:
        return {
            "median_horiz_m": round(float(np.median(sm_horiz_errors)), 2),
            "p95_horiz_m": round(float(np.percentile(sm_horiz_errors, 95)), 2),
            "median_vert_m": round(float(np.median(sm_vert_errors)), 2) if sm_vert_errors else None,
            "median_3d_m": round(float(np.median(sm_pos_errors)), 2) if sm_pos_errors else None,
            "median_rot_deg": round(float(np.median(sm_rot_errors)), 2) if sm_rot_errors else None,
        }
    return None


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
    parser.add_argument(
        "--no-smooth",
        action="store_true",
        help="Disable zero-phase RTS Kalman temporal smoothing (omit `est_smooth`).",
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

    metrics_smooth: Optional[Dict[str, Any]] = None
    if not args.no_smooth:
        metrics_smooth = apply_temporal_smoothing(
            frames_out=frames_out,
            raw_frames=raw_frames,
            to_wgs84=to_wgs84,
            geoid_offset_m=geoid_offset_m,
            offset=(ox, oy, oz),
        )

    n_total = len(frames_out)
    n_localized = sum(1 for r in frames_out if r["success"] and r["est"] is not None)

    summary: Dict[str, Any] = {
        "total_frames": n_total,
        "localized_frames": n_localized,
        "coverage_pct": round(100.0 * n_localized / n_total, 1) if n_total else 0.0,
        "keyframes": int(raw.get("keyframes", 0)),
        "tracked": int(raw.get("tracked", 0)),
        "predicted": int(raw.get("predicted", 0)),
        "smoothed": not args.no_smooth,
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
    if metrics_smooth is not None:
        summary["metrics_smooth"] = metrics_smooth

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
    smooth_tag = " + RTS smooth" if not args.no_smooth else " (raw only)"
    print(f"Exported {n_total} frames ({n_localized} localized, {summary['coverage_pct']}%{smooth_tag}) -> {args.out}")
    if "metrics" in summary:
        m = summary["metrics"]
        print(
            f"  GT accuracy (raw):    median horiz={m['median_horiz_m']} m, "
            f"p95 horiz={m['p95_horiz_m']} m, median vert={m['median_vert_m']} m, "
            f"median rot={m['median_rot_deg']} deg"
        )
    if "metrics_smooth" in summary:
        ms = summary["metrics_smooth"]
        print(
            f"  GT accuracy (smooth): median horiz={ms['median_horiz_m']} m, "
            f"p95 horiz={ms['p95_horiz_m']} m, median vert={ms['median_vert_m']} m, "
            f"median rot={ms['median_rot_deg']} deg"
        )


if __name__ == "__main__":
    main()
