"""Configuration loader — all settings from config.yaml, no hardcoded model names."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    timeout_seconds: int = 300
    cache_enabled: bool = True


class ModelsConfig(BaseModel):
    default: str


class AgentModelsConfig(BaseModel):
    architect: str = "default"
    developer: str = "default"
    tester: str = "default"
    reviewer: str = "default"


class ContextConfig(BaseModel):
    max_tokens_per_model: dict[str, int] = Field(default_factory=dict)
    relevant_files_only: bool = True
    summarize_when_over_budget: bool = True


class SandboxConfig(BaseModel):
    backend: str = "docker"
    image: str = "python:3.11-slim"
    cpu_limit: str = "1.0"
    memory_limit: str = "512m"
    timeout_seconds: int = 30
    network: bool = False
    workspace_mount_readonly: bool = False


class LoopConfig(BaseModel):
    max_iterations: int = 5
    stop_if_no_progress: bool = True
    pass_full_attempt_history: bool = True


class HITLConfig(BaseModel):
    approve_plan: bool = True


class ObservabilityConfig(BaseModel):
    log_dir: str = "./logs"
    log_every_call: bool = True


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    models: ModelsConfig
    agent_models: AgentModelsConfig = Field(default_factory=AgentModelsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    human_in_the_loop: HITLConfig = Field(default_factory=HITLConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    workspace_dir: str = "./workspace"

    @model_validator(mode="after")
    def _resolve_agent_defaults(self) -> "AppConfig":
        for field in ("architect", "developer", "tester", "reviewer"):
            if getattr(self.agent_models, field) == "default":
                setattr(self.agent_models, field, self.models.default)
        return self

    def resolve_model(self, agent: str) -> str:
        return getattr(self.agent_models, agent, self.models.default)

    def get_token_budget(self, model: str) -> int:
        return self.context.max_tokens_per_model.get(model, 32000)


def _find_config_path() -> Path:
    env = os.environ.get("AUTODEV_CONFIG")
    if env:
        return Path(env)
    candidates = [Path("config.yaml"), Path(__file__).resolve().parents[2] / "config.yaml"]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("config.yaml not found. Set AUTODEV_CONFIG or run from project root.")


def load_config(path: Path | str | None = None) -> AppConfig:
    if path is None:
        path = _find_config_path()
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(raw)
