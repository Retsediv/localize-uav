# Groundframe — 6-DoF UAV Visual Geo-Localization Studio

Video-only 6-DoF UAV localization anchored in public 3D geodata (no GPS or IMU required), paired with a synchronized **CesiumJS + Google Photorealistic 3D Tiles** Web Studio.

## How It Works

1. **Coarse Region Detection (Cold Start)** — OpenRouter Vision (`app/vlm.py`) inspects initial frames to infer approximate `(lat, lon)` (or the user drops a manual pin / selects a cached map).
2. **On-Demand 3D Map Fetch** — Checks `data/maps/` cache first; if missing and inside Switzerland, fetches only the needed `1×1 km` GeoTIFF tiles via `scripts/fetch_swisstopo.py`.
3. **Dense Localization & Tracking** — [OrthoTrack](https://github.com/cvg/orthotrack) + [RoMaV2](https://github.com/Parskatt/RoMaV2) matches keyframes against the orthophoto, lifts matches to 3D via the DSM, auto-calibrates FoV, solves 6-DoF PnP, and tracks inter-frame motion with Lucas-Kanade optical flow.
4. **WGS84 Export & 3D Replay** — Converts projected poses to WGS84 (`trajectory.json`, CSV, GeoJSON, 3D KML) and replays synchronized real video alongside Google Photorealistic 3D Tiles (`Orbit`, `Chase`, `Drone-Eye`, and `Wipe Compare`).

## 3D Map Data & On-Demand Tile Loading

The localization engine is **geographically agnostic** — it only requires two metric GeoTIFF layers covering the flight area:
- **DOP (Orthophoto):** overhead RGB imagery (`≤ 0.2 m/px` recommended).
- **DSM (Digital Surface Model):** per-pixel surface elevation including roofs and terrain.

### Why Switzerland (`swisstopo`)?
- Downloading all of Switzerland at `10 cm/px` would take **~2–3 TB**. Instead, this repo integrates the free public **swisstopo STAC API** (`https://data.geo.admin.ch/api/stac/v1/`), which covers **100% of Switzerland** in `1×1 km` tiles (`EPSG:2056`):
  - `ch.swisstopo.swissimage-dop10` — 10 cm/px orthophoto (25 cm in high Alps).
  - `ch.swisstopo.swisssurface3d-raster` — 50 cm/px LiDAR DSM.
- When a new Swiss location is requested, `scripts/fetch_swisstopo.py` downloads **only the 1–4 intersecting tiles** around `(lat, lon) ± radius` (~150–350 MB in ~15–30s) and caches them under `data/maps/` for instant reuse.
- **Outside Switzerland (global extension):** place any metric-projected `DOP` and `DSM` GeoTIFFs in `data/maps/<site>/{dop,dsm}/` (e.g., commercial satellite + stereo-DSM, Esri + Copernicus DEM, or custom photogrammetry) and select **Saved Local Map** — no tracker changes required.

> **Note on Google Photorealistic 3D Tiles:** Used strictly for 3D visualization and visual alignment verification (`Drone-Eye` / `Wipe`) in CesiumJS, not as the localization map source.

## Quick Start

**Requirements:** Linux with NVIDIA CUDA GPU (`≥ 16 GB VRAM` for `precise` matcher, `≥ 8 GB` for `turbo`), `uv`, `git`, `ffmpeg`.

```bash
# 1. Configure API keys (CESIUM_ION_TOKEN for Google 3D Tiles, OPENROUTER_API_KEY for VLM cold start)
cp .env.example .env

# 2. Clone pinned OrthoTrack/RoMaV2 + create Python 3.11 .venv via uv
make setup

# 3. Pre-cache RoMaV2 weights locally into .cache/torch
make download-weights

# 4. Launch Web Studio at http://localhost:8000
make serve
```

### Useful CLI Targets

```bash
make check-gpu                                          # Verify PyTorch CUDA & RoMaV2
make smoke-test                                         # 30-frame benchmark on MovingDrone/airport10 (~0.44m median error)
make fetch-map LAT=47.3700 LON=8.5425 RADIUS=600 MAP_DIR=data/maps/zurich
make track VIDEO=data/videos/zurich/zurich_limmat_25s.mp4 MAP_DIR=data/maps/zurich_limmat OUT=outputs/zurich
```

## Project Layout

```text
app/                 FastAPI backend (jobs, SSE stepper, OpenRouter VLM, DJI .SRT GT parser, GeoJSON/KML export)
app/static/          Zero-build Web Studio UI (CesiumJS, Leaflet mini-map, Canvas charts, Wipe Compare)
scripts/             swisstopo STAC tile downloader & WGS84 trajectory exporter
third_party/         Pinned OrthoTrack + RoMaV2 (gitignored, installed by `make setup`)
data/                Cached maps, demo videos, and uploads (gitignored)
outputs/             Saved flight trajectories, keyframe match visuals, and metrics (gitignored)
```

## Known Constraints

- Works best on continuous flights with oblique-downward to nadir views (`~30°–90°` downward) over structured landmarks (buildings, roads, bridges, rivers).
- Featureless scenes (open sea, uniform dense forest) are intentionally rejected (`REJECTED · 0%`) rather than hallucinating poses.
- [OrthoTrack](https://github.com/cvg/orthotrack) is licensed under CC BY-NC 4.0 (research/demo use).
