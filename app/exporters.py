"""Export Groundframe trajectory.json into RFC 7946 GeoJSON and 3D KML."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Dict, List, Optional


def _pick_est(frame: Dict[str, Any], smoothed: bool) -> Optional[Dict[str, Any]]:
    if smoothed and frame.get("est_smooth") is not None:
        return frame["est_smooth"]
    return frame.get("est")


def trajectory_to_geojson(
    traj: Dict[str, Any],
    name: str = "Groundframe Flight",
    smoothed: bool = True,
) -> str:
    features: List[Dict[str, Any]] = []
    est_coords: List[List[float]] = []
    gt_coords: List[List[float]] = []
    used_smoothed = False

    for f in traj.get("frames", []):
        if smoothed and f.get("est_smooth") is not None:
            used_smoothed = True
        est = _pick_est(f, smoothed)
        if est is not None:
            coord = [est["lon"], est["lat"], est.get("alt_wgs84_m", est.get("alt_map_m", 0.0))]
            est_coords.append(coord)
            if f.get("is_keyframe"):
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": coord},
                    "properties": {
                        "kind": "keyframe",
                        "frame_id": f["frame_id"],
                        "timestamp_s": f["timestamp_s"],
                        "status": f["status"],
                        "smoothed": smoothed and f.get("est_smooth") is not None,
                        "num_inliers": f.get("num_inliers", 0),
                        "heading_deg": est.get("heading_deg"),
                        "pitch_deg": est.get("pitch_deg"),
                        "alt_map_m": est.get("alt_map_m"),
                        "alt_wgs84_m": est.get("alt_wgs84_m"),
                    },
                })
        gt = f.get("gt")
        if gt is not None:
            gt_coords.append([gt["lon"], gt["lat"], gt.get("alt_wgs84_m", gt.get("alt_map_m", 0.0))])

    if est_coords:
        features.insert(0, {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": est_coords},
            "properties": {
                "kind": "estimated_trajectory",
                "name": name,
                "smoothed": used_smoothed,
                "source_crs": traj.get("source_crs"),
                "summary": traj.get("summary"),
            },
        })

    if gt_coords:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": gt_coords},
            "properties": {
                "kind": "ground_truth_trajectory",
                "name": f"{name} (Ground Truth)",
            },
        })

    return json.dumps({"type": "FeatureCollection", "features": features}, indent=2)


def trajectory_to_kml(
    traj: Dict[str, Any],
    name: str = "Groundframe Flight",
    smoothed: bool = True,
) -> str:
    est_pts: List[str] = []
    gt_pts: List[str] = []
    kf_placemarks: List[str] = []
    used_smoothed = False

    for f in traj.get("frames", []):
        if smoothed and f.get("est_smooth") is not None:
            used_smoothed = True
        est = _pick_est(f, smoothed)
        if est is not None:
            alt = est.get("alt_wgs84_m", est.get("alt_map_m", 0.0))
            est_pts.append(f"{est['lon']},{est['lat']},{alt}")
            if f.get("is_keyframe"):
                desc = (
                    f"Frame {f['frame_id']} ({f['timestamp_s']}s) | "
                    f"Inliers: {f.get('num_inliers', 0)} | "
                    f"Alt: {est.get('alt_map_m')}m"
                )
                kf_placemarks.append(
                    f"""    <Placemark>
      <name>KF #{f['frame_id']}</name>
      <description>{escape(desc)}</description>
      <styleUrl>#kfStyle</styleUrl>
      <Point>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>{est['lon']},{est['lat']},{alt}</coordinates>
      </Point>
    </Placemark>"""
                )
        gt = f.get("gt")
        if gt is not None:
            alt_g = gt.get("alt_wgs84_m", gt.get("alt_map_m", 0.0))
            gt_pts.append(f"{gt['lon']},{gt['lat']},{alt_g}")

    est_line = ""
    if est_pts:
        est_label = "Smoothed" if used_smoothed else "Estimated"
        est_line = f"""    <Placemark>
      <name>{escape(name)} ({est_label})</name>
      <styleUrl>#estLine</styleUrl>
      <LineString>
        <extrude>1</extrude>
        <tessellate>1</tessellate>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>{' '.join(est_pts)}</coordinates>
      </LineString>
    </Placemark>"""

    gt_line = ""
    if gt_pts:
        gt_line = f"""    <Placemark>
      <name>{escape(name)} (Ground Truth)</name>
      <styleUrl>#gtLine</styleUrl>
      <LineString>
        <tessellate>1</tessellate>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>{' '.join(gt_pts)}</coordinates>
      </LineString>
    </Placemark>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{escape(name)}</name>
    <Style id="estLine">
      <LineStyle><color>ffbef280</color><width>3</width></LineStyle>
      <PolyStyle><color>33bef280</color></PolyStyle>
    </Style>
    <Style id="gtLine">
      <LineStyle><color>ff77c4ff</color><width>2</width></LineStyle>
    </Style>
    <Style id="kfStyle">
      <IconStyle><color>ffbef280</color><scale>0.8</scale></IconStyle>
    </Style>
{est_line}
{gt_line}
{chr(10).join(kf_placemarks)}
  </Document>
</kml>
"""
