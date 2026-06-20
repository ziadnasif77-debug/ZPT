"""LangGraph orchestration — connects all agents with loop, anti-stuck, and HITL."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from autodev.agents import architect, developer, reviewer, tester
from autodev.context_manager import ContextManager
from autodev.deps import audit_dependencies, check_imports, pip_install_command, resolve_packages, scan_workspace
from autodev.diagnostics import check_api_mismatch, diagnose_traceback
from autodev.import_fixer import fix_imports
from autodev.schemas import AttemptRecord
from autodev.state import AutodevState
from autodev.syntax_fixer import fix_syntax

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.sandbox import Sandbox


def _error_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _auto_fix_code(workspace: Path) -> None:
    """Scan all .py files in workspace, fix syntax errors and missing imports."""
    if not workspace.is_dir():
        return
    for py_file in workspace.glob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            fixed = source

            fixed = fix_syntax(fixed)
            if fixed != source:
                print(f"[SYNTAX-FIX] Auto-fixed syntax in {py_file.name}", flush=True)

            after_imports = fix_imports(fixed)
            if after_imports != fixed:
                fixed = after_imports
                print(f"[IMPORT-FIX] Auto-fixed imports in {py_file.name}", flush=True)

            if fixed != source:
                py_file.write_text(fixed, encoding="utf-8")
        except Exception as exc:
            print(f"[CODE-FIX] Error fixing {py_file.name}: {exc}", flush=True)


def _read_py_files(workspace: Path) -> dict[str, str]:
    """Read all .py files in workspace into {filename: source}."""
    files: dict[str, str] = {}
    if not workspace.is_dir():
        return files
    for py_file in workspace.glob("*.py"):
        try:
            files[py_file.name] = py_file.read_text(encoding="utf-8")
        except Exception:
            pass
    return files


def _preflight_validate(workspace: Path) -> dict | None:
    """Statically validate code before the sandbox runs.

    Returns a synthetic failing TestResult dict if a problem is found that the
    sandbox would only rediscover at runtime, or None if everything looks sane.
    This saves a full sandbox round-trip and yields precise feedback.
    """
    import ast as _ast

    files = _read_py_files(workspace)
    if not files:
        return None

    # 1. Syntax must be valid in every file (syntax_fixer ran already).
    for name, source in files.items():
        try:
            _ast.parse(source)
        except SyntaxError as exc:
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f'File "{name}", line {exc.lineno}\nSyntaxError: {exc.msg}',
                "test_summary": "pre-flight: syntax error",
                "duration_seconds": 0.0,
            }

    # 2. The test must only import names the code actually defines.
    test_source = files.get("test_runner.py", "")
    code_files = {n: s for n, s in files.items() if n != "test_runner.py"}
    if test_source and code_files:
        mismatches = check_api_mismatch(code_files, test_source)
        if mismatches:
            return {
                "passed": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "API mismatch between code and test:\n" + "\n".join(mismatches),
                "test_summary": "pre-flight: API mismatch",
                "duration_seconds": 0.0,
            }

    return None


def build_graph(
    config: "AppConfig",
    llm: "LLMClient",
    sandbox: "Sandbox | None",
    context_mgr: "ContextManager",
) -> Any:
    graph = StateGraph(AutodevState)

    # ── Architect ──────────────────────────────────────────────
    def architect_node(state: AutodevState) -> dict:
        return architect.run(state, config, llm)

    # ── Human approval gate ────────────────────────────────────
    def approval_gate(state: AutodevState) -> dict:
        if not config.human_in_the_loop.approve_plan:
            return {"plan_approved": True}
        decision = interrupt({"plan": state.get("plan"), "type": "plan_approval"})
        approved = str(decision).strip().lower() in ("yes", "y", "approve", "true", "1")
        if not approved:
            return {
                "plan_approved": False,
                "final_status": "stopped",
                "stop_reason": "Plan rejected by user",
            }
        return {"plan_approved": True}

    # ── Developer ──────────────────────────────────────────────
    def developer_node(state: AutodevState) -> dict:
        result = developer.run(state, config, llm)
        workspace = Path(state.get("workspace_path", "./workspace"))
        _auto_fix_code(workspace)
        return result

    # ── Tester ─────────────────────────────────────────────────
    def tester_node(state: AutodevState) -> dict:
        tester_result = tester.run(state, config, llm)

        workspace = Path(state.get("workspace_path", "./workspace"))
        _auto_fix_code(workspace)

        # Pre-flight static validation — catch errors WITHOUT a sandbox run.
        preflight = _preflight_validate(workspace)
        if preflight is not None:
            print(f"[PREFLIGHT] {preflight['stderr'][:200]}", flush=True)
            return {"test_result": preflight}

        if sandbox is None:
            return tester_result
        audit = audit_dependencies(workspace)
        print(f"[DEPS] builtin (skip): {audit['builtin']}", flush=True)
        print(f"[DEPS] preinstalled (skip): {audit['preinstalled']}", flush=True)
        print(f"[DEPS] local (skip): {audit['local']}", flush=True)
        print(f"[DEPS] missing: {audit['missing']}", flush=True)

        imports = scan_workspace(workspace)
        plan = state.get("plan") or {}
        declared = plan.get("dependencies", [])
        packages = resolve_packages(imports, declared, workspace)
        setup_cmd = pip_install_command(packages)

        if packages and not sandbox._config.network:
            print(f"[DEPS] WARNING: need {packages} but sandbox has no network, skipping pip install", flush=True)
            setup_cmd = None

        sandbox_result = sandbox.run(
            workspace=workspace,
            command="python test_runner.py",
            extra_setup=setup_cmd,
        )
        return {"test_result": sandbox_result.model_dump()}

    # ── Reviewer ───────────────────────────────────────────────
    def reviewer_node(state: AutodevState) -> dict:
        return reviewer.run(state, config, llm)

    # ── Record attempt + prepare feedback for retry ────────────
    def prepare_retry(state: AutodevState) -> dict:
        test_result = state.get("test_result") or {}
        review_data = state.get("review") or {}
        code_bundle = state.get("code_bundle")
        iteration = state.get("iteration", 0)

        feedback_parts: list[str] = []
        workspace = Path(state.get("workspace_path", "./workspace"))
        code_files = _read_py_files(workspace)

        if not test_result.get("passed", False):
            stderr = test_result.get("stderr", "")
            stdout = test_result.get("stdout", "")

            # Deterministic diagnosis — trust the actual traceback over guesses.
            diag = diagnose_traceback(stderr, stdout, code_files)
            if diag.confident and diag.message:
                print(f"[DIAGNOSE] {diag.error_type} -> {diag.culprit}: {diag.message[:120]}", flush=True)
                feedback_parts.append(f"DIAGNOSIS ({diag.error_type}): {diag.message}")
                if diag.culprit == "tester":
                    feedback_parts.append(
                        "NOTE: This error is in the TEST script (test_runner.py), which the "
                        "Tester regenerates. The implementation code may be correct — focus "
                        "on making the implementation robust and well-structured."
                    )

            feedback_parts.append(f"Test FAILED (exit code {test_result.get('exit_code', -1)})")
            if stderr:
                feedback_parts.append(f"stderr:\n{stderr[:1500]}")
            if stdout:
                feedback_parts.append(f"stdout:\n{stdout[:500]}")

        comments = review_data.get("comments", [])
        if comments:
            feedback_parts.append("Reviewer comments:")
            for c in comments:
                sev = c.get("severity", "info")
                msg = c.get("message", "")
                fp = c.get("file_path", "")
                feedback_parts.append(f"  [{sev}] {fp}: {msg}")

        if review_data.get("summary"):
            feedback_parts.append(f"Reviewer summary: {review_data['summary']}")

        error_text = test_result.get("stderr", "") + review_data.get("summary", "")
        eh = _error_hash(error_text)

        attempt = AttemptRecord(
            iteration=iteration,
            code_bundle=state.get("code_bundle"),
            test_result=test_result if test_result else None,
            review=review_data if review_data else None,
            error_hash=eh,
        )

        return {
            "feedback": "\n".join(feedback_parts),
            "iteration": iteration + 1,
            "error_hashes": [eh],
            "attempt_history": [attempt.model_dump()],
        }

    # ── Done node ──────────────────────────────────────────────
    def done_node(state: AutodevState) -> dict:
        return {"final_status": "success", "stop_reason": "Approved by reviewer"}

    # ── Failed node ────────────────────────────────────────────
    def failed_node(state: AutodevState) -> dict:
        iteration = state.get("iteration", 0)
        max_iter = config.loop.max_iterations
        hashes = state.get("error_hashes", [])

        test_result = state.get("test_result") or {}
        review_data = state.get("review") or {}
        current_error = test_result.get("stderr", "") + review_data.get("summary", "")
        current_hash = _error_hash(current_error)
        is_stuck = current_hash in hashes or len(hashes) != len(set(hashes))

        if not state.get("plan_approved", True):
            reason = state.get("stop_reason", "Plan rejected by user")
        elif is_stuck:
            reason = (
                f"Stopped: same error repeated (no progress). "
                f"Completed {iteration} iteration(s)."
            )
        else:
            reason = (
                f"Stopped: reached max iterations ({max_iter}). "
                f"The code may still have issues."
            )
        return {"final_status": "failed", "stop_reason": reason}

    # ── Routing logic ──────────────────────────────────────────
    def route_after_approval(state: AutodevState) -> str:
        if state.get("plan_approved"):
            return "developer"
        return "failed"

    def route_after_review(state: AutodevState) -> str:
        review_data = state.get("review") or {}
        test_result = state.get("test_result") or {}

        if review_data.get("approved") and test_result.get("passed", False):
            return "done"

        iteration = state.get("iteration", 0)
        if iteration >= config.loop.max_iterations:
            return "failed"

        if config.loop.stop_if_no_progress:
            error_text = test_result.get("stderr", "") + review_data.get("summary", "")
            eh = _error_hash(error_text)
            if eh in state.get("error_hashes", []):
                return "failed"

        return "retry"

    # ── Build the graph ────────────────────────────────────────
    graph.add_node("architect", architect_node)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("developer", developer_node)
    graph.add_node("tester", tester_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("prepare_retry", prepare_retry)
    graph.add_node("done", done_node)
    graph.add_node("failed", failed_node)

    graph.add_edge(START, "architect")
    graph.add_edge("architect", "approval_gate")
    graph.add_conditional_edges("approval_gate", route_after_approval)
    graph.add_edge("developer", "tester")
    graph.add_edge("tester", "reviewer")
    graph.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"done": "done", "failed": "failed", "retry": "prepare_retry"},
    )
    graph.add_edge("prepare_retry", "developer")
    graph.add_edge("done", END)
    graph.add_edge("failed", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)
