"""LangGraph state definition — the data contract between all graph nodes."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class AutodevState(TypedDict, total=False):
    user_request: str
    workspace_path: str

    plan: dict | None
    plan_approved: bool

    code_bundle: dict | None
    test_result: dict | None
    review: dict | None

    iteration: int
    attempt_history: Annotated[list[dict], operator.add]
    error_hashes: Annotated[list[str], operator.add]

    final_status: str
    stop_reason: str
    feedback: str
