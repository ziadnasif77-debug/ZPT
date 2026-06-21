"""LangGraph state definition — the data contract between all graph nodes."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class AutodevState(TypedDict, total=False):
    user_request: str
    workspace_path: str

    product_spec: dict | None

    plan: dict | None
    plan_approved: bool

    code_bundle: dict | None
    test_result: dict | None
    debug_report: dict | None
    review: dict | None
    judge_decision: dict | None

    iteration: int
    attempt_history: Annotated[list[dict], operator.add]
    error_hashes: Annotated[list[str], operator.add]
    error_graph_context: str

    memory_context: str
    healer_fixes: Annotated[list[str], operator.add]
    inner_iterations: int
    package_report: dict | None

    retry_target: str
    final_status: str
    stop_reason: str
    feedback: str
    modified_files: list[str]
