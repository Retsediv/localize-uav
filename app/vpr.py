"""Offline Cross-View Visual Place Recognition (VPR) & Geometric Verification.

Supports:
  - MegaLoc (gmberton/MegaLoc via torch.hub, DINOv2 + SALAD, 8448-dim)
  - Sample4Geo (ConvNeXt-Base trained on University-1652, 1024-dim)
  - Fast exact cosine similarity search over L2-normalized float16 embeddings
  - Multi-frame query fusion + spatial deduplication (250m)
  - Geometric verification via RoMaV2 coarse + PnP-RANSAC (3D DSM when local high-res
    map exists, or planar homography + PnP on 2m/px orthophoto crops before fetching high-res).
"""

from __future__ import annotations

import json
import math
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
import timm
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms as T

from app.config import DATA_DIR, ORTHOTRACK_DIR, ROOT_DIR, TORCH_HOME_DIR

VPR_INDEX_DIR = DATA_DIR / "vpr_index"
VPR_SWISS_2M_DIR = DATA_DIR / "vpr_swiss_2m"
SAMPLE4GEO_CKPT = ROOT_DIR / ".cache" / "vpr" / "sample4geo_u1652_convnext_base.pth"

# Top-10 Swiss cities (~215+ km² total when combined with our 3 local high-res maps)
SWISS_CITIES: List[Dict[str, Any]] = [
    {"id": "bern", "name": "Bern", "lat": 46.9480, "lon": 7.4474, "radius_m": 2000.0},
    {"id": "basel", "name": "Basel", "lat": 47.5596, "lon": 7.5886, "radius_m": 2000.0},
    {"id": "geneva", "name": "Geneva", "lat": 46.2044, "lon": 6.1432, "radius_m": 2000.0},
    {"id": "lausanne", "name": "Lausanne", "lat": 46.5197, "lon": 6.6323, "radius_m": 2000.0},
    {"id": "interlaken", "name": "Interlaken", "lat": 46.6863, "lon": 7.8632, "radius_m": 2000.0},
    {"id": "lugano", "name": "Lugano", "lat": 46.0037, "lon": 8.9511, "radius_m": 2000.0},
    {"id": "st_gallen", "name": "St. Gallen", "lat": 47.4245, "lon": 9.3767, "radius_m": 1800.0},
    {"id": "zermatt", "name": "Zermatt", "lat": 46.0207, "lon": 7.7491, "radius_m": 1500.0},
]


# ---------------------------------------------------------------------------
# VPR Encoders (MegaLoc & Sample4Geo)
# ---------------------------------------------------------------------------

class Sample4GeoModel(nn.Module):
    """Exact Sample4Geo ConvNeXt-Base architecture matching Skyy93/Sample4Geo checkpoints."""

    def __init__(self, model_name: str = "convnext_base.fb_in22k_ft_in1k_384"):
        super().__init__()
        self.model = timm.create_model(model_name, pretrained=False, num_classes=0)
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1.0 / 0.07))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model(x)


class VPREncoder:
    """Unified wrapper for MegaLoc and Sample4Geo batch image encoding."""

    def __init__(self, model_key: str, device: str = "cuda"):
        self.model_key = model_key.lower()
        self.device = device if torch.cuda.is_available() else "cpu"
        os.environ["TORCH_HOME"] = str(TORCH_HOME_DIR)

        if self.model_key == "megaloc":
            self.input_size = 322
            self.model = torch.hub.load("gmberton/MegaLoc", "get_trained_model", trust_repo=True)
            self.model = self.model.to(self.device).eval()
            self.transform = T.Compose([
                T.Resize((self.input_size, self.input_size), interpolation=T.InterpolationMode.BICUBIC),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ])
        elif self.model_key == "sample4geo":
            if not SAMPLE4GEO_CKPT.exists():
                raise FileNotFoundError(
                    f"Sample4Geo checkpoint missing: {SAMPLE4GEO_CKPT}. Run scripts/download_vpr_weights.py first."
                )
            self.input_size = 384
            self.model = Sample4GeoModel("convnext_base.fb_in22k_ft_in1k_384")
            sd = torch.load(SAMPLE4GEO_CKPT, map_location="cpu", weights_only=False)
            if isinstance(sd, dict) and "state_dict" in sd:
                sd = sd["state_dict"]
            # Strip optional DataParallel 'module.' prefix
            cleaned = {k.replace("module.", "", 1): v for k, v in sd.items()}
            missing, unexpected = self.model.load_state_dict(cleaned, strict=False)
            if len(missing) > 2:
                raise RuntimeError(f"Sample4Geo checkpoint mismatch! Missing keys: {missing[:10]}")
            self.model = self.model.to(self.device).eval()
            self.transform = T.Compose([
                T.Resize((self.input_size, self.input_size), interpolation=T.InterpolationMode.BICUBIC),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ])
        else:
            raise ValueError(f"Unknown VPR model_key: {model_key!r} (expected 'megaloc' or 'sample4geo')")

    @torch.inference_mode()
    def encode_pils(self, images: List[Image.Image], batch_size: int = 64) -> np.ndarray:
        """Return L2-normalized float32 embeddings of shape (N, D)."""
        feats: List[np.ndarray] = []
        for i in range(0, len(images), batch_size):
            batch = torch.stack([self.transform(im.convert("RGB")) for im in images[i : i + batch_size]])
            batch = batch.to(self.device, non_blocking=True)
            with torch.amp.autocast("cuda", enabled=(self.device == "cuda")):
                out = self.model(batch)
            out = F.normalize(out.float(), p=2, dim=-1)
            feats.append(out.cpu().numpy())
        return np.concatenate(feats, axis=0) if feats else np.zeros((0, 1), dtype=np.float32)


# ---------------------------------------------------------------------------
# Offline Index Loader & Multi-Frame Retrieval
# ---------------------------------------------------------------------------

def get_active_vpr_model() -> str:
    cfg_file = VPR_INDEX_DIR / "active_model.json"
    if cfg_file.exists():
        try:
            return str(json.loads(cfg_file.read_text(encoding="utf-8")).get("model", "sample4geo"))
        except Exception:
            pass
    if (VPR_INDEX_DIR / "sample4geo_embeddings.npy").exists():
        return "sample4geo"
    return "megaloc"


class OfflineVPRIndex:
    """Loads precomputed L2-normalized float16 embeddings & tile metadata for fast retrieval."""

    def __init__(self, model_key: Optional[str] = None, index_dir: Path = VPR_INDEX_DIR):
        self.index_dir = Path(index_dir)
        self.model_key = (model_key or get_active_vpr_model()).lower()
        meta_path = self.index_dir / "tiles_meta.json"
        emb_path = self.index_dir / f"{self.model_key}_embeddings.npy"
        if not meta_path.exists() or not emb_path.exists():
            raise FileNotFoundError(
                f"VPR index not built for '{self.model_key}' in {self.index_dir}. Run `make build-index`."
            )
        self.tiles: List[Dict[str, Any]] = json.loads(meta_path.read_text(encoding="utf-8"))
        emb = np.load(emb_path)
        self.num_tiles, self.dim = emb.shape
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.device == "cuda":
            # Keep float16 matrix in GPU VRAM (~88 MB Sample4Geo, ~727 MB MegaLoc) for <1 ms exact search
            self.emb_gpu: Optional[torch.Tensor] = torch.from_numpy(emb).to(self.device, dtype=torch.float16)
            self.emb_np: Optional[np.ndarray] = None
        else:
            self.emb_gpu = None
            self.emb_np = emb.astype(np.float32, copy=False)

    def close(self) -> None:
        if self.emb_gpu is not None:
            del self.emb_gpu
            self.emb_gpu = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def _dedup_top_k_single(
        self,
        scores_1d: np.ndarray,
        top_k: int,
        dedup_radius_m: float,
        query_idx: int,
    ) -> List[Dict[str, Any]]:
        cand_pool = min(self.num_tiles, max(top_k * 40, 200))
        top_indices = np.argpartition(-scores_1d, cand_pool - 1)[:cand_pool]
        top_indices = top_indices[np.argsort(-scores_1d[top_indices])]

        results: List[Dict[str, Any]] = []
        for idx in top_indices:
            t = self.tiles[int(idx)]
            cx, cy = float(t["center_x"]), float(t["center_y"])
            too_close = False
            for r in results:
                if r["region"] == t["region"] and math.hypot(cx - r["center_x"], cy - r["center_y"]) < dedup_radius_m:
                    too_close = True
                    break
            if too_close:
                continue
            item = dict(t)
            item["score"] = round(float(scores_1d[idx]), 4)
            item["best_query_idx"] = int(query_idx)
            results.append(item)
            if len(results) >= top_k:
                break
        return results

    def search_embeddings(
        self,
        query_embs: np.ndarray,
        top_k: int = 5,
        dedup_radius_m: float = 250.0,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Search 1..M query frames, spatially deduplicate, and return Top-K distinct
        spatial candidates plus query time in ms.

        For M > 1 frames, uses Reciprocal Rank Fusion (RRF) over per-frame spatially
        deduplicated Top-K lists so a single noisy/high-background frame cannot crowd
        out strong localized landmarks from other frames.
        """
        t0 = time.perf_counter()
        q = np.asarray(query_embs, dtype=np.float32)
        if q.ndim == 1:
            q = q[None, :]

        if self.emb_gpu is not None:
            with torch.inference_mode():
                q_t = torch.from_numpy(q).to(self.device, dtype=torch.float16)
                sims_t = torch.matmul(q_t, self.emb_gpu.T).float()
                torch.cuda.synchronize()
                sims = sims_t.cpu().numpy()
        else:
            assert self.emb_np is not None
            sims = q @ self.emb_np.T

        if sims.shape[0] == 1:
            results = self._dedup_top_k_single(sims[0], top_k=top_k, dedup_radius_m=dedup_radius_m, query_idx=0)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return results, elapsed_ms

        # Multi-frame Reciprocal Rank Fusion (RRF) over spatial clusters
        per_frame_k = max(top_k * 2, 10)
        rrf_k = 10.0
        clusters: List[Dict[str, Any]] = []
        for q_idx in range(sims.shape[0]):
            frame_cands = self._dedup_top_k_single(
                sims[q_idx], top_k=per_frame_k, dedup_radius_m=dedup_radius_m, query_idx=q_idx
            )
            for rank_0, cand in enumerate(frame_cands):
                contrib = 1.0 / (rrf_k + (rank_0 + 1))
                cx, cy = float(cand["center_x"]), float(cand["center_y"])
                matched_cluster = None
                for cl in clusters:
                    if (
                        cl["rep"]["region"] == cand["region"]
                        and math.hypot(cx - cl["rep"]["center_x"], cy - cl["rep"]["center_y"]) < dedup_radius_m
                    ):
                        matched_cluster = cl
                        break
                if matched_cluster is None:
                    clusters.append({
                        "rrf": contrib,
                        "best_rank": rank_0,
                        "rep": cand,
                        "hits": 1,
                    })
                else:
                    matched_cluster["rrf"] += contrib
                    matched_cluster["hits"] += 1
                    # Keep the tile + frame with the sharpest rank (tie-break by cosine similarity)
                    if (rank_0 < matched_cluster["best_rank"]) or (
                        rank_0 == matched_cluster["best_rank"] and cand["score"] > matched_cluster["rep"]["score"]
                    ):
                        matched_cluster["best_rank"] = rank_0
                        matched_cluster["rep"] = cand

        clusters.sort(key=lambda cl: (cl["rrf"], cl["rep"]["score"]), reverse=True)
        results = []
        for cl in clusters[:top_k]:
            item = dict(cl["rep"])
            item["rrf_score"] = round(float(cl["rrf"]), 4)
            item["frame_hits"] = int(cl["hits"])
            results.append(item)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return results, elapsed_ms


# ---------------------------------------------------------------------------
# Geometric Verification (RoMaV2 coarse + PnP-RANSAC / Planar RANSAC)
# ---------------------------------------------------------------------------

def _read_crop_from_dir(
    tif_dir: Path,
    cx: float,
    cy: float,
    size_m: float,
    is_dsm: bool = False,
    target_px: int = 800,
) -> Tuple[np.ndarray, rasterio.Affine]:
    """Read a square geographic crop centered at (cx, cy) from GeoTIFF tiles in tif_dir."""
    half = size_m / 2.0
    bl, bb, br, bt = cx - half, cy - half, cx + half, cy + half
    tifs = sorted(list(tif_dir.glob("*.tif")) + list(tif_dir.glob("*.tiff")))
    overlapping = []
    for p in tifs:
        with rasterio.open(p) as src:
            b = src.bounds
            if b.right <= bl or b.left >= br or b.top <= bb or b.bottom >= bt:
                continue
            overlapping.append(p)
    if not overlapping:
        raise RuntimeError(f"No GeoTIFF tiles overlap ({cx:.0f}, {cy:.0f}) in {tif_dir}")

    res = size_m / float(target_px)
    datasets = [rasterio.open(p) for p in overlapping]
    try:
        merged, transform = rio_merge(
            datasets,
            bounds=(bl, bb, br, bt),
            res=(res, res),
            resampling=Resampling.bilinear,
        )
    finally:
        for ds in datasets:
            ds.close()

    if is_dsm:
        return merged[0].astype(np.float32), transform
    if merged.shape[0] > 3:
        merged = merged[:3]
    elif merged.shape[0] == 1:
        merged = np.repeat(merged, 3, axis=0)
    rgb = np.transpose(np.clip(merged, 0, 255).astype(np.uint8), (1, 2, 0))
    return rgb, transform


class GeometricVerifier:
    """Verifies VPR Top-K candidates using RoMaV2 coarse + 3D PnP-RANSAC (or planar RANSAC
    on 2m/px expansion tiles without local DSM) to achieve 0% false positives.
    """

    def __init__(self, roma_setting: str = "base"):
        os.environ["TORCH_HOME"] = str(TORCH_HOME_DIR)
        if str(ORTHOTRACK_DIR) not in sys.path:
            sys.path.insert(0, str(ORTHOTRACK_DIR))
        roma_src = ORTHOTRACK_DIR / "thirdparty" / "RoMaV2" / "src"
        if str(roma_src) not in sys.path:
            sys.path.insert(0, str(roma_src))
        from orthotrack.matchers.roma_matcher import RoMaV2Matcher

        self.matcher = RoMaV2Matcher(setting=roma_setting)

    def verify_candidate(
        self,
        query_rgb: np.ndarray,
        candidate: Dict[str, Any],
        conf_thresh: float = 0.40,
        min_inliers_3d: int = 120,
        min_inliers_planar: int = 250,
    ) -> Dict[str, Any]:
        """Run RoMaV2 matching + geometric RANSAC verification on one candidate tile.

        Returns dict with: accepted (bool), inliers (int), confident_matches (int),
        inlier_ratio (float), spread_ratio (float), mode ('pnp_3d' | 'pnp_planar'),
        elapsed_s (float).
        """
        t0 = time.perf_counter()
        cx, cy = float(candidate["center_x"]), float(candidate["center_y"])
        # Use at least 600m verification window so oblique drone views have rich overlap
        verify_size_m = max(float(candidate.get("size_m", 600.0)), 600.0)
        rot_deg = int(candidate.get("rot_deg", 0))

        map_dir_str = candidate.get("map_dir")
        has_local_dsm = bool(map_dir_str and (ROOT_DIR / map_dir_str / "dsm").exists())

        if has_local_dsm:
            dop_dir = ROOT_DIR / map_dir_str / "dop"
            dsm_dir = ROOT_DIR / map_dir_str / "dsm"
            dop_rgb, dop_tf = _read_crop_from_dir(dop_dir, cx, cy, verify_size_m, is_dsm=False, target_px=768)
            dsm_arr, dsm_tf = _read_crop_from_dir(dsm_dir, cx, cy, verify_size_m, is_dsm=True, target_px=768)
            mode = "pnp_3d"
        else:
            src_dir = Path(candidate["source_dop_dir"])
            if not src_dir.is_absolute():
                src_dir = ROOT_DIR / src_dir
            dop_rgb, dop_tf = _read_crop_from_dir(src_dir, cx, cy, verify_size_m, is_dsm=False, target_px=512)
            dsm_arr, dsm_tf = None, None
            mode = "pnp_planar"

        # Rotate orthophoto crop to match the retrieved tile orientation so RoMaV2 matches upright
        H_d, W_d = dop_rgb.shape[:2]
        if rot_deg == 90:
            dop_rot = cv2.rotate(dop_rgb, cv2.ROTATE_90_CLOCKWISE)
        elif rot_deg == 180:
            dop_rot = cv2.rotate(dop_rgb, cv2.ROTATE_180)
        elif rot_deg == 270:
            dop_rot = cv2.rotate(dop_rgb, cv2.ROTATE_90_COUNTERCLOCKWISE)
        else:
            dop_rot = dop_rgb

        # Resize query to max 960px wide for fast coarse verification
        Hq, Wq = query_rgb.shape[:2]
        q_scale = min(1.0, 960.0 / max(Hq, Wq))
        if q_scale < 1.0:
            q_img = cv2.resize(query_rgb, (int(Wq * q_scale), int(Hq * q_scale)), interpolation=cv2.INTER_AREA)
        else:
            q_img = query_rgb
        hq, wq = q_img.shape[:2]

        match_res = self.matcher.match(q_img, dop_rot, num_matches=2000)
        pts_q = np.asarray(match_res.kpts_query, dtype=np.float64)
        pts_d_rot = np.asarray(match_res.kpts_dop, dtype=np.float64)
        confs = np.asarray(match_res.confidences, dtype=np.float64)

        mask_conf = confs >= conf_thresh
        n_conf = int(mask_conf.sum())
        if n_conf < 25:
            return {
                "accepted": False,
                "inliers": 0,
                "confident_matches": n_conf,
                "inlier_ratio": 0.0,
                "spread_ratio": 0.0,
                "mode": mode,
                "elapsed_s": round(time.perf_counter() - t0, 2),
            }

        pts_q = pts_q[mask_conf]
        pts_d_rot = pts_d_rot[mask_conf]

        # Map rotated DOP pixel coords back to unrotated DOP pixel coords
        u_r, v_r = pts_d_rot[:, 0], pts_d_rot[:, 1]
        if rot_deg == 90:
            u_d, v_d = v_r, (H_d - 1) - u_r
        elif rot_deg == 180:
            u_d, v_d = (W_d - 1) - u_r, (H_d - 1) - v_r
        elif rot_deg == 270:
            u_d, v_d = (W_d - 1) - v_r, u_r
        else:
            u_d, v_d = u_r, v_r

        # Lift DOP pixels -> metric world (x, y, z)
        wx = dop_tf.c + u_d * dop_tf.a + v_d * dop_tf.b
        wy = dop_tf.f + u_d * dop_tf.d + v_d * dop_tf.e

        if dsm_arr is not None and dsm_tf is not None:
            cols = np.clip(np.round((wx - dsm_tf.c) / dsm_tf.a).astype(int), 0, dsm_arr.shape[1] - 1)
            rows = np.clip(np.round((wy - dsm_tf.f) / dsm_tf.e).astype(int), 0, dsm_arr.shape[0] - 1)
            wz = dsm_arr[rows, cols].astype(np.float64)
            valid_z = np.isfinite(wz) & (wz > 100.0) & (wz < 4800.0)
            pts_q = pts_q[valid_z]
            wx, wy, wz = wx[valid_z], wy[valid_z], wz[valid_z]
        else:
            wz = np.zeros_like(wx)

        if len(pts_q) < 25:
            return {
                "accepted": False,
                "inliers": 0,
                "confident_matches": n_conf,
                "inlier_ratio": 0.0,
                "spread_ratio": 0.0,
                "mode": mode,
                "elapsed_s": round(time.perf_counter() - t0, 2),
            }

        pts_3d = np.stack([wx - cx, wy - cy, wz - float(np.median(wz))], axis=1)

        # Evaluate PnP-RANSAC across 3 canonical drone FoVs (32°, 45°, 62°) and take best inliers
        best_inliers = 0
        best_inlier_idx = None
        for fov_v in (32.0, 45.0, 62.0):
            fy = (hq / 2.0) / math.tan(math.radians(fov_v / 2.0))
            K = np.array([[fy, 0.0, wq / 2.0], [0.0, fy, hq / 2.0], [0.0, 0.0, 1.0]], dtype=np.float64)
            try:
                ok, rvec, tvec, inl = cv2.solvePnPRansac(
                    pts_3d.reshape(-1, 1, 3),
                    pts_q.reshape(-1, 1, 2),
                    K,
                    None,
                    iterationsCount=1500,
                    reprojectionError=8.0,
                    confidence=0.99,
                    flags=cv2.SOLVEPNP_SQPNP,
                )
            except cv2.error:
                ok, inl = False, None
            if ok and inl is not None and len(inl) > best_inliers:
                # Reject degenerate poses behind/under the ground (camera Z must be above ground)
                R_w2c, _ = cv2.Rodrigues(rvec)
                cam_pos = (-R_w2c.T @ tvec).ravel()
                if cam_pos[2] > 8.0 and cam_pos[2] < 2500.0:
                    best_inliers = int(len(inl))
                    best_inlier_idx = inl.ravel()

        inlier_ratio = best_inliers / max(1, len(pts_q))
        spread_ratio = 0.0
        if best_inlier_idx is not None and len(best_inlier_idx) >= 10:
            iq = pts_q[best_inlier_idx]
            span_x = (np.percentile(iq[:, 0], 95) - np.percentile(iq[:, 0], 5)) / max(1.0, wq)
            span_y = (np.percentile(iq[:, 1], 95) - np.percentile(iq[:, 1], 5)) / max(1.0, hq)
            spread_ratio = float(min(1.0, math.sqrt(max(0.0, span_x * span_y))))

        min_req = min_inliers_3d if mode == "pnp_3d" else min_inliers_planar
        min_conf = 300 if mode == "pnp_3d" else 650
        accepted = (
            (best_inliers >= min_req)
            and (n_conf >= min_conf)
            and (inlier_ratio >= 0.16)
            and (spread_ratio >= 0.32)
        )

        return {
            "accepted": bool(accepted),
            "inliers": best_inliers,
            "confident_matches": n_conf,
            "inlier_ratio": round(float(inlier_ratio), 3),
            "spread_ratio": round(float(spread_ratio), 3),
            "mode": mode,
            "elapsed_s": round(time.perf_counter() - t0, 2),
        }


def sample_video_rgb_frames(video_path: Path, count: int = 5) -> List[np.ndarray]:
    """Extract evenly spaced RGB frames across the video (default 5 frames: 8%..88%)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fracs = np.linspace(0.08, 0.88, max(1, count)).tolist() if count > 1 else [0.25]
        indices = sorted({min(max(0, total - 1), max(0, int(total * f))) for f in fracs}) if total > 1 else [0]
        frames: List[np.ndarray] = []
        for idx in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, bgr = cap.read()
            if ok and bgr is not None:
                frames.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        return frames
    finally:
        cap.release()


def localize_video_offline_vpr(
    video_path: Path,
    model_key: Optional[str] = None,
    top_k: int = 5,
    log_fn=None,
) -> Dict[str, Any]:
    """Full Stage-1 offline VPR retrieval + RoMaV2/PnP-RANSAC geometric verification."""
    t_start = time.perf_counter()
    model_key = (model_key or get_active_vpr_model()).lower()
    frames_rgb = sample_video_rgb_frames(video_path, count=5)
    if not frames_rgb:
        raise RuntimeError(f"No frames decoded from {video_path}")

    pils = [Image.fromarray(f) for f in frames_rgb]
    if log_fn:
        log_fn(f"[VPR] Loading '{model_key}' encoder & offline Mini-Switzerland index...")
    encoder = VPREncoder(model_key=model_key)
    q_embs = encoder.encode_pils(pils, batch_size=len(pils))

    # Free VPR encoder VRAM before loading RoMaV2
    del encoder
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    index = OfflineVPRIndex(model_key=model_key)
    candidates, search_ms = index.search_embeddings(q_embs, top_k=top_k, dedup_radius_m=250.0)
    num_indexed = index.num_tiles
    index.close()
    if log_fn:
        log_fn(f"[VPR] Retrieved Top-{len(candidates)} candidates from {num_indexed:,} indexed tiles in {search_ms:.1f} ms")

    verifier = GeometricVerifier(roma_setting="base")
    verified_candidates: List[Dict[str, Any]] = []
    for cand in candidates:
        q_idx = min(len(frames_rgb) - 1, max(0, int(cand.get("best_query_idx", 0))))
        ver = verifier.verify_candidate(frames_rgb[q_idx], cand)
        entry = {
            "region": cand["region"],
            "map_dir": cand.get("map_dir"),
            "center_lat": cand["center_lat"],
            "center_lon": cand["center_lon"],
            "size_m": cand["size_m"],
            "rot_deg": cand["rot_deg"],
            "score": cand["score"],
            **ver,
        }
        verified_candidates.append(entry)
        mark = "✓" if entry["accepted"] else "✗"
        if log_fn:
            log_fn(
                f"  [{mark}] {entry['region']} ({entry['center_lat']:.4f}, {entry['center_lon']:.4f}) "
                f"sim={entry['score']:.3f} | inliers={entry['inliers']} ({entry['mode']}, {entry['elapsed_s']}s)"
            )

    # Free RoMaV2 VRAM before OrthoTrack subprocess starts
    del verifier
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    accepted_list = [c for c in verified_candidates if c["accepted"]]
    accepted_list.sort(key=lambda c: (c["inliers"], c["score"]), reverse=True)
    winner = accepted_list[0] if accepted_list else None

    return {
        "model": model_key,
        "num_indexed_tiles": num_indexed,
        "search_ms": round(search_ms, 2),
        "total_s": round(time.perf_counter() - t_start, 2),
        "accepted": winner is not None,
        "winner": winner,
        "candidates": verified_candidates,
    }
