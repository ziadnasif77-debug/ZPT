"""Developer agent — writes code according to the Architect's plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from autodev.schemas import CodeBundle

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "developer.md"


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    system_prompt = load_prompt()
    plan = state.get("plan") or {}

    user_parts = [f"## Plan\n```json\n{json.dumps(plan, indent=2)}\n```"]

    feedback = state.get("feedback", "")
    if feedback:
        user_parts.append(f"## Feedback from Previous Attempt\n{feedback}")

    attempt_history = state.get("attempt_history", [])
    if attempt_history and config.loop.pass_full_attempt_history:
        history_summary = []
        for attempt in attempt_history:
            entry = f"### Attempt {attempt.get('iteration', '?')}\n"
            if attempt.get("error_hash"):
                tr = attempt.get("test_result", {})
                entry += f"- Error: {tr.get('stderr', 'N/A')[:500]}\n"
            if attempt.get("review"):
                entry += f"- Reviewer: {attempt['review'].get('summary', 'N/A')}\n"
            history_summary.append(entry)
        user_parts.append(f"## Previous Attempts\n{''.join(history_summary)}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]

    code_bundle = llm.chat(agent="developer", messages=messages, response_model=CodeBundle)

    workspace = Path(state.get("workspace_path", "./workspace"))
    workspace.mkdir(parents=True, exist_ok=True)
    for f in code_bundle.files:
        fp = workspace / f.path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(f.content, encoding="utf-8")

    return {
        "code_bundle": code_bundle.model_dump(),
    }
