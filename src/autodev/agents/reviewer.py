"""Reviewer agent — reviews code and test results, approves or rejects with feedback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from autodev.schemas import Review

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "reviewer.md"


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    system_prompt = load_prompt()
    plan = state.get("plan") or {}
    code_bundle = state.get("code_bundle") or {}
    test_result = state.get("test_result") or {}

    files_desc = ""
    for f in code_bundle.get("files", []):
        files_desc += f"### {f['path']}\n```python\n{f['content']}\n```\n\n"

    test_status = "PASSED" if test_result.get("passed") else "FAILED"
    test_desc = (
        f"**Status**: {test_status}\n"
        f"**Exit Code**: {test_result.get('exit_code', -1)}\n"
        f"**stdout**:\n```\n{test_result.get('stdout', '')[:2000]}\n```\n"
        f"**stderr**:\n```\n{test_result.get('stderr', '')[:2000]}\n```"
    )

    criteria = "\n".join(f"- {c}" for c in plan.get("acceptance_criteria", []))

    user_msg = (
        f"## Plan Summary\n{plan.get('problem_description', 'N/A')}\n\n"
        f"## Acceptance Criteria\n{criteria}\n\n"
        f"## Code\n{files_desc}\n"
        f"## Test Results\n{test_desc}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    tests_failed = not test_result.get("passed", False)

    review = llm.chat(agent="reviewer", messages=messages, response_model=Review)

    if tests_failed:
        review.approved = False

    return {
        "review": review.model_dump(),
    }
