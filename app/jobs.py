"""Background GPU job manager for Groundframe with SSE progress streaming,
isolated OrthoTrack subprocess execution, clean process-group cancellation,
ffprobe/ffmpeg browser-compatibility transcoding, and flight_meta.json persistence.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import cv2

from app.config import (
    MAPS_DIR,
    MOVINGDRONE_DIR,
    ORTHOTRACK_DIR,
    OUTPUTS_DIR,
    ROOT_DIR,
    SCRIPTS_DIR,
    SETTINGS,
    TORCH_HOME_DIR,
    UPLOADS_DIR,
)
from app.gt_parser import apply_dji_srt_to_trajectory
from app.vlm import (
    detect_region_via_openrouter,
    find_matching_local_map,
    is_in_switzerland,
)
from scripts.prepare_map import prepare_single_file_map

STEP_IDS = [
    ("vlm_detect", "VLM Region Detect"),
    ("fetch_map", "Fetch 3D Map"),
    ("cold_start", "Cold-Start Keyframe"),
    ("frame_tracking", "Frame Tracking"),
    ("export_wgs84", "Export WGS84"),
]

_TQDM_TRACK_RE = re.compile(
    r"Tracking\s*(?:\[([^\]]+)\])?:\s*(\d+)%\|[^|]*\|\s*(\d+)/(\d+)(?:.*?fps=([0-9.]+))?"
)
_TOTAL_FRAMES_RE = re.compile(r"Tracking\s+(\d+)\s+frames")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return s[:48] or "flight"


def probe_video_info(video_path: Path) -> Dict[str, Any]:
    """Probe video codec, fps, resolution, and frame count using ffprobe + OpenCV fallback."""
    info: Dict[str, Any] = {
        "codec": "unknown",
        "pix_fmt": "unknown",
        "width": 0,
        "height": 0,
        "fps": 24.0,
        "frames": 0,
        "duration_s": 0.0,
    }
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_frames,duration",
                "-of",
                "json",
                str(video_path),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        streams = json.loads(out).get("streams", [])
        if streams:
            st = streams[0]
            info["codec"] = str(st.get("codec_name") or "unknown")
            info["pix_fmt"] = str(st.get("pix_fmt") or "unknown")
            info["width"] = int(st.get("width") or 0)
            info["height"] = int(st.get("height") or 0)
            rfr = str(st.get("r_frame_rate") or "24/1")
            if "/" in rfr:
                num, den = rfr.split("/", 1)
                if float(den) > 0:
                    info["fps"] = round(float(num) / float(den), 4)
            if st.get("nb_frames") and str(st["nb_frames"]).isdigit():
                info["frames"] = int(st["nb_frames"])
            if st.get("duration"):
                info["duration_s"] = round(float(st["duration"]), 2)
    except Exception:
        pass

    if info["frames"] <= 0 or info["width"] <= 0:
        cap = cv2.VideoCapture(str(video_path))
        try:
            if cap.isOpened():
                info["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or info["width"])
                info["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or info["height"])
                fps = cap.get(cv2.CAP_PROP_FPS)
                if fps and fps > 0.1:
                    info["fps"] = round(float(fps), 4)
                fc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                if fc > 0:
                    info["frames"] = fc
                    info["duration_s"] = round(fc / info["fps"], 2)
        finally:
            cap.release()
    return info


def ensure_browser_playable_mp4(video_path: Path) -> Tuple[Path, Dict[str, Any], bool]:
    """Keep H.264 yuv420p MP4 untouched; transcode .mov/HEVC/unsupported codecs to H.264 MP4."""
    info = probe_video_info(video_path)
    is_mp4 = video_path.suffix.lower() == ".mp4"
    is_h264 = info["codec"].lower() == "h264"
    is_yuv420 = info["pix_fmt"].lower() in ("yuv420p", "yuvj420p")

    if is_mp4 and is_h264 and is_yuv420:
        return video_path, info, False

    out_path = video_path.with_name(f"{video_path.stem}_h264.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
    return out_path, probe_video_info(out_path), True


@dataclass
class JobState:
    job_id: str
    flight_id: str
    name: str
    params: Dict[str, Any]
    status: str = "queued"  # queued | running | completed | rejected | failed | cancelled
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    steps: List[Dict[str, Any]] = field(
        default_factory=lambda: [
            {"id": sid, "label": label, "status": "pending", "detail": ""}
            for sid, label in STEP_IDS
        ]
    )
    frame_current: int = 0
    frame_total: int = 0
    fps: float = 0.0
    tracking_badge: str = ""
    vlm_result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)
    cancel_requested: bool = False
    active_proc: Optional[subprocess.Popen] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "flight_id": self.flight_id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "steps": self.steps,
            "frame_current": self.frame_current,
            "frame_total": self.frame_total,
            "fps": self.fps,
            "tracking_badge": self.tracking_badge,
            "vlm_result": self.vlm_result,
            "error": self.error,
            "logs_tail": self.logs[-120:],
        }


class JobManager:
    """Single-GPU-slot job manager with asyncio SSE broadcasting."""

    def __init__(self) -> None:
        self.jobs: Dict[str, JobState] = {}
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._gpu_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _emit(self, job_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        subs = self._subscribers.get(job_id, [])
        if not subs or self._loop is None:
            return
        msg = {"event": event_type, "data": payload}
        for q in list(subs):
            self._loop.call_soon_threadsafe(q.put_nowait, msg)

    def _log(self, job: JobState, line: str) -> None:
        line = line.rstrip("\r\n")
        if not line:
            return
        job.logs.append(line)
        if len(job.logs) > 800:
            job.logs = job.logs[-600:]
        self._emit(job.job_id, "log", {"line": line})

    def _set_step(self, job: JobState, step_id: str, status: str, detail: str = "") -> None:
        for s in job.steps:
            if s["id"] == step_id:
                s["status"] = status
                if detail:
                    s["detail"] = detail
                break
        self._emit(job.job_id, "state", job.snapshot())

    async def subscribe(self, job_id: str) -> AsyncIterator[str]:
        job = self.jobs.get(job_id)
        if job is None:
            yield f"event: error\ndata: {json.dumps({'error': 'Job not found'})}\n\n"
            return

        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(job_id, []).append(q)
        try:
            # Send immediate initial state
            yield f"event: state\ndata: {json.dumps(job.snapshot())}\n\n"
            if job.status in ("completed", "rejected", "failed", "cancelled"):
                yield f"event: done\ndata: {json.dumps(job.snapshot())}\n\n"
                return

            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                    if msg["event"] == "done":
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            subs = self._subscribers.get(job_id, [])
            if q in subs:
                subs.remove(q)

    def start_job(self, params: Dict[str, Any]) -> JobState:
        job_id = uuid.uuid4().hex[:10]
        base_name = params.get("name") or Path(str(params["video_path"])).stem
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        flight_id = f"{slugify(base_name)}_{stamp}"

        job = JobState(
            job_id=job_id,
            flight_id=flight_id,
            name=str(base_name),
            params=params,
        )
        self.jobs[job_id] = job
        t = threading.Thread(target=self._run_worker, args=(job,), daemon=True)
        t.start()
        return job

    def cancel_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancel_requested = True
        proc = job.active_proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(0.5)
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        job.status = "cancelled"
        self._log(job, "[cancelled] Process group terminated by user.")
        self._emit(job.job_id, "done", job.snapshot())
        return True

    def _run_subprocess(
        self,
        job: JobState,
        cmd: List[str],
        env: Dict[str, str],
        on_line=None,
    ) -> int:
        if job.cancel_requested:
            return -15

        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        job.active_proc = proc
        try:
            assert proc.stdout is not None
            buf = ""
            while True:
                ch = proc.stdout.read(1)
                if ch == "" and proc.poll() is not None:
                    if buf:
                        if on_line:
                            on_line(buf)
                        self._log(job, buf)
                    break
                if ch in ("\n", "\r"):
                    if buf.strip():
                        if on_line:
                            on_line(buf)
                        self._log(job, buf)
                    buf = ""
                else:
                    buf += ch
            return proc.wait()
        finally:
            job.active_proc = None

    def _write_rejected_flight(
        self,
        job: JobState,
        out_dir: Path,
        video_path: Path,
        video_info: Dict[str, Any],
        reason: str,
        location_label: str,
        map_dir: Optional[Path] = None,
        center_wgs84: Optional[Dict[str, float]] = None,
    ) -> None:
        """Persist a 0%-localized / rejected flight so stress tests are inspectable in the Library."""
        out_dir.mkdir(parents=True, exist_ok=True)
        fps = float(video_info.get("fps") or 24.0)
        n_frames = int(video_info.get("frames") or 0)
        start_f = int(job.params.get("start_frame") or 0)
        end_f = int(job.params["end_frame"]) if job.params.get("end_frame") is not None else n_frames
        skip_f = max(1, int(job.params.get("skip_frames") or 1))
        frame_ids = list(range(start_f, max(start_f + 1, end_f), skip_f)) if n_frames > 0 else [0]

        frames_out = [
            {
                "frame_id": fid,
                "timestamp_s": round(fid / fps, 4),
                "status": "LOST",
                "method": "none",
                "is_keyframe": False,
                "success": False,
                "num_inliers": 0,
                "num_tracked_points": 0,
                "reproj_error_px": None,
                "processing_time_s": None,
                "kf_reason": None,
                "est": None,
                "gt": None,
                "error": None,
            }
            for fid in frame_ids
        ]

        traj = {
            "schema_version": "1.0",
            "source_crs": "EPSG:2056",
            "vertical_datum": "LN02",
            "geoid_offset_m": 48.0,
            "fps": fps,
            "intrinsics": None,
            "device": None,
            "summary": {
                "total_frames": len(frames_out),
                "localized_frames": 0,
                "coverage_pct": 0.0,
                "keyframes": 0,
                "tracked": 0,
                "predicted": 0,
                "rejected": True,
                "rejection_reason": reason,
            },
            "frames": frames_out,
        }
        (out_dir / "trajectory.json").write_text(json.dumps(traj, indent=2), encoding="utf-8")

        meta = {
            "flight_id": job.flight_id,
            "name": job.name,
            "location": location_label,
            "created_at": job.created_at,
            "status": "REJECTED",
            "rejection_reason": reason,
            "video_path": str(video_path.relative_to(ROOT_DIR)) if video_path.is_relative_to(ROOT_DIR) else str(video_path),
            "map_dir": str(map_dir.relative_to(ROOT_DIR)) if (map_dir and map_dir.is_relative_to(ROOT_DIR)) else (str(map_dir) if map_dir else None),
            "center_wgs84": center_wgs84,
            "vlm_result": job.vlm_result,
            "params": {k: v for k, v in job.params.items() if "key" not in k.lower() and "token" not in k.lower()},
        }
        (out_dir / "flight_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _run_worker(self, job: JobState) -> None:
        with self._gpu_lock:
            if job.cancel_requested:
                return
            job.status = "running"
            self._emit(job.job_id, "state", job.snapshot())

            p = job.params
            video_path = Path(p["video_path"]).resolve()
            out_dir = OUTPUTS_DIR / job.flight_id
            out_dir.mkdir(parents=True, exist_ok=True)
            video_info = probe_video_info(video_path)

            base_env = os.environ.copy()
            base_env["PYTHONPATH"] = str(ORTHOTRACK_DIR)
            base_env["TORCH_HOME"] = str(TORCH_HOME_DIR)
            base_env["PYTHONUNBUFFERED"] = "1"
            base_env["MPLCONFIGDIR"] = "/tmp/groundframe_mpl"

            location_mode = p.get("location_mode", "auto")  # auto | pin | local_map
            radius_m = float(p.get("radius_m") or 600.0)
            map_dir: Optional[Path] = None
            sequence_dir: Optional[Path] = None
            location_label = job.name
            center_wgs84: Optional[Dict[str, float]] = None

            try:
                # -----------------------------------------------------------
                # STEP 1: VLM Region Detect
                # -----------------------------------------------------------
                if location_mode == "auto":
                    self._set_step(job, "vlm_detect", "running", "Sampling video frames for OpenRouter Vision...")
                    api_key = p.get("openrouter_api_key") or SETTINGS.openrouter_api_key
                    model = p.get("openrouter_model") or SETTINGS.openrouter_model
                    self._log(job, f"[VLM] Querying OpenRouter ({model}) on {video_path.name}...")
                    vlm = detect_region_via_openrouter(video_path, api_key=api_key, model=model)
                    job.vlm_result = vlm
                    self._log(job, f"[VLM] Result: {json.dumps(vlm, ensure_ascii=False)}")

                    if not vlm["identifiable"] or vlm["lat"] is None or vlm["lon"] is None:
                        reason = f"VLM rejected clip as unidentifiable: {vlm.get('reasoning') or vlm.get('location_name')}"
                        self._set_step(job, "vlm_detect", "rejected", reason)
                        for sid in ("fetch_map", "cold_start", "frame_tracking", "export_wgs84"):
                            self._set_step(job, sid, "skipped")
                        self._write_rejected_flight(job, out_dir, video_path, video_info, reason, vlm["location_name"])
                        job.status = "rejected"
                        job.error = reason
                        self._emit(job.job_id, "done", job.snapshot())
                        return

                    lat, lon = float(vlm["lat"]), float(vlm["lon"])
                    location_label = vlm["location_name"]
                    center_wgs84 = {"lat": lat, "lon": lon}
                    self._set_step(
                        job,
                        "vlm_detect",
                        "done",
                        f"{location_label} ({lat:.4f}, {lon:.4f}) · {vlm['confidence']}",
                    )
                elif location_mode == "pin":
                    lat = float(p["pin_lat"])
                    lon = float(p["pin_lon"])
                    location_label = f"Pin ({lat:.4f}, {lon:.4f})"
                    center_wgs84 = {"lat": lat, "lon": lon}
                    self._set_step(job, "vlm_detect", "skipped", f"Manual pin: {lat:.4f}, {lon:.4f}")
                else:
                    self._set_step(job, "vlm_detect", "skipped", f"Saved map: {p.get('local_map_path')}")

                if job.cancel_requested:
                    return

                # -----------------------------------------------------------
                # STEP 2: Fetch 3D Map (or match cached local map)
                # -----------------------------------------------------------
                if location_mode == "local_map":
                    chosen = Path(p["local_map_path"])
                    if not chosen.is_absolute():
                        chosen = (ROOT_DIR / chosen).resolve()
                    if (chosen / "meta.json").exists() and (chosen / "dsm.npz").exists():
                        sequence_dir = chosen
                        location_label = chosen.name
                        self._set_step(job, "fetch_map", "done", f"Using MovingDrone scene: {chosen.name}")
                    else:
                        map_dir = chosen
                        location_label = chosen.name
                        self._set_step(job, "fetch_map", "done", f"Using cached map: {chosen.name}")
                else:
                    assert center_wgs84 is not None
                    lat, lon = center_wgs84["lat"], center_wgs84["lon"]
                    self._set_step(job, "fetch_map", "running", "Checking cached local maps...")
                    matched = find_matching_local_map(lat, lon, max_distance_m=max(radius_m, 900.0))
                    if matched and matched["kind"] == "geotiff_dir":
                        map_dir = Path(matched["path"])
                        self._log(job, f"[Map] Cache hit: {map_dir.name} ({matched['distance_m']} m from query)")
                        self._set_step(job, "fetch_map", "done", f"Cached map hit: {map_dir.name}")
                    elif matched and matched["kind"] == "movingdrone_scene":
                        sequence_dir = Path(matched["path"])
                        self._log(job, f"[Map] Matched MovingDrone scene: {sequence_dir.name}")
                        self._set_step(job, "fetch_map", "done", f"Cached scene: {sequence_dir.name}")
                    elif is_in_switzerland(lat, lon):
                        slug = f"auto_{slugify(location_label)}_{lat:.3f}_{lon:.3f}".replace(".", "p")
                        map_dir = MAPS_DIR / slug
                        self._set_step(job, "fetch_map", "running", f"Fetching swisstopo ±{int(radius_m)}m...")
                        fetch_cmd = [
                            sys.executable,
                            str(SCRIPTS_DIR / "fetch_swisstopo.py"),
                            "--lat",
                            str(lat),
                            "--lon",
                            str(lon),
                            "--radius-m",
                            str(radius_m),
                            "--out",
                            str(map_dir),
                        ]
                        rc = self._run_subprocess(job, fetch_cmd, base_env)
                        if job.cancel_requested:
                            return
                        if rc != 0:
                            raise RuntimeError(f"swisstopo map download failed (exit {rc})")
                        self._set_step(job, "fetch_map", "done", f"swisstopo cached -> {map_dir.name}")
                    else:
                        reason = (
                            f"Detected location '{location_label}' ({lat:.4f}, {lon:.4f}) is outside Switzerland "
                            "and no cached local 3D map covers it. Choose Pin on Map (CH) or a Saved Local Map."
                        )
                        self._set_step(job, "fetch_map", "rejected", reason)
                        for sid in ("cold_start", "frame_tracking", "export_wgs84"):
                            self._set_step(job, sid, "skipped")
                        self._write_rejected_flight(job, out_dir, video_path, video_info, reason, location_label, None, center_wgs84)
                        job.status = "rejected"
                        job.error = reason
                        self._emit(job.job_id, "done", job.snapshot())
                        return

                # Build / reuse fast single-file 0.2m/px DOP + DSM cache (<= 6000px -> OrthoTrack RAM preload)
                cached_dop_path: Optional[str] = None
                cached_dsm_path: Optional[str] = None
                if map_dir is not None:
                    self._set_step(job, "fetch_map", "running", f"Preparing cached 0.2m/px DOP + DSM ({map_dir.name})...")
                    prep = prepare_single_file_map(
                        map_dir,
                        lat=center_wgs84["lat"] if center_wgs84 else None,
                        lon=center_wgs84["lon"] if center_wgs84 else None,
                        radius_m=radius_m,
                        target_dop_gsd=0.2,
                        log_fn=lambda m: self._log(job, m),
                    )
                    cached_dop_path = prep["dop_path"]
                    cached_dsm_path = prep["dsm_path"]
                    hit_str = "cache hit" if prep["cached"] else f"built in {prep['elapsed_s']}s"
                    self._set_step(
                        job,
                        "fetch_map",
                        "done",
                        f"{map_dir.name} · {prep['dop_width']}×{prep['dop_height']} @ {prep['dop_gsd']}m/px ({hit_str})",
                    )

                if job.cancel_requested:
                    return

                # -----------------------------------------------------------
                # STEP 3 & 4: Cold-Start Keyframe + Frame Tracking (OrthoTrack)
                # -----------------------------------------------------------
                self._set_step(job, "cold_start", "running", "Initializing RoMaV2 & searching first keyframe...")
                cmd = [
                    sys.executable,
                    str(ORTHOTRACK_DIR / "scripts" / "run_tracking.py"),
                    "--output",
                    str(out_dir),
                    "--fine_matcher",
                    str(p.get("fine_matcher") or "precise"),
                    "--skip_frames",
                    str(int(p.get("skip_frames") or 1)),
                ]
                if p.get("save_keyframe_vis", False):
                    cmd.append("--save_keyframe_vis")

                if sequence_dir is not None:
                    cmd += ["--sequence_dir", str(sequence_dir)]
                    if video_path != (sequence_dir / "video.mp4").resolve():
                        cmd += ["--footage", str(video_path)]
                else:
                    assert cached_dop_path and cached_dsm_path
                    cmd += [
                        "--footage",
                        str(video_path),
                        "--dop",
                        cached_dop_path,
                        "--dsm",
                        cached_dsm_path,
                    ]

                if p.get("start_frame") is not None and str(p.get("start_frame")) != "":
                    cmd += ["--start_frame", str(int(p["start_frame"]))]
                if p.get("end_frame") is not None and str(p.get("end_frame")) != "":
                    cmd += ["--end_frame", str(int(p["end_frame"]))]

                calib_mode = p.get("calibration_mode", "auto")
                if calib_mode == "auto":
                    if sequence_dir is None:
                        cmd += ["--force_calibration"]
                elif calib_mode == "file" and p.get("intrinsics_path"):
                    cmd += ["--intrinsics", str(p["intrinsics_path"])]
                elif calib_mode == "manual_fov" and p.get("manual_fov"):
                    w = int(video_info.get("width") or 1920)
                    h = int(video_info.get("height") or 1080)
                    fov_v = float(p["manual_fov"])
                    import math as _m
                    fy = (h / 2.0) / _m.tan(_m.radians(fov_v / 2.0))
                    intr_path = out_dir / "manual_intrinsics.json"
                    intr_path.write_text(
                        json.dumps({
                            "fov_vertical": fov_v,
                            "fx": round(fy, 4),
                            "fy": round(fy, 4),
                            "cx": round(w / 2.0, 2),
                            "cy": round(h / 2.0, 2),
                            "width": w,
                            "height": h,
                        }, indent=2),
                        encoding="utf-8",
                    )
                    cmd += ["--intrinsics", str(intr_path)]

                gt_path_str = p.get("gt_path")
                gt_srt_path: Optional[Path] = None
                if gt_path_str:
                    gt_p = Path(gt_path_str)
                    if gt_p.suffix.lower() == ".srt":
                        gt_srt_path = gt_p
                    elif gt_p.suffix.lower() == ".csv":
                        cmd += ["--gt_poses", str(gt_p)]

                self._log(job, f"[OrthoTrack] Running: {' '.join(cmd)}")

                cold_done = False

                def on_track_line(line: str) -> None:
                    nonlocal cold_done
                    mtot = _TOTAL_FRAMES_RE.search(line)
                    if mtot:
                        job.frame_total = int(mtot.group(1))
                    m = _TQDM_TRACK_RE.search(line)
                    if m:
                        badge = (m.group(1) or "").strip()
                        cur = int(m.group(3))
                        tot = int(m.group(4))
                        fps_val = float(m.group(5)) if m.group(5) else job.fps
                        job.frame_current = cur
                        job.frame_total = tot
                        job.fps = fps_val
                        job.tracking_badge = badge
                        if not cold_done and cur >= 1:
                            cold_done = True
                            self._set_step(job, "cold_start", "done", f"Initial pose locked ({badge or 'KF'})")
                            self._set_step(job, "frame_tracking", "running", f"Frame {cur}/{tot} · {fps_val:.1f} FPS")
                        elif cold_done:
                            self._set_step(job, "frame_tracking", "running", f"Frame {cur}/{tot} · {fps_val:.1f} FPS")

                rc = self._run_subprocess(job, cmd, base_env, on_line=on_track_line)
                if job.cancel_requested:
                    return

                results_json = out_dir / "results.json"
                if rc != 0 or not results_json.exists():
                    # Check if this was a clean localization rejection (e.g., FirstFrameLocalizationError)
                    joined_logs = "\n".join(job.logs[-40:])
                    is_loc_reject = any(
                        tok in joined_logs
                        for tok in ("FirstFrameLocalizationError", "InsufficientConfidentMatchesError", "Failed to localize", "0 localized")
                    )
                    reason = (
                        "Out-of-map / featureless clip: OrthoTrack rejected initial localization (0% localized)."
                        if is_loc_reject
                        else f"OrthoTrack exited with code {rc}. Check terminal logs."
                    )
                    self._set_step(job, "cold_start", "rejected" if is_loc_reject else "failed", reason)
                    self._set_step(job, "frame_tracking", "skipped")
                    self._set_step(job, "export_wgs84", "skipped")
                    self._write_rejected_flight(job, out_dir, video_path, video_info, reason, location_label, map_dir or sequence_dir, center_wgs84)
                    job.status = "rejected" if is_loc_reject else "failed"
                    job.error = reason
                    self._emit(job.job_id, "done", job.snapshot())
                    return

                if not cold_done:
                    self._set_step(job, "cold_start", "done", "Initial pose locked")
                self._set_step(job, "frame_tracking", "done", f"Tracked {job.frame_current or job.frame_total} frames")

                # -----------------------------------------------------------
                # STEP 5: Export WGS84 + Optional DJI .SRT Join + Metadata
                # -----------------------------------------------------------
                self._set_step(job, "export_wgs84", "running", "Converting poses to WGS84 & building telemetry...")
                traj_path = out_dir / "trajectory.json"
                exp_cmd = [
                    sys.executable,
                    str(SCRIPTS_DIR / "export_trajectory.py"),
                    "--results",
                    str(results_json),
                    "--video",
                    str(video_path),
                    "--out",
                    str(traj_path),
                ]
                if sequence_dir is not None:
                    exp_cmd += ["--sequence-dir", str(sequence_dir)]
                elif map_dir is not None:
                    exp_cmd += ["--map-dir", str(map_dir)]

                rc_exp = self._run_subprocess(job, exp_cmd, base_env)
                if rc_exp != 0 or not traj_path.exists():
                    raise RuntimeError(f"Trajectory export failed (exit {rc_exp})")

                if gt_srt_path is not None and gt_srt_path.exists():
                    self._log(job, f"[GT] Joining DJI .SRT telemetry from {gt_srt_path.name}...")
                    apply_dji_srt_to_trajectory(traj_path, gt_srt_path)

                traj_data = json.loads(traj_path.read_text(encoding="utf-8"))
                cov = traj_data.get("summary", {}).get("coverage_pct", 0.0)
                final_status = "COMPLETED" if cov > 0 else "REJECTED"

                meta = {
                    "flight_id": job.flight_id,
                    "name": job.name,
                    "location": location_label,
                    "created_at": job.created_at,
                    "status": final_status,
                    "video_path": str(video_path.relative_to(ROOT_DIR)) if video_path.is_relative_to(ROOT_DIR) else str(video_path),
                    "map_dir": str((map_dir or sequence_dir).relative_to(ROOT_DIR))
                    if ((map_dir or sequence_dir) and (map_dir or sequence_dir).is_relative_to(ROOT_DIR))
                    else (str(map_dir or sequence_dir) if (map_dir or sequence_dir) else None),
                    "center_wgs84": center_wgs84,
                    "vlm_result": job.vlm_result,
                    "params": {k: v for k, v in p.items() if "key" not in k.lower() and "token" not in k.lower()},
                }
                (out_dir / "flight_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

                self._set_step(job, "export_wgs84", "done", f"Saved {job.flight_id} ({cov}% localized)")
                job.status = "completed" if cov > 0 else "rejected"
                self._emit(job.job_id, "done", job.snapshot())

            except Exception as exc:
                self._log(job, f"[ERROR] {exc}")
                for s in job.steps:
                    if s["status"] == "running":
                        s["status"] = "failed"
                        s["detail"] = str(exc)
                job.status = "failed"
                job.error = str(exc)
                self._emit(job.job_id, "done", job.snapshot())


JOB_MANAGER = JobManager()
