"""Ground Truth parser & trajectory joiner for DJI .SRT telemetry subtitles
and OrthoTrack poses.csv validation.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from app.vlm import haversine_m

_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)
_LAT_RE = re.compile(r"\[?latitude\s*[:=]\s*([-+]?\d+(?:\.\d+)?)\]?", re.I)
_LON_RE = re.compile(r"\[?longitude\s*[:=]\s*([-+]?\d+(?:\.\d+)?)\]?", re.I)
_ABS_ALT_RE = re.compile(r"abs_alt\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", re.I)
_REL_ALT_RE = re.compile(r"rel_alt\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", re.I)
_GPS_TUPLE_RE = re.compile(r"GPS\s*\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*(?:,\s*([-+]?\d+(?:\.\d+)?))?\s*\)", re.I)
_FRAME_CNT_RE = re.compile(r"FrameCnt\s*[:=]\s*(\d+)", re.I)


def _hms_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600.0 + int(m) * 60.0 + int(s) + int(ms) / 1000.0


def parse_dji_srt(srt_path: Path) -> List[Dict[str, Any]]:
    """Parse per-frame GPS telemetry from a DJI .SRT subtitle file."""
    text = srt_path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\r?\n\r?\n+", text.strip())
    records: List[Dict[str, Any]] = []

    for idx, block in enumerate(blocks):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue

        t_start = None
        for ln in lines[:3]:
            tm = _TIME_RE.search(ln)
            if tm:
                t_start = _hms_to_sec(tm.group(1), tm.group(2), tm.group(3), tm.group(4))
                break

        joined = " ".join(lines)
        lat = lon = alt = None

        mlat = _LAT_RE.search(joined)
        mlon = _LON_RE.search(joined)
        if mlat and mlon:
            lat = float(mlat.group(1))
            lon = float(mlon.group(1))
        else:
            mgps = _GPS_TUPLE_RE.search(joined)
            if mgps:
                # Legacy DJI SRT prints GPS(lon, lat, alt)
                lon = float(mgps.group(1))
                lat = float(mgps.group(2))
                if mgps.group(3) is not None:
                    alt = float(mgps.group(3))

        if lat is None or lon is None:
            continue

        m_abs = _ABS_ALT_RE.search(joined)
        m_rel = _REL_ALT_RE.search(joined)
        if m_abs:
            alt = float(m_abs.group(1))
        elif m_rel and alt is None:
            alt = float(m_rel.group(1))

        m_fc = _FRAME_CNT_RE.search(joined)
        frame_id = int(m_fc.group(1)) - 1 if m_fc else idx

        records.append({
            "frame_id": frame_id,
            "timestamp_s": t_start,
            "lat": lat,
            "lon": lon,
            "alt_m": alt,
            "alt_is_abs": bool(m_abs),
        })

    return records


def apply_dji_srt_to_trajectory(trajectory_path: Path, srt_path: Path) -> Dict[str, Any]:
    """Join DJI .SRT GPS telemetry onto an exported trajectory.json and compute error metrics."""
    traj = json.loads(trajectory_path.read_text(encoding="utf-8"))
    srt_records = parse_dji_srt(srt_path)
    if not srt_records:
        return traj

    by_frame = {r["frame_id"]: r for r in srt_records}
    timed = [r for r in srt_records if r["timestamp_s"] is not None]
    geoid_offset = float(traj.get("geoid_offset_m") or 0.0)

    horiz_errors: List[float] = []
    vert_errors: List[float] = []
    sm_horiz_errors: List[float] = []
    sm_vert_errors: List[float] = []

    for frame in traj.get("frames", []):
        fid = int(frame["frame_id"])
        rec = by_frame.get(fid)
        if rec is None and timed:
            ts = float(frame.get("timestamp_s") or 0.0)
            nearest = min(timed, key=lambda r: abs(r["timestamp_s"] - ts))
            if abs(nearest["timestamp_s"] - ts) <= 0.25:
                rec = nearest
        if rec is None:
            continue

        gt_alt_map = rec["alt_m"] if rec["alt_m"] is not None else None
        gt_alt_wgs = (gt_alt_map + geoid_offset) if (gt_alt_map is not None and rec["alt_is_abs"]) else gt_alt_map

        frame["gt"] = {
            "lat": round(rec["lat"], 7),
            "lon": round(rec["lon"], 7),
            "alt_map_m": round(gt_alt_map, 2) if gt_alt_map is not None else None,
            "alt_wgs84_m": round(gt_alt_wgs, 2) if gt_alt_wgs is not None else None,
            "source": "dji_srt",
        }

        est = frame.get("est")
        if est is not None:
            h_err = haversine_m(float(est["lat"]), float(est["lon"]), rec["lat"], rec["lon"])
            horiz_errors.append(h_err)
            err_obj: Dict[str, Any] = {"horiz_m": round(h_err, 3)}
            if gt_alt_map is not None and rec["alt_is_abs"] and est.get("alt_map_m") is not None:
                v_err = abs(float(est["alt_map_m"]) - gt_alt_map)
                vert_errors.append(v_err)
                err_obj["vert_m"] = round(v_err, 3)
                err_obj["pos_3d_m"] = round(math.hypot(h_err, v_err), 3)
            frame["error"] = err_obj

        est_sm = frame.get("est_smooth")
        if est_sm is not None:
            sh_err = haversine_m(float(est_sm["lat"]), float(est_sm["lon"]), rec["lat"], rec["lon"])
            sm_horiz_errors.append(sh_err)
            serr_obj: Dict[str, Any] = {"horiz_m": round(sh_err, 3)}
            if gt_alt_map is not None and rec["alt_is_abs"] and est_sm.get("alt_map_m") is not None:
                sv_err = abs(float(est_sm["alt_map_m"]) - gt_alt_map)
                sm_vert_errors.append(sv_err)
                serr_obj["vert_m"] = round(sv_err, 3)
                serr_obj["pos_3d_m"] = round(math.hypot(sh_err, sv_err), 3)
            frame["error_smooth"] = serr_obj

    if horiz_errors:
        summary = traj.setdefault("summary", {})
        metrics: Dict[str, Any] = {
            "median_horiz_m": round(float(np.median(horiz_errors)), 2),
            "p95_horiz_m": round(float(np.percentile(horiz_errors, 95)), 2),
            "gt_source": "dji_srt",
            "gt_matched_frames": len(horiz_errors),
        }
        if vert_errors:
            metrics["median_vert_m"] = round(float(np.median(vert_errors)), 2)
        summary["metrics"] = metrics

    if sm_horiz_errors:
        summary = traj.setdefault("summary", {})
        metrics_sm: Dict[str, Any] = {
            "median_horiz_m": round(float(np.median(sm_horiz_errors)), 2),
            "p95_horiz_m": round(float(np.percentile(sm_horiz_errors, 95)), 2),
            "gt_source": "dji_srt",
            "gt_matched_frames": len(sm_horiz_errors),
        }
        if sm_vert_errors:
            metrics_sm["median_vert_m"] = round(float(np.median(sm_vert_errors)), 2)
        summary["metrics_smooth"] = metrics_sm

    trajectory_path.write_text(json.dumps(traj, indent=2), encoding="utf-8")
    return traj
