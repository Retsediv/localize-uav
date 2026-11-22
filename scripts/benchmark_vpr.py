#!/usr/bin/env python3
"""Head-to-head ablation benchmark: MegaLoc vs Sample4Geo on the offline Mini-Switzerland VPR index,
including RoMaV2 + PnP-RANSAC geometric verification and 4 out-of-map stress tests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import cv2
import numpy as np
import torch
from PIL import Image

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import DATA_DIR, OUTPUTS_DIR
from app.vpr import GeometricVerifier, OfflineVPRIndex, VPR_INDEX_DIR, VPREncoder


def extract_video_frames(video_path: Path, count: int) -> List[Dict[str, Any]]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fracs = np.linspace(0.05, 0.90, count)
        indices = [min(max(0, total - 1), int(total * f)) for f in fracs]
        out = []
        for fid in indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fid)
            ok, bgr = cap.read()
            if ok and bgr is not None:
                out.append({
                    "frame_id": int(fid),
                    "rgb": cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
                })
        return out
    finally:
        cap.release()


def extract_image_dir_frames(img_dir: Path, count: int) -> List[Dict[str, Any]]:
    files = sorted(img_dir.glob("*.jpg"))
    if not files:
        raise RuntimeError(f"No JPGs in {img_dir}")
    idxs = np.linspace(0, len(files) - 1, count).astype(int)
    out = []
    for i in idxs:
        bgr = cv2.imread(str(files[int(i)]))
        if bgr is not None:
            out.append({
                "frame_id": int(i),
                "filename": files[int(i)].name,
                "rgb": cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
            })
    return out


def build_query_suite(n_pos: int = 8, n_neg: int = 4) -> List[Dict[str, Any]]:
    suite: List[Dict[str, Any]] = []

    pos_sources = [
        ("zurich_limmat", "video", DATA_DIR / "videos/zurich/zurich_limmat_25s.mp4", "zurich_limmat"),
        ("lucerne", "video", DATA_DIR / "videos/lucerne/lucerne_kapellbruecke_22s.mp4", "lucerne"),
        ("zurich_mav", "dir", DATA_DIR / "zurich_mav/AGZ_subset/MAV Images", "zurich_mav"),
    ]
    for name, kind, path, expected_region in pos_sources:
        frames = extract_video_frames(path, n_pos) if kind == "video" else extract_image_dir_frames(path, n_pos)
        for f in frames:
            suite.append({
                "group": name,
                "positive": True,
                "expected_region": expected_region,
                "frame_id": f["frame_id"],
                "rgb": f["rgb"],
            })

    neg_sources = [
        ("stress_forest_pine", DATA_DIR / "videos/stress_tests/forest_pine_21s.mp4"),
        ("stress_water_sea", DATA_DIR / "videos/stress_tests/water_sea_12s.mp4"),
        ("stress_fields_cultivated", DATA_DIR / "videos/stress_tests/fields_cultivated_15s.mp4"),
        ("stress_suburb_residential", DATA_DIR / "videos/stress_tests/suburb_residential_20s.mp4"),
    ]
    for name, path in neg_sources:
        for f in extract_video_frames(path, n_neg):
            suite.append({
                "group": name,
                "positive": False,
                "expected_region": None,
                "frame_id": f["frame_id"],
                "rgb": f["rgb"],
            })

    return suite


def evaluate_model(
    model_key: str,
    suite: List[Dict[str, Any]],
    verifier: GeometricVerifier,
    top_k: int = 5,
) -> Dict[str, Any]:
    print(f"\n{'=' * 64}\n[Benchmark] Evaluating '{model_key}' on {len(suite)} queries (Top-{top_k} + PnP)\n{'=' * 64}")

    info_file = VPR_INDEX_DIR / f"{model_key}_info.json"
    build_info = json.loads(info_file.read_text(encoding="utf-8")) if info_file.exists() else {}

    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    encoder = VPREncoder(model_key=model_key)
    # Warmup 1 pass
    _ = encoder.encode_pils([Image.fromarray(suite[0]["rgb"])], batch_size=1)

    encode_times_ms: List[float] = []
    query_embs: List[np.ndarray] = []
    for q in suite:
        t0 = time.perf_counter()
        emb = encoder.encode_pils([Image.fromarray(q["rgb"])], batch_size=1)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        encode_times_ms.append((time.perf_counter() - t0) * 1000.0)
        query_embs.append(emb[0])

    query_vram_mb = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1) if torch.cuda.is_available() else 0.0
    del encoder
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    index = OfflineVPRIndex(model_key=model_key)
    # Warmup 1 GPU matmul pass before timing search_ms
    _ = index.search_embeddings(query_embs[0], top_k=top_k, dedup_radius_m=250.0)

    per_query_records: List[Dict[str, Any]] = []
    search_times_ms: List[float] = []
    correct_inliers: List[int] = []
    wrong_inliers: List[int] = []
    neg_inliers: List[int] = []

    for q, emb in zip(suite, query_embs):
        cands, s_ms = index.search_embeddings(emb, top_k=top_k, dedup_radius_m=250.0)
        search_times_ms.append(s_ms)

        exp = q["expected_region"]
        regions = [c["region"] for c in cands]
        r1 = bool(q["positive"] and regions and regions[0] == exp)
        r5 = bool(q["positive"] and exp in regions)

        verified_cands = []
        for c in cands:
            ver = verifier.verify_candidate(q["rgb"], c)
            rec = {
                "region": c["region"],
                "center_lat": c["center_lat"],
                "center_lon": c["center_lon"],
                "size_m": c["size_m"],
                "rot_deg": c["rot_deg"],
                "score": c["score"],
                **ver,
            }
            verified_cands.append(rec)
            if q["positive"]:
                if c["region"] == exp:
                    correct_inliers.append(ver["inliers"])
                else:
                    wrong_inliers.append(ver["inliers"])
            else:
                neg_inliers.append(ver["inliers"])

        accepted = [v for v in verified_cands if v["accepted"]]
        accepted.sort(key=lambda v: (v["inliers"], v["score"]), reverse=True)
        winner = accepted[0] if accepted else None

        pnp_correct = bool(q["positive"] and winner is not None and winner["region"] == exp)
        false_positive = bool((not q["positive"]) and winner is not None)

        per_query_records.append({
            "group": q["group"],
            "positive": q["positive"],
            "expected_region": exp,
            "frame_id": q["frame_id"],
            "r1": r1,
            "r5": r5,
            "pnp_accepted": winner is not None,
            "pnp_winner_region": winner["region"] if winner else None,
            "pnp_winner_inliers": winner["inliers"] if winner else 0,
            "pnp_correct": pnp_correct,
            "false_positive": false_positive,
            "top1_score": cands[0]["score"] if cands else 0.0,
            "candidates": verified_cands,
        })

    num_tiles = index.num_tiles
    dim = index.dim
    index.close()

    # Aggregate metrics per group
    groups_summary: Dict[str, Any] = {}
    for gname in dict.fromkeys(r["group"] for r in per_query_records):
        rows = [r for r in per_query_records if r["group"] == gname]
        is_pos = rows[0]["positive"]
        if is_pos:
            groups_summary[gname] = {
                "n": len(rows),
                "positive": True,
                "recall_at_1": round(100.0 * sum(r["r1"] for r in rows) / len(rows), 1),
                "recall_at_5": round(100.0 * sum(r["r5"] for r in rows) / len(rows), 1),
                "pnp_verified_acc": round(100.0 * sum(r["pnp_correct"] for r in rows) / len(rows), 1),
                "median_top1_sim": round(float(np.median([r["top1_score"] for r in rows])), 3),
                "median_winner_inliers": round(float(np.median([r["pnp_winner_inliers"] for r in rows])), 1),
            }
        else:
            all_inl = [c["inliers"] for r in rows for c in r["candidates"]]
            groups_summary[gname] = {
                "n": len(rows),
                "positive": False,
                "false_positives": sum(r["false_positive"] for r in rows),
                "fpr_pct": round(100.0 * sum(r["false_positive"] for r in rows) / len(rows), 1),
                "median_top1_sim": round(float(np.median([r["top1_score"] for r in rows])), 3),
                "max_inliers": int(max(all_inl)) if all_inl else 0,
            }

    pos_rows = [r for r in per_query_records if r["positive"]]
    aerial_rows = [r for r in pos_rows if r["group"] in ("zurich_limmat", "lucerne")]
    neg_rows = [r for r in per_query_records if not r["positive"]]

    overall = {
        "all_pos_r1": round(100.0 * sum(r["r1"] for r in pos_rows) / max(1, len(pos_rows)), 1),
        "all_pos_r5": round(100.0 * sum(r["r5"] for r in pos_rows) / max(1, len(pos_rows)), 1),
        "all_pos_pnp_acc": round(100.0 * sum(r["pnp_correct"] for r in pos_rows) / max(1, len(pos_rows)), 1),
        "aerial_r1": round(100.0 * sum(r["r1"] for r in aerial_rows) / max(1, len(aerial_rows)), 1),
        "aerial_r5": round(100.0 * sum(r["r5"] for r in aerial_rows) / max(1, len(aerial_rows)), 1),
        "aerial_pnp_acc": round(100.0 * sum(r["pnp_correct"] for r in aerial_rows) / max(1, len(aerial_rows)), 1),
        "stress_false_positives": sum(r["false_positive"] for r in neg_rows),
        "stress_total": len(neg_rows),
        "stress_fpr_pct": round(100.0 * sum(r["false_positive"] for r in neg_rows) / max(1, len(neg_rows)), 1),
        "inliers_correct_median": round(float(np.median(correct_inliers)), 1) if correct_inliers else 0.0,
        "inliers_wrong_median": round(float(np.median(wrong_inliers)), 1) if wrong_inliers else 0.0,
        "inliers_neg_median": round(float(np.median(neg_inliers)), 1) if neg_inliers else 0.0,
        "inliers_neg_max": int(max(neg_inliers)) if neg_inliers else 0,
        "encode_ms_median": round(float(np.median(encode_times_ms)), 2),
        "search_ms_median": round(float(np.median(search_times_ms)), 2),
        "total_query_ms_median": round(float(np.median(encode_times_ms) + np.median(search_times_ms)), 2),
        "query_vram_mb": query_vram_mb,
        "build_time_s": build_info.get("build_time_s"),
        "index_size_mb": build_info.get("index_size_mb"),
        "num_tiles": num_tiles,
        "dim": dim,
    }

    print(f"  [{model_key}] Aerial (Zurich+Lucerne): R@1={overall['aerial_r1']}%, R@5={overall['aerial_r5']}%, PnP={overall['aerial_pnp_acc']}%")
    print(f"  [{model_key}] All Positive (+AGZ):     R@1={overall['all_pos_r1']}%, R@5={overall['all_pos_r5']}%, PnP={overall['all_pos_pnp_acc']}%")
    print(f"  [{model_key}] Stress FPR:              {overall['stress_false_positives']}/{overall['stress_total']} ({overall['stress_fpr_pct']}%) | max neg inliers={overall['inliers_neg_max']}")
    print(f"  [{model_key}] Latency: encode={overall['encode_ms_median']}ms + search={overall['search_ms_median']}ms | index={overall['index_size_mb']} MB")

    return {
        "model": model_key,
        "build_info": build_info,
        "overall": overall,
        "groups": groups_summary,
        "queries": per_query_records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark MegaLoc vs Sample4Geo on Mini-Switzerland VPR index.")
    parser.add_argument("--models", nargs="+", default=["megaloc", "sample4geo"], choices=["megaloc", "sample4geo"])
    parser.add_argument("--n-pos", type=int, default=8, help="Frames sampled per positive video/dataset (default: 8)")
    parser.add_argument("--n-neg", type=int, default=4, help="Frames sampled per negative stress-test video (default: 4)")
    parser.add_argument("--out", type=Path, default=OUTPUTS_DIR / "vpr_benchmark" / "benchmark_results.json")
    args = parser.parse_args()

    suite = build_query_suite(n_pos=args.n_pos, n_neg=args.n_neg)
    print(f"Built benchmark query suite: {len(suite)} total frames")

    verifier = GeometricVerifier(roma_setting="base")
    results: Dict[str, Any] = {}
    for mk in args.models:
        results[mk] = evaluate_model(mk, suite, verifier, top_k=5)

    # Pick winner objectively: highest aerial PnP acc -> aerial R@5 -> aerial R@1 -> lowest FPR -> smallest index
    def _rank_key(mk: str):
        o = results[mk]["overall"]
        return (
            o["aerial_pnp_acc"],
            -o["stress_fpr_pct"],
            o["aerial_r5"],
            o["aerial_r1"],
            o["all_pos_r5"],
            -(o["index_size_mb"] or 9999),
        )

    winner = max(args.models, key=_rank_key)
    payload = {
        "winner": winner,
        "models": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (VPR_INDEX_DIR / "active_model.json").write_text(
        json.dumps({"model": winner, "benchmark_file": str(args.out.relative_to(ROOT_DIR))}, indent=2),
        encoding="utf-8",
    )
    print(f"\n=== WINNER: {winner} ===\nSaved full benchmark results to {args.out}")


if __name__ == "__main__":
    main()
