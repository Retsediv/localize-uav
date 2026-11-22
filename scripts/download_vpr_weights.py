#!/usr/bin/env python3
"""Download & cache VPR model weights locally under .cache/:
  1. MegaLoc (gmberton/MegaLoc via torch.hub -> .cache/torch)
  2. Sample4Geo ConvNeXt-Base University-1652 (weights_e1_0.9515.pth -> .cache/vpr/)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import requests
import torch
from tqdm import tqdm

ROOT_DIR = Path(__file__).resolve().parent.parent
TORCH_HOME = ROOT_DIR / ".cache" / "torch"
VPR_CACHE = ROOT_DIR / ".cache" / "vpr"
SAMPLE4GEO_CKPT = VPR_CACHE / "sample4geo_u1652_convnext_base.pth"

# Official Sample4Geo Google Drive weights folder (from Skyy93/Sample4Geo README)
GDRIVE_ROOT_FOLDER = "1PMuUqvDnCb216D8_ZDDJzDD3FxeH5BoA"


def download_megaloc() -> None:
    os.environ["TORCH_HOME"] = str(TORCH_HOME)
    TORCH_HOME.mkdir(parents=True, exist_ok=True)
    print(f"[MegaLoc] Loading/caching gmberton/MegaLoc into {TORCH_HOME}...")
    model = torch.hub.load("gmberton/MegaLoc", "get_trained_model", trust_repo=True)
    model.eval()
    x = torch.randn(1, 3, 322, 322)
    with torch.no_grad():
        y = model(x)
    print(f"[MegaLoc] Ready! Output dim={tuple(y.shape)}")


def _list_gdrive_folder(folder_id: str) -> list[tuple[str, str]]:
    """Parse (file_or_folder_id, name) pairs from a public Google Drive folder HTML page."""
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    html = r.text
    # Google Drive embeds item tuples like ["<id>",["<parent>"],"<name>","<mime>"...]
    matches = re.findall(r'\["([a-zA-Z0-9_-]{25,45})",\["' + re.escape(folder_id) + r'"\],"([^"]+)"', html)
    if not matches:
        # Unescaped or escaped JSON variant
        html_u = html.replace(r'\"', '"')
        matches = re.findall(r'\["([a-zA-Z0-9_-]{25,45})",\["' + re.escape(folder_id) + r'"\],"([^"]+)"', html_u)
    seen = {}
    for fid, name in matches:
        seen[fid] = name
    return list(seen.items())


def _download_gdrive_file(file_id: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype.lower():
            raise RuntimeError(f"Google Drive returned HTML instead of binary checkpoint (ctype={ctype})")
        total = int(r.headers.get("content-length", 0))
        with open(tmp, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True, unit_divisor=1024, desc=f"  {dest.name}"
        ) as pbar:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))
    if tmp.stat().st_size < 10_000_000:
        raise RuntimeError(f"Downloaded file too small ({tmp.stat().st_size} bytes) — likely not a valid .pth checkpoint")
    tmp.rename(dest)


def download_sample4geo() -> Path:
    if SAMPLE4GEO_CKPT.exists() and SAMPLE4GEO_CKPT.stat().st_size > 100_000_000:
        print(f"[Sample4Geo] Already cached: {SAMPLE4GEO_CKPT} ({SAMPLE4GEO_CKPT.stat().st_size / 1e6:.1f} MB)")
        return SAMPLE4GEO_CKPT

    import gdown

    VPR_CACHE.mkdir(parents=True, exist_ok=True)
    url = f"https://drive.google.com/drive/folders/{GDRIVE_ROOT_FOLDER}"
    print(f"[Sample4Geo] Listing official Google Drive folder {url}...")
    files = gdown.download_folder(url=url, skip_download=True, quiet=False) or []
    print(f"  Found {len(files)} entries:")
    for f in files:
        print(f"    - id={f.id} path={f.path}")

    # Pick University-1652 ConvNeXt-Base checkpoint (weights_e1_0.9515.pth or similar)
    candidates = [
        f for f in files
        if (f.path.lower().endswith(".pth") or f.path.lower().endswith(".pt"))
        and ("univ" in f.path.lower() or "u1652" in f.path.lower() or "0.9515" in f.path.lower())
    ]
    if not candidates:
        candidates = [f for f in files if f.path.lower().endswith(".pth") or f.path.lower().endswith(".zip")]

    candidates.sort(key=lambda f: (
        0 if "0.9515" in f.path else 1,
        0 if ("univ" in f.path.lower() and "convnext_base" in f.path.lower()) else 1,
        0 if "univ" in f.path.lower() else 1,
    ))
    if not candidates:
        raise RuntimeError("Could not locate Sample4Geo University-1652 checkpoint in Google Drive folder.")

    target = candidates[0]
    print(f"[Sample4Geo] Downloading {target.path} (id={target.id}) -> {SAMPLE4GEO_CKPT}...")
    if target.path.lower().endswith(".zip"):
        import shutil, zipfile
        zip_tmp = VPR_CACHE / "pretrained.zip"
        out = gdown.download(id=target.id, output=str(zip_tmp), quiet=False)
        if not out or not zip_tmp.exists():
            raise RuntimeError(f"gdown failed to download {target.path}")
        with zipfile.ZipFile(zip_tmp) as zf:
            names = zf.namelist()
            member = next(n for n in names if "university" in n.lower() and "convnext_base" in n.lower() and n.endswith(".pth"))
            print(f"[Sample4Geo] Extracting {member}...")
            with zf.open(member) as src, open(SAMPLE4GEO_CKPT, "wb") as dst:
                shutil.copyfileobj(src, dst)
        zip_tmp.unlink(missing_ok=True)
    else:
        tmp = SAMPLE4GEO_CKPT.with_suffix(".pth.part")
        out = gdown.download(id=target.id, output=str(tmp), quiet=False)
        if not out or not tmp.exists() or tmp.stat().st_size < 10_000_000:
            raise RuntimeError(f"gdown failed to download Sample4Geo checkpoint ({target.path})")
        tmp.rename(SAMPLE4GEO_CKPT)

    sd = torch.load(SAMPLE4GEO_CKPT, map_location="cpu", weights_only=False)
    keys = list(sd.keys()) if isinstance(sd, dict) else []
    print(f"[Sample4Geo] Verified checkpoint! {len(keys)} tensors, sample keys={keys[:3]}")
    return SAMPLE4GEO_CKPT


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "megaloc"):
        download_megaloc()
    if which in ("all", "sample4geo"):
        download_sample4geo()


if __name__ == "__main__":
    main()
