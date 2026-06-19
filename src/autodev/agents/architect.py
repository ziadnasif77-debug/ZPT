"""Architect agent — analyzes user request and produces a structured plan."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from autodev.schemas import Plan

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "architect.md"


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    system_prompt = load_prompt()
    user_request = state["user_request"]

    context_parts = [f"## User Request\n{user_request}"]

    workspace = Path(state.get("workspace_path", "./workspace"))
    if workspace.exists():
        existing = [str(p.relative_to(workspace)) for p in workspace.rglob("*.py")]
        if existing:
            context_parts.append(f"## Existing Files in Workspace\n{', '.join(existing)}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(context_parts)},
    ]

    plan = llm.chat(agent="architect", messages=messages, response_model=Plan)

    return {
        "plan": plan.model_dump(),
    }
