# Reviewer Setup

Step-by-step checklist to get Groundframe running on your machine with the same data and results as the author.
Tested targets: **macOS (Apple Silicon, no GPU)** for the Web Studio, **Linux + NVIDIA GPU** for running new tracking jobs.

## What works where

| Feature | macOS / no GPU | Linux + NVIDIA |
|---|---|---|
| Web Studio: browse precomputed flights, 3D replay, charts, KML/GeoJSON export | yes | yes |
| VPR cold start (MegaLoc / Sample4Geo search over the Mini-Switzerland index) | yes, slow (CPU) | yes |
| New OrthoTrack tracking job (`+ New Flight` -> tracking, `make track`, `make smoke-test`) | no (OrthoTrack calls `.cuda()` directly) | yes |

## Checklist

### 1. Install system tools

- [ ] **macOS**
  ```bash
  xcode-select --install
  ```
  ```bash
  brew install uv ffmpeg
  ```
- [ ] **Linux (Debian/Ubuntu)**
  ```bash
  sudo apt install -y git make ffmpeg unzip
  ```
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

You do not need to install Python yourself: `uv` downloads Python 3.11 automatically.

### 2. Clone the project

- [ ] Clone the repository and enter it:
  ```bash
  git clone https://github.com/Retsediv/localize-uav.git
  ```
  ```bash
  cd localize-uav
  ```

### 3. Download and unpack the data

- [ ] Download `groundframe-data.zip` (~8 GB) from the link the author sent you.
- [ ] Move it into the project root (the `localize-uav/` folder) and unpack it there:
  ```bash
  unzip groundframe-data.zip
  ```
  If macOS `unzip` complains about the archive size, use `ditto -x -k groundframe-data.zip .` instead.
- [ ] Check that three folders appeared in the project root:
  ```bash
  ls -d data outputs .cache
  ```

What is inside:

| Folder | Contents |
|---|---|
| `data/maps/` | swisstopo SWISSIMAGE orthophotos + swissSURFACE3D DSM tiles for each demo area |
| `data/videos/` | demo drone videos (Zurich, Lucerne, EPFL, stress tests) |
| `data/vpr_index/`, `data/vpr_swiss_2m/` | prebuilt Mini-Switzerland VPR index (takes hours to rebuild, so it is shipped) |
| `data/MovingDrone/`, `data/zurich_mav/`, `data/crossloc/` | benchmark datasets with ground truth |
| `outputs/` | precomputed flights shown in the Web Studio |
| `.cache/` | pretrained weights (RoMaV2, MegaLoc, Sample4Geo), so `make download-weights` is not needed |

### 4. Get a Cesium ion token (free, ~2 minutes)

Without a token the 3D view falls back to a plain OpenStreetMap globe. With it you get Google Photorealistic 3D Tiles.

- [ ] Sign up at https://ion.cesium.com/signup (free Community plan).
- [ ] Open **Asset Depot**, find **Google Photorealistic 3D Tiles** (asset id `2275207`) and click **Add to my assets**.
- [ ] Open **Access Tokens** (https://ion.cesium.com/tokens) and copy the `Default` token, or create a new one with the `assets:read` scope.
- [ ] Create your `.env` and paste the token into it:
  ```bash
  cp .env.example .env
  ```
  Then edit `.env`:
  ```
  CESIUM_ION_TOKEN=<your token>
  ```
- [ ] Optional: set `OPENROUTER_API_KEY` in `.env` to enable the VLM-based region guess for videos without GPS. Everything else works without it.

### 5. Create the Python environment

- [ ] Run the setup (clones the pinned OrthoTrack + RoMaV2 into `third_party/`, creates `.venv` with Python 3.11):
  ```bash
  make setup
  ```
- [ ] Optional sanity check (on a Mac it prints `CUDA available: False`, which is expected):
  ```bash
  make check-gpu
  ```

### 6. Launch the Web Studio

- [ ] Start the server:
  ```bash
  make serve
  ```
- [ ] Open http://localhost:8000. The precomputed flights from `outputs/` are listed in the sidebar. Pick one to replay it in 3D with the synced charts.
- [ ] Stop the server with `Ctrl+C`.

### 7. Optional: Linux + NVIDIA only

- [ ] 30-frame OrthoTrack smoke test on MovingDrone/airport10:
  ```bash
  make smoke-test
  ```
- [ ] MegaLoc vs Sample4Geo VPR benchmark (rewrites `outputs/vpr_benchmark/`):
  ```bash
  make benchmark-vpr
  ```
- [ ] Track a custom video against a fetched map:
  ```bash
  make track VIDEO=data/videos/zurich/zurich_limmat_25s.mp4 MAP_DIR=data/maps/zurich_limmat OUT=outputs/my_zurich_run
  ```

Run `make help` to see all targets.

## Troubleshooting

- **The 3D view says "OSM Fallback (check Cesium token)"**: the token is wrong, or Google Photorealistic 3D Tiles was not added to your ion assets (step 4). Restart `make serve` after editing `.env`.
- **A flight shows no video**: `data/videos/` is missing. Re-check step 3.
- **Port 8000 is busy**: run `PORT=8010 make serve`.
