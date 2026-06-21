"""Debugger agent — focused root cause analysis after test failures."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from autodev.schemas import DebugReport

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "debugger.md"


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    system_prompt = load_prompt()
    test_result = state.get("test_result") or {}
    code_bundle = state.get("code_bundle") or {}

    files_desc = ""
    for f in code_bundle.get("files", []):
        files_desc += f"### {f['path']}\n```python\n{f['content']}\n```\n\n"

    stderr = test_result.get("stderr", "")
    stdout = test_result.get("stdout", "")

    user_msg = (
        f"## Test Result\n"
        f"**Passed**: {test_result.get('passed', False)}\n"
        f"**Exit Code**: {test_result.get('exit_code', -1)}\n\n"
        f"## stderr\n```\n{stderr[:2000]}\n```\n\n"
        f"## stdout\n```\n{stdout[:1000]}\n```\n\n"
        f"## Code\n{files_desc}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    report = llm.chat(agent="debugger", messages=messages, response_model=DebugReport)

    return {
        "debug_report": report.model_dump(),
    }
