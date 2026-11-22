.PHONY: help setup clone-third-party venv download-weights download-vpr-weights build-index benchmark-vpr check-gpu smoke-data smoke-test fetch-map track export serve clean

UV          ?= uv
PYTHON      := .venv/bin/python
ORTHOTRACK  := third_party/orthotrack
ROMAV2      := $(ORTHOTRACK)/thirdparty/RoMaV2
export TORCH_HOME := $(CURDIR)/.cache/torch

ORTHOTRACK_REPO   := https://github.com/cvg/orthotrack.git
ORTHOTRACK_COMMIT := c00d6f66bb053ba353ab6867df8b5e4ab70db6c3
ROMAV2_REPO       := https://github.com/Parskatt/RoMaV2.git
ROMAV2_COMMIT     := 95c9968145c8906b7b59383258e9f73b02853d89

# Default parameters for map fetching & custom tracking
LAT         ?= 47.3717
LON         ?= 8.5422
RADIUS      ?= 600
MAP_DIR     ?= data/maps/zurich_oldtown
VIDEO       ?=
OUT         ?= outputs/custom_flight
FINE_MATCHER ?= precise
EXTRA_ARGS  ?=

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

setup: clone-third-party venv ## Full reproducible setup (clone repos + uv env + RoMaV2)
	$(UV) pip install --python $(PYTHON) -e $(ROMAV2) --no-deps
	@echo "Setup complete. Run 'make check-gpu' or 'make smoke-test'."

clone-third-party: ## Clone pinned OrthoTrack and RoMaV2 into third_party/
	@mkdir -p third_party
	@if [ ! -d "$(ORTHOTRACK)/.git" ]; then \
		git clone --filter=blob:none $(ORTHOTRACK_REPO) $(ORTHOTRACK) && \
		git -C $(ORTHOTRACK) checkout $(ORTHOTRACK_COMMIT); \
	else \
		echo "[skip] $(ORTHOTRACK) already cloned"; \
	fi
	@mkdir -p $(ORTHOTRACK)/thirdparty
	@if [ ! -d "$(ROMAV2)/.git" ]; then \
		git clone --filter=blob:none $(ROMAV2_REPO) $(ROMAV2) && \
		git -C $(ROMAV2) checkout $(ROMAV2_COMMIT); \
	else \
		echo "[skip] $(ROMAV2) already cloned"; \
	fi

venv: ## Create .venv (Python 3.11) and install dependencies from pyproject.toml via uv
	$(UV) sync --python 3.11 --extra app

download-weights: ## Pre-download RoMaV2 + VPR (MegaLoc, Sample4Geo) pretrained weights
	PYTHONPATH=$(ORTHOTRACK) $(PYTHON) -c "from romav2 import RoMaV2; RoMaV2(RoMaV2.Cfg(compile=False)); print('RoMaV2 weights ready.')"
	$(PYTHON) scripts/download_vpr_weights.py

download-vpr-weights: ## Pre-download MegaLoc and Sample4Geo University-1652 weights into .cache/
	$(PYTHON) scripts/download_vpr_weights.py

build-index: ## Build offline Mini-Switzerland VPR index (MegaLoc + Sample4Geo)
	$(PYTHON) scripts/build_vpr_index.py --models megaloc sample4geo

benchmark-vpr: ## Run MegaLoc vs Sample4Geo + PnP geometric verification benchmark
	$(PYTHON) scripts/benchmark_vpr.py --models megaloc sample4geo

check-gpu: ## Verify PyTorch CUDA, GPU memory, and RoMaV2 import
	@PYTHONPATH=$(ORTHOTRACK) $(PYTHON) -c "\
import torch, rasterio, pyproj, romav2; \
print('PyTorch:', torch.__version__, '| CUDA available:', torch.cuda.is_available()); \
print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'); \
print('RoMaV2:', romav2.__file__)"

smoke-data: ## Download minimal MovingDrone scene (airport10, without depth maps)
	@if [ ! -f "data/MovingDrone/scenes/airport10/meta.json" ]; then \
		mkdir -p data/MovingDrone && \
		PYTHONPATH=$(ORTHOTRACK) $(PYTHON) -c "\
from dataset.MovingDrone import MovingDrone; \
MovingDrone(dataset_dir='data/MovingDrone', sequences=['airport10'], predownload=True, load_dop=True, load_dsm=True, load_depth=False)"; \
	else \
		echo "[skip] data/MovingDrone/scenes/airport10 already present"; \
	fi

smoke-test: smoke-data ## Run 30-frame OrthoTrack smoke test on MovingDrone/airport10
	PYTHONPATH=$(ORTHOTRACK) $(PYTHON) $(ORTHOTRACK)/scripts/run_tracking.py \
		--sequence_dir data/MovingDrone/scenes/airport10 \
		--fine_matcher $(FINE_MATCHER) \
		--output outputs/smoke_airport10 \
		--end_frame 30 \
		--save_keyframe_vis
	$(PYTHON) scripts/export_trajectory.py \
		--results outputs/smoke_airport10/results.json \
		--sequence-dir data/MovingDrone/scenes/airport10 \
		--video data/MovingDrone/scenes/airport10/video.mp4 \
		--out outputs/smoke_airport10/trajectory.json

fetch-map: ## Fetch SWISSIMAGE DOP + swissSURFACE3D DSM tiles (LAT, LON, RADIUS, MAP_DIR)
	$(PYTHON) scripts/fetch_swisstopo.py \
		--lat $(LAT) \
		--lon $(LON) \
		--radius-m $(RADIUS) \
		--out $(MAP_DIR)

track: ## Run OrthoTrack on custom video + map dir (VIDEO=..., MAP_DIR=..., OUT=...)
	@test -n "$(VIDEO)" || (echo "Error: pass VIDEO=path/to/video.mp4" && exit 1)
	@MAP_JSON=$$($(PYTHON) scripts/prepare_map.py --map-dir $(MAP_DIR) --radius-m $(RADIUS) | tail -n 15); \
	DOP_FILE=$$(echo "$$MAP_JSON" | $(PYTHON) -c "import sys,json; s=sys.stdin.read(); print(json.loads(s[s.index('{'):])['dop_path'])"); \
	DSM_FILE=$$(echo "$$MAP_JSON" | $(PYTHON) -c "import sys,json; s=sys.stdin.read(); print(json.loads(s[s.index('{'):])['dsm_path'])"); \
	PYTHONPATH=$(ORTHOTRACK) $(PYTHON) $(ORTHOTRACK)/scripts/run_tracking.py \
		--footage $(VIDEO) \
		--dop "$$DOP_FILE" \
		--dsm "$$DSM_FILE" \
		--output $(OUT) \
		--fine_matcher $(FINE_MATCHER) \
		--save_keyframe_vis \
		$(EXTRA_ARGS)
	$(PYTHON) scripts/export_trajectory.py \
		--results $(OUT)/results.json \
		--map-dir $(MAP_DIR) \
		--video $(VIDEO) \
		--out $(OUT)/trajectory.json

export: ## Convert OrthoTrack results.json -> WGS84 trajectory.json (OUT=..., MAP_DIR=...)
	$(PYTHON) scripts/export_trajectory.py \
		--results $(OUT)/results.json \
		--map-dir $(MAP_DIR) \
		$(if $(VIDEO),--video $(VIDEO),) \
		--out $(OUT)/trajectory.json

serve: ## Launch Groundframe Web Studio (FastAPI + CesiumJS) on http://localhost:8000
	$(PYTHON) -m uvicorn app.server:app --host $${HOST:-0.0.0.0} --port $${PORT:-8000}

clean: ## Remove generated outputs (keeps downloaded maps and .venv)
	rm -rf outputs/
