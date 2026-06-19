"""Reads model configuration from the autodev config.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

_CONFIG_CANDIDATES = [
    Path(__file__).resolve().parent.parent / "config.yaml",
    Path(__file__).resolve().parent / "config.yaml",
]


def _find_config() -> Path | None:
    for p in _CONFIG_CANDIDATES:
        if p.exists():
            return p
    return None


def get_default_model() -> str:
    cfg_path = _find_config()
    if cfg_path is None:
        return "qwen3-coder:latest"
    raw = yaml.safe_load(cfg_path.read_text())
    return raw.get("models", {}).get("default", "qwen3-coder:latest")


def get_ollama_base_url() -> str:
    cfg_path = _find_config()
    if cfg_path is None:
        return "http://localhost:11434"
    raw = yaml.safe_load(cfg_path.read_text())
    base = raw.get("llm", {}).get("base_url", "http://localhost:11434/v1")
    return base.replace("/v1", "").rstrip("/")
