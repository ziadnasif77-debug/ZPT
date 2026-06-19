"""Tester agent — generates test script from acceptance criteria, runs code in sandbox."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from autodev.schemas import CodeFile, TestResult

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "tester.md"


class TestPlan(BaseModel):
    test_file: CodeFile
    test_command: str = "python test_runner.py"
    summary: str = ""


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    plan = state.get("plan") or {}
    code_bundle = state.get("code_bundle") or {}
    acceptance_criteria = plan.get("acceptance_criteria", [])

    system_prompt = load_prompt()

    files_desc = ""
    for f in code_bundle.get("files", []):
        files_desc += f"### {f['path']}\n```python\n{f['content']}\n```\n\n"

    criteria_text = "\n".join(f"- {c}" for c in acceptance_criteria)

    user_msg = (
        f"## Code Under Test\n{files_desc}\n"
        f"## Acceptance Criteria\n{criteria_text}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    test_plan = llm.chat(agent="tester", messages=messages, response_model=TestPlan)

    workspace = Path(state.get("workspace_path", "./workspace"))
    test_path = workspace / test_plan.test_file.path
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(test_plan.test_file.content, encoding="utf-8")

    return {
        "test_result": _placeholder_result(test_plan),
    }


def _placeholder_result(test_plan: TestPlan) -> dict:
    """Return a pending TestResult — sandbox.py will replace this with real execution."""
    return TestResult(
        passed=False,
        exit_code=-1,
        stdout="",
        stderr="awaiting sandbox execution",
        test_summary=test_plan.summary,
    ).model_dump()
