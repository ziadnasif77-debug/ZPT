"""Judge agent — decides ACCEPT/REJECT/ROLLBACK/ESCALATE based on full context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from autodev.schemas import JudgeDecision

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "judge.md"


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    system_prompt = load_prompt()
    test_result = state.get("test_result") or {}
    review = state.get("review") or {}
    debug_report = state.get("debug_report") or {}
    iteration = state.get("iteration", 0)
    error_graph_stats = state.get("error_graph_context", "")

    user_msg = (
        f"## Iteration: {iteration}\n\n"
        f"## Test Result\n"
        f"- Passed: {test_result.get('passed', False)}\n"
        f"- Exit Code: {test_result.get('exit_code', -1)}\n"
        f"- stderr: {(test_result.get('stderr', '') or '')[:500]}\n\n"
        f"## Reviewer Decision\n"
        f"- Approved: {review.get('approved', False)}\n"
        f"- Summary: {review.get('summary', 'N/A')}\n\n"
        f"## Debug Report\n"
        f"- Root Cause: {debug_report.get('root_cause', 'N/A')}\n"
        f"- Affected Files: {debug_report.get('affected_files', [])}\n"
        f"- Category: {debug_report.get('error_category', 'unknown')}\n\n"
        f"## Error History\n{error_graph_stats or 'No previous errors.'}\n"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    decision = llm.chat(agent="judge", messages=messages, response_model=JudgeDecision)

    return {
        "judge_decision": decision.model_dump(),
    }
