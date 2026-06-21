"""Developer agent — writes code according to the Architect's plan.

On first attempt: writes complete files.
On retries: preserves unchanged files, only overwrites modified ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from autodev.patcher import apply_code_preserving
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
    iteration = state.get("iteration", 0)
    is_retry = iteration > 0

    user_parts = [f"## Plan\n```json\n{json.dumps(plan, indent=2)}\n```"]

    feedback = state.get("feedback", "")
    if feedback:
        user_parts.append(f"## Feedback from Previous Attempt\n{feedback}")

    error_graph_ctx = state.get("error_graph_context", "")
    if error_graph_ctx:
        user_parts.append(f"## Error History\n{error_graph_ctx}")

    memory_ctx = state.get("memory_context", "")
    if memory_ctx:
        user_parts.append(memory_ctx)

    if is_retry:
        workspace = Path(state.get("workspace_path", "./workspace"))
        existing_code = _read_current_code(workspace, plan)
        if existing_code:
            user_parts.append(
                "## Current Code (MODIFY only what needs fixing — do NOT rewrite working code)\n"
                + existing_code
            )

    attempt_history = state.get("attempt_history", [])
    if attempt_history and config.loop.pass_full_attempt_history:
        history_summary = []
        for attempt in attempt_history[-3:]:
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

    files_data = [f.model_dump() for f in code_bundle.files]
    modified = apply_code_preserving(workspace, files_data, is_retry)

    return {
        "code_bundle": code_bundle.model_dump(),
        "modified_files": modified,
    }


def _read_current_code(workspace: Path, plan: dict) -> str:
    """Read current files from workspace to include in retry prompt."""
    if not workspace.is_dir():
        return ""
    parts = []
    for py_file in workspace.glob("*.py"):
        if py_file.name == "test_runner.py":
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            parts.append(f"### {py_file.name}\n```python\n{content}\n```")
        except Exception:
            pass
    return "\n\n".join(parts)
