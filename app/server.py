"""Groundframe UAV Visual Localization — FastAPI Web Studio Server."""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import (
    DATA_DIR,
    MAPS_DIR,
    MOVINGDRONE_DIR,
    OUTPUTS_DIR,
    ROOT_DIR,
    SETTINGS,
    STATIC_DIR,
    UPLOADS_DIR,
    VIDEOS_DIR,
)
from app.exporters import trajectory_to_geojson, trajectory_to_kml
from app.jobs import JOB_MANAGER, ensure_browser_playable_mp4, probe_video_info


@asynccontextmanager
async def lifespan(_app: FastAPI):
    JOB_MANAGER.set_loop(asyncio.get_running_loop())
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Groundframe UAV Localization Studio", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers: Flight Discovery
# ---------------------------------------------------------------------------

def _infer_flight_meta(flight_dir: Path, traj: Dict[str, Any]) -> Dict[str, Any]:
    """Build metadata for legacy/CLI outputs (like smoke_airport10) that lack flight_meta.json."""
    meta_file = flight_dir / "flight_meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    fid = flight_dir.name
    video_path = None
    location = fid.replace("_", " ").title()

    # Auto-link known datasets from MANIFEST.json or MovingDrone
    if "airport10" in fid and (MOVINGDRONE_DIR / "airport10" / "video.mp4").exists():
        video_path = str((MOVINGDRONE_DIR / "airport10" / "video.mp4").relative_to(ROOT_DIR))
        location = "Berlin Schönefeld (MovingDrone)"
    elif "zurich" in fid and (VIDEOS_DIR / "zurich" / "zurich_limmat_25s.mp4").exists():
        video_path = str((VIDEOS_DIR / "zurich" / "zurich_limmat_25s.mp4").relative_to(ROOT_DIR))
        location = "Zurich Old Town / Limmat"
    elif "lucerne" in fid and (VIDEOS_DIR / "lucerne" / "lucerne_kapellbruecke_22s.mp4").exists():
        video_path = str((VIDEOS_DIR / "lucerne" / "lucerne_kapellbruecke_22s.mp4").relative_to(ROOT_DIR))
        location = "Lucerne / Kapellbrücke"

    cov = traj.get("summary", {}).get("coverage_pct", 0.0)
    return {
        "flight_id": fid,
        "name": fid.replace("_", " ").title(),
        "location": location,
        "created_at": "",
        "status": "COMPLETED" if cov > 0 else "REJECTED",
        "video_path": video_path,
        "map_dir": None,
    }


def _resolve_video_path(meta: Dict[str, Any]) -> Optional[Path]:
    vp = meta.get("video_path")
    if not vp:
        return None
    p = Path(vp)
    if not p.is_absolute():
        p = (ROOT_DIR / p).resolve()
    return p if p.exists() else None


def _list_keyframes(flight_dir: Path) -> List[Dict[str, Any]]:
    kf_dir = flight_dir / "keyframes"
    if not kf_dir.exists():
        return []
    items: List[Dict[str, Any]] = []
    for png in sorted(kf_dir.glob("*.png")):
        # CategorizeOrthoTrack vis images: frame_000000_keyframe.png, _coarse.png, _fine.png, keyframe_0000_crops.png
        name = png.name
        frame_id = None
        parts = name.replace(".png", "").split("_")
        for tok in parts:
            if tok.isdigit():
                frame_id = int(tok)
                break
        kind = "keyframe"
        if "coarse" in name:
            kind = "coarse"
        elif "fine" in name:
            kind = "fine"
        elif "crops" in name:
            kind = "crops"
        items.append({
            "filename": name,
            "frame_id": frame_id,
            "kind": kind,
            "url": f"/api/flights/{flight_dir.name}/keyframes/{name}",
        })
    return items


# ---------------------------------------------------------------------------
# Config & Settings
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    cesium_ion_token: Optional[str] = None
    google_maps_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    openrouter_model: Optional[str] = None


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    return SETTINGS.public_client_config()


@app.get("/api/settings")
def get_settings() -> Dict[str, Any]:
    return SETTINGS.masked_settings()


@app.post("/api/settings")
def update_settings(body: SettingsUpdate) -> Dict[str, Any]:
    SETTINGS.update(body.model_dump())
    return {
        "settings": SETTINGS.masked_settings(),
        "config": SETTINGS.public_client_config(),
    }


# ---------------------------------------------------------------------------
# Flights Library
# ---------------------------------------------------------------------------

@app.get("/api/flights")
def list_flights() -> Dict[str, Any]:
    flights: List[Dict[str, Any]] = []
    if OUTPUTS_DIR.exists():
        for d in sorted(OUTPUTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            traj_file = d / "trajectory.json"
            if not traj_file.exists():
                continue
            try:
                traj = json.loads(traj_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            meta = _infer_flight_meta(d, traj)
            summary = traj.get("summary", {})
            metrics = summary.get("metrics") or {}
            flights.append({
                "flight_id": d.name,
                "name": meta.get("name", d.name),
                "location": meta.get("location", d.name),
                "status": meta.get("status", "COMPLETED"),
                "created_at": meta.get("created_at", ""),
                "has_video": _resolve_video_path(meta) is not None,
                "total_frames": summary.get("total_frames", 0),
                "localized_frames": summary.get("localized_frames", 0),
                "coverage_pct": summary.get("coverage_pct", 0.0),
                "median_horiz_m": metrics.get("median_horiz_m"),
                "has_gt": bool(metrics),
                "rejection_reason": meta.get("rejection_reason") or summary.get("rejection_reason"),
            })
    return {"flights": flights}


@app.get("/api/flights/{flight_id}")
def get_flight(flight_id: str) -> Dict[str, Any]:
    flight_dir = (OUTPUTS_DIR / flight_id).resolve()
    if not flight_dir.is_relative_to(OUTPUTS_DIR.resolve()) or not flight_dir.exists():
        raise HTTPException(status_code=404, detail="Flight not found")
    traj_file = flight_dir / "trajectory.json"
    if not traj_file.exists():
        raise HTTPException(status_code=404, detail="trajectory.json not found for flight")

    traj = json.loads(traj_file.read_text(encoding="utf-8"))
    meta = _infer_flight_meta(flight_dir, traj)
    has_video = _resolve_video_path(meta) is not None
    return {
        "flight_id": flight_id,
        "meta": meta,
        "trajectory": traj,
        "video_url": f"/api/flights/{flight_id}/video" if has_video else None,
        "keyframes": _list_keyframes(flight_dir),
        "has_csv": (flight_dir / "results.csv").exists(),
        "has_plot": (flight_dir / "tracking_results.png").exists(),
    }


@app.delete("/api/flights/{flight_id}")
def delete_flight(flight_id: str) -> Dict[str, Any]:
    flight_dir = (OUTPUTS_DIR / flight_id).resolve()
    if not flight_dir.is_relative_to(OUTPUTS_DIR.resolve()) or not flight_dir.exists():
        raise HTTPException(status_code=404, detail="Flight not found")
    shutil.rmtree(flight_dir)
    return {"deleted": flight_id}


@app.get("/api/flights/{flight_id}/video")
def stream_flight_video(flight_id: str):
    flight_dir = (OUTPUTS_DIR / flight_id).resolve()
    if not flight_dir.is_relative_to(OUTPUTS_DIR.resolve()) or not flight_dir.exists():
        raise HTTPException(status_code=404, detail="Flight not found")
    traj = json.loads((flight_dir / "trajectory.json").read_text(encoding="utf-8"))
    meta = _infer_flight_meta(flight_dir, traj)
    vp = _resolve_video_path(meta)
    if vp is None:
        raise HTTPException(status_code=404, detail="Source video not found")
    return FileResponse(vp, media_type="video/mp4", filename=vp.name)


@app.get("/api/flights/{flight_id}/keyframes/{filename}")
def get_keyframe_image(flight_id: str, filename: str):
    kf_path = (OUTPUTS_DIR / flight_id / "keyframes" / filename).resolve()
    expected_root = (OUTPUTS_DIR / flight_id / "keyframes").resolve()
    if not kf_path.is_relative_to(expected_root) or not kf_path.exists():
        raise HTTPException(status_code=404, detail="Keyframe image not found")
    return FileResponse(kf_path, media_type="image/png")


@app.get("/api/flights/{flight_id}/export/{fmt}")
def export_flight(flight_id: str, fmt: str, smoothed: bool = True):
    flight_dir = (OUTPUTS_DIR / flight_id).resolve()
    if not flight_dir.is_relative_to(OUTPUTS_DIR.resolve()) or not flight_dir.exists():
        raise HTTPException(status_code=404, detail="Flight not found")

    fmt = fmt.lower()
    traj_file = flight_dir / "trajectory.json"
    if not traj_file.exists():
        raise HTTPException(status_code=404, detail="trajectory.json not found")

    if fmt == "json":
        return FileResponse(traj_file, media_type="application/json", filename=f"{flight_id}_trajectory.json")
    if fmt == "csv":
        csv_file = flight_dir / "results.csv"
        if not csv_file.exists():
            raise HTTPException(status_code=404, detail="results.csv not available for this flight")
        return FileResponse(csv_file, media_type="text/csv", filename=f"{flight_id}_results.csv")

    traj = json.loads(traj_file.read_text(encoding="utf-8"))
    meta = _infer_flight_meta(flight_dir, traj)
    name = meta.get("name", flight_id)
    suffix = "" if smoothed else "_raw"

    if fmt == "geojson":
        content = trajectory_to_geojson(traj, name=name, smoothed=smoothed)
        return Response(
            content=content,
            media_type="application/geo+json",
            headers={"Content-Disposition": f'attachment; filename="{flight_id}{suffix}.geojson"'},
        )
    if fmt == "kml":
        content = trajectory_to_kml(traj, name=name, smoothed=smoothed)
        return Response(
            content=content,
            media_type="application/vnd.google-earth.kml+xml",
            headers={"Content-Disposition": f'attachment; filename="{flight_id}{suffix}.kml"'},
        )
    raise HTTPException(status_code=400, detail=f"Unsupported export format: {fmt}")


# ---------------------------------------------------------------------------
# Local Resources (Videos & Cached Maps) + File Uploads
# ---------------------------------------------------------------------------

@app.get("/api/resources")
def list_resources() -> Dict[str, Any]:
    videos: List[Dict[str, Any]] = []
    seen_videos = set()

    # Load MANIFEST.json first for nice labels
    manifest_file = DATA_DIR / "MANIFEST.json"
    if manifest_file.exists():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            for key, item in manifest.get("datasets", {}).items():
                vp = item.get("video")
                if not vp:
                    continue
                full = (ROOT_DIR / vp).resolve()
                if full.exists():
                    seen_videos.add(str(full))
                    videos.append({
                        "id": key,
                        "label": f"{key} — {item.get('role', '')[:64]}",
                        "path": vp,
                        "resolution": item.get("resolution"),
                        "duration_sec": item.get("duration_sec"),
                        "frames": item.get("frames"),
                        "suggested_map": item.get("map_dir") or (item.get("path") if key.startswith("movingdrone") else None),
                    })
        except Exception:
            pass

    for scan_dir in (VIDEOS_DIR, UPLOADS_DIR):
        if not scan_dir.exists():
            continue
        for ext in ("*.mp4", "*.mov", "*.mkv"):
            for f in sorted(scan_dir.rglob(ext)):
                if "raw" in f.parts or str(f.resolve()) in seen_videos:
                    continue
                seen_videos.add(str(f.resolve()))
                rel = str(f.relative_to(ROOT_DIR))
                videos.append({
                    "id": f.stem,
                    "label": rel,
                    "path": rel,
                })

    maps: List[Dict[str, Any]] = []
    if MAPS_DIR.exists():
        for d in sorted(MAPS_DIR.iterdir()):
            if not d.is_dir():
                continue
            dop_count = len(list((d / "dop").glob("*.tif"))) if (d / "dop").exists() else 0
            dsm_count = len(list((d / "dsm").glob("*.tif"))) if (d / "dsm").exists() else 0
            if dop_count == 0 or dsm_count == 0:
                continue
            meta_file = d / "map_meta.json"
            center = None
            if meta_file.exists():
                try:
                    center = json.loads(meta_file.read_text(encoding="utf-8")).get("center_wgs84")
                except Exception:
                    pass
            maps.append({
                "kind": "geotiff_dir",
                "name": d.name,
                "label": f"{d.name} (swisstopo · {dop_count} DOP / {dsm_count} DSM tiles)",
                "path": str(d.relative_to(ROOT_DIR)),
                "center_wgs84": center,
            })

    if MOVINGDRONE_DIR.exists():
        for d in sorted(MOVINGDRONE_DIR.iterdir()):
            if (d / "meta.json").exists() and (d / "dsm.npz").exists():
                maps.append({
                    "kind": "movingdrone_scene",
                    "name": d.name,
                    "label": f"{d.name} (MovingDrone scene · with GT)",
                    "path": str(d.relative_to(ROOT_DIR)),
                })

    return {"videos": videos, "maps": maps}


@app.post("/api/upload/video")
async def upload_video(file: UploadFile = File(...)) -> Dict[str, Any]:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    orig_name = Path(file.filename or "upload.mp4").name
    ext = Path(orig_name).suffix.lower() or ".mp4"
    if ext not in (".mp4", ".mov", ".mkv", ".avi", ".webm"):
        raise HTTPException(status_code=400, detail=f"Unsupported video extension: {ext}")

    dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{orig_name}"
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    playable_path, info, transcoded = await asyncio.to_thread(ensure_browser_playable_mp4, dest)
    return {
        "path": str(playable_path.relative_to(ROOT_DIR)),
        "filename": playable_path.name,
        "transcoded": transcoded,
        "info": info,
    }


@app.post("/api/upload/file")
async def upload_aux_file(kind: str = "gt", file: UploadFile = File(...)) -> Dict[str, Any]:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    orig_name = Path(file.filename or f"{kind}.dat").name
    dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{orig_name}"
    with open(dest, "wb") as out:
        out.write(await file.read())
    return {
        "path": str(dest.relative_to(ROOT_DIR)),
        "filename": orig_name,
    }


# ---------------------------------------------------------------------------
# Jobs & SSE Stream
# ---------------------------------------------------------------------------

class JobCreateRequest(BaseModel):
    name: Optional[str] = None
    video_path: str
    location_mode: str = "vpr"  # vpr | auto | pin | local_map
    vpr_model: Optional[str] = None  # sample4geo | megaloc | None (active winner)
    pin_lat: Optional[float] = None
    pin_lon: Optional[float] = None
    radius_m: float = 600.0
    local_map_path: Optional[str] = None
    calibration_mode: str = "auto"  # auto | file | manual_fov
    intrinsics_path: Optional[str] = None
    manual_fov: Optional[float] = None
    fine_matcher: str = "base"
    skip_frames: int = 1
    start_frame: Optional[int] = None
    end_frame: Optional[int] = None
    accumulate_points: bool = True
    save_keyframe_vis: bool = False
    gt_path: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    openrouter_model: Optional[str] = None


@app.post("/api/jobs")
def create_job(req: JobCreateRequest) -> Dict[str, Any]:
    vp = Path(req.video_path)
    if not vp.is_absolute():
        vp = (ROOT_DIR / vp).resolve()
    if not vp.exists():
        raise HTTPException(status_code=400, detail=f"Video file not found: {req.video_path}")

    if req.location_mode == "pin" and (req.pin_lat is None or req.pin_lon is None):
        raise HTTPException(status_code=400, detail="pin_lat and pin_lon are required for Pin on Map mode")
    if req.location_mode == "local_map" and not req.local_map_path:
        raise HTTPException(status_code=400, detail="local_map_path is required for Saved Local Map mode")

    params = req.model_dump()
    params["video_path"] = str(vp)
    for k in ("intrinsics_path", "gt_path"):
        if params.get(k):
            fp = Path(params[k])
            if not fp.is_absolute():
                fp = (ROOT_DIR / fp).resolve()
            params[k] = str(fp)

    job = JOB_MANAGER.start_job(params)
    return job.snapshot()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    job = JOB_MANAGER.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str):
    return StreamingResponse(
        JOB_MANAGER.subscribe(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    ok = JOB_MANAGER.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"cancelled": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# Static Frontend
# ---------------------------------------------------------------------------

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return PlainTextResponse("Groundframe UI not built yet.", status_code=503)
    return FileResponse(idx, media_type="text/html")
