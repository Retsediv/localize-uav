"""Runtime configuration, .env loading, and in-memory session overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
MAPS_DIR = DATA_DIR / "maps"
VIDEOS_DIR = DATA_DIR / "videos"
UPLOADS_DIR = DATA_DIR / "uploads"
MOVINGDRONE_DIR = DATA_DIR / "MovingDrone" / "scenes"
OUTPUTS_DIR = ROOT_DIR / "outputs"
ORTHOTRACK_DIR = ROOT_DIR / "third_party" / "orthotrack"
SCRIPTS_DIR = ROOT_DIR / "scripts"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TORCH_HOME_DIR = ROOT_DIR / ".cache" / "torch"

# Load .env once at startup without clobbering real shell env vars
load_dotenv(ROOT_DIR / ".env", override=False)


def _mask_secret(value: str) -> str:
    val = (value or "").strip()
    if not val:
        return ""
    if len(val) <= 8:
        return "••••"
    return f"{val[:4]}••••{val[-4:]}"


@dataclass
class RuntimeSettings:
    cesium_ion_token: str = ""
    google_maps_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-2.5-flash"

    def __post_init__(self) -> None:
        self.cesium_ion_token = os.getenv("CESIUM_ION_TOKEN", "").strip()
        self.google_maps_api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.openrouter_model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash").strip() or "google/gemini-2.5-flash"

    def update(self, payload: Dict[str, Any]) -> None:
        """Apply in-memory session overrides without modifying .env on disk."""
        for field_name in ("cesium_ion_token", "google_maps_api_key", "openrouter_api_key", "openrouter_model"):
            if field_name in payload and payload[field_name] is not None:
                val = str(payload[field_name]).strip()
                # Empty string clears override only if explicitly requested via clear_* flag;
                # otherwise ignore masked placeholders like 'sk-o••••1234'.
                if "••••" in val:
                    continue
                if val:
                    setattr(self, field_name, val)

    def public_client_config(self) -> Dict[str, Any]:
        """Config sent to the browser for CesiumJS initialization and feature flags."""
        vpr_dir = DATA_DIR / "vpr_index"
        avail_models = [
            m for m in ("sample4geo", "megaloc")
            if (vpr_dir / f"{m}_embeddings.npy").exists()
        ]
        active_model = None
        active_file = vpr_dir / "active_model.json"
        if active_file.exists():
            try:
                import json as _json
                active_model = _json.loads(active_file.read_text(encoding="utf-8")).get("model")
            except Exception:
                pass
        if not active_model and avail_models:
            active_model = avail_models[0]
        num_tiles = 0
        if active_model and (vpr_dir / f"{active_model}_info.json").exists():
            try:
                import json as _json
                num_tiles = int(_json.loads((vpr_dir / f"{active_model}_info.json").read_text(encoding="utf-8")).get("num_tiles", 0))
            except Exception:
                pass
        return {
            "cesium_ion_token": self.cesium_ion_token,
            "google_maps_api_key": self.google_maps_api_key,
            "openrouter_configured": bool(self.openrouter_api_key),
            "openrouter_model": self.openrouter_model,
            "gpu_name": detect_gpu_name(),
            "vpr_ready": bool(avail_models),
            "vpr_active_model": active_model,
            "vpr_available_models": avail_models,
            "vpr_num_tiles": num_tiles,
        }

    def masked_settings(self) -> Dict[str, Any]:
        """Masked view of credentials for GET /api/settings."""
        return {
            "cesium_ion_token_set": bool(self.cesium_ion_token),
            "cesium_ion_token_masked": _mask_secret(self.cesium_ion_token),
            "google_maps_api_key_set": bool(self.google_maps_api_key),
            "google_maps_api_key_masked": _mask_secret(self.google_maps_api_key),
            "openrouter_api_key_set": bool(self.openrouter_api_key),
            "openrouter_api_key_masked": _mask_secret(self.openrouter_api_key),
            "openrouter_model": self.openrouter_model,
        }


_cached_gpu_name: Optional[str] = None


def detect_gpu_name() -> str:
    global _cached_gpu_name
    if _cached_gpu_name is not None:
        return _cached_gpu_name
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        _cached_gpu_name = out.splitlines()[0] if out else "CPU"
    except Exception:
        _cached_gpu_name = "CPU"
    return _cached_gpu_name


SETTINGS = RuntimeSettings()
