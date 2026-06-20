"""Product Manager agent — interprets vague requests into structured specs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from autodev.schemas import ProductSpec

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "product_manager.md"


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    system_prompt = load_prompt()
    user_request = state["user_request"]

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_request},
    ]

    spec = llm.chat(agent="product_manager", messages=messages, response_model=ProductSpec)

    return {
        "product_spec": spec.model_dump(),
    }
