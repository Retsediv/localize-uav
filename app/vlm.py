"""OpenRouter Vision-based coarse region detection (Video-Only Cold Start)
and smart local-map cache lookup.
"""

from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import pyproj
import rasterio
import requests

from app.config import MAPS_DIR, MOVINGDRONE_DIR

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Bounding box of Switzerland for swisstopo STAC coverage check
CH_LAT_MIN, CH_LAT_MAX = 45.78, 47.85
CH_LON_MIN, CH_LON_MAX = 5.90, 10.55


def is_in_switzerland(lat: float, lon: float) -> bool:
    return CH_LAT_MIN <= lat <= CH_LAT_MAX and CH_LON_MIN <= lon <= CH_LON_MAX


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(1e-12, 1.0 - a)))


def extract_sample_frames_b64(video_path: Path, count: int = 3, max_side: int = 1024) -> List[str]:
    """Extract evenly spaced keyframes from a video and return base64-encoded JPEGs."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video for VLM sampling: {video_path}")
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 1:
            indices = [0]
        elif count == 1:
            indices = [0]
        else:
            # Sample start, ~30%, ~65% to give VLM strong landmark context
            fracs = [0.02, 0.32, 0.65][:count]
            indices = sorted({min(total - 1, max(0, int(total * f))) for f in fracs})

        encoded: List[str] = []
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            scale = min(1.0, max_side / max(h, w))
            if scale < 1.0:
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            ok_enc, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if ok_enc:
                encoded.append(base64.b64encode(buf.tobytes()).decode("ascii"))
        return encoded
    finally:
        cap.release()


def _parse_json_response(text: str) -> Dict[str, Any]:
    text = text.strip()
    # Strip markdown code fences if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"(\{.*\})", text, flags=re.S)
        if brace:
            text = brace.group(1)
    return json.loads(text)


def detect_region_via_openrouter(
    video_path: Path,
    api_key: str,
    model: str = "google/gemini-2.5-flash",
) -> Dict[str, Any]:
    """Query OpenRouter Vision to infer approximate WGS84 (lat, lon) from video frames."""
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is not configured. Add it to .env or Settings, or use Pin on Map / Saved Local Map.")

    frames_b64 = extract_sample_frames_b64(video_path, count=3, max_side=1024)
    if not frames_b64:
        raise RuntimeError("Could not decode any frames from the video for VLM detection.")

    prompt = (
        "You are an expert aerial geo-localization analyst. Examine these frames from a single continuous UAV/drone flight. "
        "Determine if this location can be visually identified on Earth (e.g., a recognizable city, bridge, church, airport, landmark, river bend). "
        "If the imagery shows only generic featureless terrain (open ocean water, uniform dense forest, generic rural fields, or unidentifiable suburban roofs) "
        "where a reliable geographic location cannot be determined, set identifiable=false and lat/lon=null.\n\n"
        "Respond with ONLY a valid JSON object matching this exact schema:\n"
        "{\n"
        '  "identifiable": true or false,\n'
        '  "location_name": "Short human-readable place name (e.g. Zurich Old Town, Limmat River)",\n'
        '  "country": "Country name or Unknown",\n'
        '  "lat": 47.3700 or null,\n'
        '  "lon": 8.5425 or null,\n'
        '  "confidence": "high" | "medium" | "low",\n'
        '  "reasoning": "One concise sentence listing visual landmarks used (or why unidentifiable)."\n'
        "}"
    )

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for b64 in frames_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "Groundframe UAV Localization",
        },
        json={
            "model": model,
            "temperature": 0.1,
            "max_tokens": 400,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=60,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenRouter API error ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    raw_text = data["choices"][0]["message"]["content"]
    parsed = _parse_json_response(raw_text)

    identifiable = bool(parsed.get("identifiable", False))
    lat = parsed.get("lat")
    lon = parsed.get("lon")
    if lat is not None and lon is not None:
        lat = float(lat)
        lon = float(lon)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            identifiable = False
            lat, lon = None, None
    else:
        identifiable = False

    return {
        "identifiable": identifiable,
        "location_name": str(parsed.get("location_name") or "Unknown location"),
        "country": str(parsed.get("country") or "Unknown"),
        "lat": lat,
        "lon": lon,
        "confidence": str(parsed.get("confidence") or "low").lower(),
        "reasoning": str(parsed.get("reasoning") or ""),
        "model": model,
    }


def _map_dir_bounds_wgs84(map_dir: Path) -> Optional[Tuple[float, float, List[float]]]:
    """Return (center_lat, center_lon, [min_lon, min_lat, max_lon, max_lat]) for a local map directory."""
    meta_file = map_dir / "map_meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            c = meta.get("center_wgs84")
            b = meta.get("bbox_wgs84")
            if c and b and len(b) == 4:
                return float(c["lat"]), float(c["lon"]), [float(x) for x in b]
        except Exception:
            pass

    dop_dir = map_dir / "dop"
    tifs = sorted(dop_dir.glob("*.tif")) if dop_dir.exists() else []
    if not tifs:
        return None
    try:
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        crs = None
        for tif in tifs:
            with rasterio.open(tif) as src:
                crs = src.crs
                min_x = min(min_x, src.bounds.left)
                min_y = min(min_y, src.bounds.bottom)
                max_x = max(max_x, src.bounds.right)
                max_y = max(max_y, src.bounds.top)
        if crs is None:
            return None
        tr = pyproj.Transformer.from_crs(str(crs), "EPSG:4326", always_xy=True)
        min_lon, min_lat = tr.transform(min_x, min_y)
        max_lon, max_lat = tr.transform(max_x, max_y)
        c_lon, c_lat = tr.transform((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        return float(c_lat), float(c_lon), [float(min_lon), float(min_lat), float(max_lon), float(max_lat)]
    except Exception:
        return None


def find_matching_local_map(lat: float, lon: float, max_distance_m: float = 1200.0) -> Optional[Dict[str, Any]]:
    """Check if any cached map in data/maps/ or MovingDrone/scenes/ already covers (lat, lon)."""
    best: Optional[Tuple[float, Dict[str, Any]]] = None

    if MAPS_DIR.exists():
        for d in sorted(MAPS_DIR.iterdir()):
            if not d.is_dir():
                continue
            info = _map_dir_bounds_wgs84(d)
            if not info:
                continue
            c_lat, c_lon, bbox = info
            dist = haversine_m(lat, lon, c_lat, c_lon)
            inside = bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]
            if inside or dist <= max_distance_m:
                cand = {
                    "kind": "geotiff_dir",
                    "name": d.name,
                    "path": str(d),
                    "center_lat": c_lat,
                    "center_lon": c_lon,
                    "distance_m": round(dist, 1),
                }
                if best is None or dist < best[0]:
                    best = (dist, cand)

    if MOVINGDRONE_DIR.exists():
        for d in sorted(MOVINGDRONE_DIR.iterdir()):
            meta_file = d / "meta.json"
            if not meta_file.exists():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                utm_zone = meta.get("utm_zone")
                utm_offset = meta.get("utm_offset")
                if utm_zone is None or not utm_offset:
                    continue
                tr = pyproj.Transformer.from_crs(f"EPSG:{32600 + int(utm_zone)}", "EPSG:4326", always_xy=True)
                c_lon, c_lat = tr.transform(float(utm_offset[0]), float(utm_offset[1]))
                dist = haversine_m(lat, lon, c_lat, c_lon)
                if dist <= max_distance_m:
                    cand = {
                        "kind": "movingdrone_scene",
                        "name": d.name,
                        "path": str(d),
                        "center_lat": c_lat,
                        "center_lon": c_lon,
                        "distance_m": round(dist, 1),
                    }
                    if best is None or dist < best[0]:
                        best = (dist, cand)
            except Exception:
                continue

    return best[1] if best else None
