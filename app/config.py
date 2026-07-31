from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    event_file: Path = PROJECT_ROOT / "config" / "event.yaml"
    project_catalog_file: Path = PROJECT_ROOT / "config" / "project_catalog.json"
    vip_file: Path = PROJECT_ROOT / "config" / "vips.yaml"
    serial_port: str = os.getenv("ROBOT_SERIAL_PORT", "COM5")
    serial_baud: int = int(os.getenv("ROBOT_SERIAL_BAUD", "115200"))
    camera_source: str = os.getenv(
        "CAMERA_SOURCE", os.getenv("CAMERA_INDEX", "0")
    ).strip()
    camera_width: int = int(os.getenv("CAMERA_WIDTH", "1280"))
    camera_height: int = int(os.getenv("CAMERA_HEIGHT", "720"))
    camera_fps: int = int(os.getenv("CAMERA_FPS", "30"))
    vision_backend: str = os.getenv("VISION_BACKEND", "deep").strip().lower()
    insightface_model: str = os.getenv("INSIGHTFACE_MODEL", "buffalo_s").strip()
    vip_similarity_threshold: float = float(
        os.getenv("VIP_SIMILARITY_THRESHOLD", "0.50")
    )
    microphone_device: str | None = os.getenv("MICROPHONE_DEVICE") or None
    remote_host: str = os.getenv("REMOTE_HOST", "0.0.0.0")
    remote_port: int = int(os.getenv("REMOTE_PORT", "8000"))
    remote_control_pin: str = os.getenv("REMOTE_CONTROL_PIN", "2468")
    simulation: bool = _bool_env("ROBOT_SIMULATION", True)
    sarvam_api_key: str | None = os.getenv("SARVAM_API_KEY")
    llm_provider: str = os.getenv("LLM_PROVIDER", "local_first").strip().lower()
    ollama_base_url: str = os.getenv(
        "OLLAMA_BASE_URL", "http://127.0.0.1:11434"
    ).strip()
    ollama_model: str = os.getenv(
        "OLLAMA_MODEL", "llama3.2:3b-instruct-q4_K_M"
    ).strip()
    ollama_local_timeout_seconds: float = float(
        os.getenv("OLLAMA_LOCAL_TIMEOUT_SECONDS", "8")
    )
    ollama_context_tokens: int = int(os.getenv("OLLAMA_CONTEXT_TOKENS", "8192"))
    llm_max_output_tokens: int = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "120"))
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m").strip()


settings = Settings()
