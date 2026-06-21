"""LangGraph orchestration — pipeline with memory, healer, git, error graph, and escalation.

Flow: Memory Recall → Product Manager → Architect → [approval] → Developer →
      Healer → Tester → [if failed] Debugger → Reviewer → Judge →
      [ACCEPT] → Memory Save → Done
      [REJECT] → Prepare Retry → Developer
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from autodev.agents import architect, debugger, developer, judge, product_manager, reviewer, tester
from autodev.code_healer import extract_test_failures, format_test_failures, heal_workspace
from autodev.context_manager import ContextManager
from autodev.deps import audit_dependencies, check_imports, pip_install_command, resolve_packages, scan_workspace
from autodev.diagnostics import _last_file_in_traceback, check_api_mismatch, diagnose_traceback
from autodev.error_graph import ErrorGraph
from autodev.git_manager import commit_snapshot, rollback_to_last_success, tag_success
from autodev.package_manager import AutonomousPackageManager
from autodev.memory import (
    LessonRecord,
    Memory,
    MemoryConfig,
    SolutionRecord,
    format_memory_context,
)
from autodev.schemas import AttemptRecord
from autodev.state import AutodevState

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.sandbox import Sandbox


def _error_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _run_healer(workspace: Path) -> dict[str, list[str]]:
    """Run the deterministic code healer on all workspace files."""
    report = heal_workspace(workspace)
    for category, files in report.items():
        if files and category != "compile_errors":
            print(f"[HEALER-{category.upper()}] Fixed: {', '.join(files)}", flush=True)
    for err in report.get("compile_errors", []):
        print(f"[HEALER-COMPILE] {err}", flush=True)
    return report


_FORBIDDEN_IMPORT_REPLACEMENTS: dict[str, str] = {
    "pydantic": "dataclasses",
}


def _auto_replace_forbidden_imports(workspace: Path, missing: list[str]) -> bool:
    """Try to replace forbidden third-party imports with stdlib equivalents.

    Returns True if any file was modified.
    """
    import ast as _ast
    import re as _re

    fixed_any = False
    for py_file in workspace.glob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        original = source
        for mod in missing:
            if mod not in source:
                continue

            if mod == "pydantic":
                source = _re.sub(
                    r'^from\s+pydantic\s+import\s+.*$',
                    'from dataclasses import dataclass, field',
                    source,
                    flags=_re.MULTILINE,
                )
                source = _re.sub(
                    r'^import\s+pydantic\b.*$',
                    'from dataclasses import dataclass, field',
                    source,
                    flags=_re.MULTILINE,
                )
                source = source.replace('(BaseModel)', '')
                source = source.replace('BaseModel', 'object')
                source = _re.sub(
                    r'^(\s*class\s+\w+)(?:\(object\))?(\s*:)',
                    r'\1\2',
                    source,
                    flags=_re.MULTILINE,
                )
            else:
                replacement = _FORBIDDEN_IMPORT_REPLACEMENTS.get(mod)
                if replacement:
                    source = _re.sub(
                        rf'^(from\s+){_re.escape(mod)}(\s+import\s+)',
                        rf'\g<1>{replacement}\g<2>',
                        source,
                        flags=_re.MULTILINE,
                    )
                    source = _re.sub(
                        rf'^import\s+{_re.escape(mod)}\b.*$',
                        f'import {replacement}',
                        source,
                        flags=_re.MULTILINE,
                    )
                else:
                    source = _re.sub(
                        rf'^from\s+{_re.escape(mod)}\s+import\s+.*$',
                        f'# REMOVED: unavailable import {mod}',
                        source,
                        flags=_re.MULTILINE,
                    )
                    source = _re.sub(
                        rf'^import\s+{_re.escape(mod)}\b.*$',
                        f'# REMOVED: unavailable import {mod}',
                        source,
                        flags=_re.MULTILINE,
                    )

        if source != original:
            py_file.write_text(source, encoding="utf-8")
            print(f"[DEPS] Auto-replaced forbidden imports in {py_file.name}", flush=True)
            fixed_any = True

    return fixed_any


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
    """Statically validate code before the sandbox runs."""
    import ast as _ast

    files = _read_py_files(workspace)
    if not files:
        return None

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

    test_source = ""
    test_name = "test_runner.py"
    for fname in files:
        if fname == "test_runner.py" or fname.startswith("test_"):
            test_source = files[fname]
            test_name = fname
            break
    code_files = {n: s for n, s in files.items() if n != test_name}
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


def _classify_error_type(stderr: str) -> str:
    """Classify an error from stderr into a category."""
    text = stderr.lower()
    if "syntaxerror" in text or "syntax error" in text:
        return "syntax"
    if "importerror" in text or "modulenotfounderror" in text:
        return "import"
    if "nameerror" in text:
        return "name"
    if "typeerror" in text:
        return "type"
    if "attributeerror" in text:
        return "attribute"
    if "assertionerror" in text or "assert" in text:
        return "logic"
    if "indexerror" in text:
        return "index"
    if "keyerror" in text:
        return "key"
    return "runtime"


def _extract_avoid_pattern(stderr: str) -> str:
    """Extract a concise 'avoid this' pattern from stderr."""
    import re
    m = re.search(r"(SyntaxError|ImportError|NameError|TypeError|AttributeError|ModuleNotFoundError):\s*(.+?)(?:\n|$)", stderr)
    if m:
        return f"Don't cause {m.group(1)}: {m.group(2).strip()[:150]}"
    if "getvalue" in stderr.lower():
        return "Don't use sys.stdout.getvalue() — use io.StringIO instead"
    if "unexpected keyword" in stderr.lower():
        return "Don't pass wrong keyword arguments — check method signatures match"
    lines = stderr.strip().split("\n")
    if lines:
        return f"Avoid: {lines[-1][:150]}"
    return "Unknown error pattern"


def build_graph(
    config: "AppConfig",
    llm: "LLMClient",
    sandbox: "Sandbox | None",
    context_mgr: "ContextManager",
) -> Any:
    graph = StateGraph(AutodevState)

    eg_path = config.error_graph.path if config.error_graph.enabled else None
    error_graph = ErrorGraph(persist_path=eg_path)

    mem_config = MemoryConfig(**config.memory.model_dump()) if config.memory.enabled else MemoryConfig(enabled=False)
    memory = Memory(mem_config)
    pkg_mgr = AutonomousPackageManager(config)

    # ── Memory Recall ─────────────────────────────────────────
    def memory_recall_node(state: AutodevState) -> dict:
        if not memory.is_available:
            return {"memory_context": ""}

        request = state.get("user_request", "")
        if not request:
            return {"memory_context": ""}

        solutions = memory.recall_solutions(request)
        lessons = memory.recall_lessons(request)

        if not solutions and not lessons:
            print("[MEMORY] No relevant memories found", flush=True)
            return {"memory_context": ""}

        context = format_memory_context(solutions, lessons)
        print(f"[MEMORY] Recalled {len(solutions)} solution(s), {len(lessons)} lesson(s)", flush=True)
        return {"memory_context": context}

    # ── Product Manager ───────────────────────────────────────
    def product_manager_node(state: AutodevState) -> dict:
        error_graph.reset()
        result = product_manager.run(state, config, llm)

        # If memory has past solutions for similar tasks, inject into spec
        mem_ctx = state.get("memory_context", "")
        if mem_ctx and result.get("product_spec"):
            spec = result["product_spec"]
            if not spec.get("notes"):
                spec["notes"] = ""
            spec["notes"] += f"\n\n{mem_ctx}"

        return result

    # ── Architect ──────────────────────────────────────────────
    def architect_node(state: AutodevState) -> dict:
        result = architect.run(state, config, llm)
        spec = state.get("product_spec")
        if spec and result.get("plan"):
            plan = result["plan"]
            if not plan.get("acceptance_criteria"):
                plan["acceptance_criteria"] = spec.get("success_criteria", [])
        return result

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

    # ── Developer (self-healing inner loop) ─────────────────────
    def developer_node(state: AutodevState) -> dict:
        workspace = Path(state.get("workspace_path", "./workspace"))

        plan = state.get("plan") or {}
        plan_files = plan.get("files_needed", [])
        iteration = state.get("iteration", 0)
        if iteration == 0 and plan_files and workspace.is_dir():
            allowed_basenames = {Path(p).name for p in plan_files}
            allowed_basenames.add("test_runner.py")
            for py_file in list(workspace.glob("*.py")):
                if py_file.name not in allowed_basenames:
                    py_file.unlink()
                    print(f"[SCOPE] Removed stale file: {py_file.name}", flush=True)

        # Package audit before code generation
        if config.packages.audit_before_run:
            audit = pkg_mgr.auto_resolve(workspace)
            pkg_report = {
                "skip": audit.skip,
                "ready": audit.ready,
                "install": audit.install,
                "local": audit.local,
            }
        else:
            pkg_report = None

        result = developer.run(state, config, llm)
        inner_iter = result.get("inner_iterations", 1)
        print(f"[DEVELOPER] Completed in {inner_iter} inner iteration(s)", flush=True)

        # Post-generation package audit (new imports may have appeared)
        if config.packages.audit_before_run:
            post_audit = pkg_mgr.auto_resolve(workspace)
            if post_audit.install:
                print(f"[PKG] Post-generation: still missing {post_audit.install}", flush=True)

        result["package_report"] = pkg_report
        return result

    # ── Healer (deterministic, zero LLM) ──────────────────────
    def healer_node(state: AutodevState) -> dict:
        workspace = Path(state.get("workspace_path", "./workspace"))
        report = _run_healer(workspace)

        if config.git.auto_commit:
            iteration = state.get("iteration", 0)
            sha = commit_snapshot(workspace, f"autodev: attempt {iteration}")
            if sha:
                print(f"[GIT] Committed attempt {iteration}: {sha[:8]}", flush=True)

        fix_descriptions: list[str] = []
        for category, files in report.items():
            if files and category != "compile_errors":
                fix_descriptions.append(f"{category}: {', '.join(files)}")

        total_fixes = len(fix_descriptions)
        if total_fixes > 0:
            print(f"[HEALER] Applied {total_fixes} fix(es) before testing", flush=True)

        return {"healer_fixes": fix_descriptions}

    # ── Tester ─────────────────────────────────────────────────
    def tester_node(state: AutodevState) -> dict:
        tester_result = tester.run(state, config, llm)

        workspace = Path(state.get("workspace_path", "./workspace"))

        test_file = "test_runner.py"
        for candidate in workspace.glob("test_*.py"):
            test_file = candidate.name
            break
        if (workspace / "test_runner.py").exists():
            test_file = "test_runner.py"

        _run_healer(workspace)

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

        if audit['missing']:
            fixed_any = _auto_replace_forbidden_imports(workspace, audit['missing'])
            if fixed_any:
                audit = audit_dependencies(workspace)
                print(f"[DEPS] After auto-fix, still missing: {audit['missing']}", flush=True)

            if audit['missing']:
                missing_str = ", ".join(audit['missing'])
                error_msg = (
                    f"ModuleNotFoundError: The following packages are not available "
                    f"in the sandbox: {missing_str}. "
                    f"Use only Python standard library modules. "
                    f"Replace pydantic with dataclasses, requests with urllib.request, etc."
                )
                print(f"[DEPS] BLOCKED: {error_msg}", flush=True)
                return {"test_result": {
                    "passed": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": error_msg,
                    "test_summary": f"Blocked: forbidden imports {missing_str}",
                }}

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
            command=f"python {test_file}",
            extra_setup=setup_cmd,
        )
        return {"test_result": sandbox_result.model_dump()}

    # ── Debugger ───────────────────────────────────────────────
    def debugger_node(state: AutodevState) -> dict:
        test_result = state.get("test_result") or {}
        if test_result.get("passed", False):
            return {"debug_report": {"root_cause": "N/A", "affected_files": [], "error_category": "none"}}

        workspace = Path(state.get("workspace_path", "./workspace"))
        code_files = _read_py_files(workspace)
        stderr = test_result.get("stderr", "")
        stdout = test_result.get("stdout", "")

        diag = diagnose_traceback(stderr, stdout, code_files)

        if diag.confident and diag.message:
            print(f"[DEBUGGER-DIAG] {diag.error_type} -> {diag.culprit}: {diag.message[:120]}", flush=True)
            traceback_file = _last_file_in_traceback(stderr)
            affected = [traceback_file] if traceback_file else []
            report = {
                "root_cause": diag.message,
                "affected_files": affected,
                "error_category": diag.error_type,
                "culprit": diag.culprit,
            }
        else:
            result = debugger.run(state, config, llm)
            report = result.get("debug_report", {})

        iteration = state.get("iteration", 0)
        if config.error_graph.enabled:
            node = error_graph.record_error(
                error_type=report.get("error_category", "unknown"),
                file=", ".join(report.get("affected_files", [])),
                root_cause=report.get("root_cause", ""),
                iteration=iteration,
                stderr=stderr[:500],
            )
            ctx = error_graph.get_error_context(node.signature)
            return {"debug_report": report, "error_graph_context": ctx}

        return {"debug_report": report}

    # ── Reviewer ───────────────────────────────────────────────
    def reviewer_node(state: AutodevState) -> dict:
        return reviewer.run(state, config, llm)

    # ── Judge ──────────────────────────────────────────────────
    def judge_node(state: AutodevState) -> dict:
        test_result = state.get("test_result") or {}
        review_data = state.get("review") or {}

        if review_data.get("approved") and test_result.get("passed", False):
            workspace = Path(state.get("workspace_path", "./workspace"))
            iteration = state.get("iteration", 0)
            if config.git.auto_commit:
                tag_success(workspace, iteration)
                print(f"[GIT] Tagged successful build at iteration {iteration}", flush=True)
            if config.error_graph.enabled:
                error_graph.reset()
            return {
                "judge_decision": {
                    "decision": "ACCEPT",
                    "reason": "All tests passed and reviewer approved",
                    "strategy": "",
                },
            }

        debug_report = state.get("debug_report") or {}
        error_ctx = state.get("error_graph_context", "")

        if config.error_graph.enabled and error_ctx:
            if "occurred 5+" in error_ctx or "ESCALATE" in error_ctx.upper():
                return {
                    "judge_decision": {
                        "decision": "ESCALATE",
                        "reason": "Same error repeated 5+ times — human intervention needed",
                        "strategy": "Review the error pattern and provide manual guidance",
                    },
                }
            if "occurred 4 TIMES" in error_ctx or "ROLLBACK" in error_ctx.upper():
                workspace = Path(state.get("workspace_path", "./workspace"))
                if config.git.rollback_on_repeated_error:
                    rolled_back = rollback_to_last_success(workspace)
                    if rolled_back:
                        return {
                            "judge_decision": {
                                "decision": "ROLLBACK",
                                "reason": "Same error repeated 4 times — rolled back to last stable version",
                                "strategy": "Try a completely different implementation approach",
                            },
                        }

        result = judge.run(state, config, llm)
        return result

    # ── Prepare retry (surgical healing + feedback assembly) ──
    def prepare_retry(state: AutodevState) -> dict:
        test_result = state.get("test_result") or {}
        review_data = state.get("review") or {}
        debug_report = state.get("debug_report") or {}
        iteration = state.get("iteration", 0)

        feedback_parts: list[str] = []
        workspace = Path(state.get("workspace_path", "./workspace"))
        code_files = _read_py_files(workspace)

        # Surgical healing: try to fix structural gaps before LLM retry
        if not test_result.get("passed", False):
            stderr_raw = test_result.get("stderr", "")
            stdout_raw = test_result.get("stdout", "")
            heal_result = developer.heal_until_passing(
                workspace, stderr_raw, stdout_raw, llm,
            )
            if heal_result.get("patches_applied"):
                patches = heal_result["patches_applied"]
                feedback_parts.append(
                    f"SURGICAL HEALER applied {len(patches)} patch(es):\n"
                    + "\n".join(f"  - {p}" for p in patches)
                )

        if debug_report.get("root_cause"):
            feedback_parts.append(f"ROOT CAUSE: {debug_report['root_cause']}")
            if debug_report.get("affected_files"):
                feedback_parts.append(f"AFFECTED FILES: {', '.join(debug_report['affected_files'])}")

        stderr = test_result.get("stderr", "").lower()
        root_cause = (debug_report.get("root_cause") or "").lower()
        combined_error = stderr + " " + root_cause
        if any(p in combined_error for p in [
            "not defined", "not accessible", "global", "not being reset",
            "shared state", "state leak", "list index out of range",
        ]):
            feedback_parts.append(
                "⚠️ ANTI-PATTERN DETECTED: Global mutable state.\n"
                "MANDATORY FIX: Wrap ALL state inside a CLASS.\n"
                "- Replace `tasks = []` at module level with `self.tasks = []` in __init__\n"
                "- ALL functions that access state must be CLASS METHODS using self.\n"
                "- Tests must create FRESH instances: `mgr = TaskManager()` per test\n"
                "- Do NOT use global variables for any mutable data."
            )
        if any(p in combined_error for p in [
            "getvalue", "textiowrapper", "has no attribute",
        ]):
            feedback_parts.append(
                "⚠️ ANTI-PATTERN DETECTED: Wrong output capture.\n"
                "Use io.StringIO + contextlib.redirect_stdout, NOT sys.stdout.getvalue().\n"
                "Pattern: f = io.StringIO(); with contextlib.redirect_stdout(f): func(); output = f.getvalue()"
            )

        import re as _re
        kwarg_match = _re.search(
            r"unexpected keyword argument ['\"](\w+)['\"]", combined_error
        )
        if kwarg_match:
            bad_kwarg = kwarg_match.group(1)
            feedback_parts.append(
                f"⚠️ ANTI-PATTERN DETECTED: Keyword argument mismatch.\n"
                f"The code passes '{bad_kwarg}=' but the method expects a DIFFERENT "
                f"parameter name. MANDATORY FIX:\n"
                f"1. Find the class __init__ or method definition\n"
                f"2. Check what the parameter is ACTUALLY named\n"
                f"3. Update ALL call sites to use the CORRECT name\n"
                f"4. Do NOT add '{bad_kwarg}' as a new parameter — use the existing name"
            )

        error_ctx = state.get("error_graph_context", "")
        if error_ctx:
            feedback_parts.append(error_ctx)

        if not test_result.get("passed", False):
            stderr = test_result.get("stderr", "")
            stdout = test_result.get("stdout", "")

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

            # Pytest healer: extract exact failing assertions
            test_failures = extract_test_failures(stderr, stdout)
            if test_failures:
                failure_report = format_test_failures(test_failures)
                feedback_parts.append(failure_report)

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

        judge_decision = state.get("judge_decision") or {}
        if judge_decision.get("strategy"):
            feedback_parts.append(f"JUDGE STRATEGY: {judge_decision['strategy']}")

        error_text = test_result.get("stderr", "") + review_data.get("summary", "")
        eh = _error_hash(error_text)

        attempt = AttemptRecord(
            iteration=iteration,
            code_bundle=state.get("code_bundle"),
            test_result=test_result if test_result else None,
            review=review_data if review_data else None,
            error_hash=eh,
        )

        retry_target = "developer"
        diag_culprit = debug_report.get("culprit", "")
        test_summary = test_result.get("test_summary", "")
        if diag_culprit == "tester" or "pre-flight: syntax" in test_summary:
            retry_target = "tester"

        return {
            "feedback": "\n".join(feedback_parts),
            "iteration": iteration + 1,
            "error_hashes": [eh],
            "attempt_history": [attempt.model_dump()],
            "retry_target": retry_target,
        }

    # ── Memory Save (after ACCEPT) ────────────────────────────
    def memory_save_node(state: AutodevState) -> dict:
        if not memory.is_available:
            return {}

        from datetime import datetime as _dt

        cb = state.get("code_bundle") or {}
        files = cb.get("files", [])

        record = SolutionRecord(
            task=state.get("user_request", ""),
            product_spec=state.get("product_spec"),
            plan=state.get("plan"),
            files=files,
            iterations_needed=state.get("iteration", 0),
            healer_fixes=state.get("healer_fixes", []),
            timestamp=_dt.now().isoformat(),
            model_used=config.models.default,
        )
        memory.save_solution(record)

        # Save lessons from any failed attempts in this run
        for attempt in state.get("attempt_history", []):
            tr = attempt.get("test_result") or {}
            rv = attempt.get("review") or {}
            if not tr.get("passed", True):
                stderr = tr.get("stderr", "")
                lesson = LessonRecord(
                    error_type=_classify_error_type(stderr),
                    what_went_wrong=stderr[:300] if stderr else rv.get("summary", "")[:300],
                    what_fixed_it="Fixed in subsequent iteration",
                    avoid_this=_extract_avoid_pattern(stderr),
                    task_context=state.get("user_request", ""),
                    timestamp=_dt.now().isoformat(),
                )
                memory.save_lesson(lesson)

        return {}

    # ── Done node ──────────────────────────────────────────────
    def done_node(state: AutodevState) -> dict:
        return {"final_status": "success", "stop_reason": "Approved by reviewer and judge"}

    # ── Failed node ────────────────────────────────────────────
    def failed_node(state: AutodevState) -> dict:
        iteration = state.get("iteration", 0)
        judge_decision = state.get("judge_decision") or {}
        decision = judge_decision.get("decision", "")

        if not state.get("plan_approved", True):
            reason = state.get("stop_reason", "Plan rejected by user")
        elif decision == "ESCALATE":
            reason = (
                f"Escalated to user: {judge_decision.get('reason', 'repeated errors')}. "
                f"Completed {iteration} iteration(s)."
            )
        elif decision == "ROLLBACK":
            reason = (
                f"Rolled back: {judge_decision.get('reason', 'repeated errors')}. "
                f"Completed {iteration} iteration(s)."
            )
        else:
            max_iter = config.loop.max_iterations
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

    def route_after_tester(state: AutodevState) -> str:
        test_result = state.get("test_result") or {}
        if test_result.get("passed", False):
            return "reviewer"
        return "debugger"

    def route_after_judge(state: AutodevState) -> str:
        judge_decision = state.get("judge_decision") or {}
        decision = judge_decision.get("decision", "REJECT").upper()

        if decision == "ACCEPT":
            return "done"

        if decision in ("ESCALATE", "ROLLBACK"):
            return "failed"

        iteration = state.get("iteration", 0)
        if iteration >= config.loop.max_iterations:
            return "failed"

        if config.loop.stop_if_no_progress:
            test_result = state.get("test_result") or {}
            review_data = state.get("review") or {}
            error_text = test_result.get("stderr", "") + review_data.get("summary", "")
            eh = _error_hash(error_text)
            if eh in state.get("error_hashes", []):
                return "failed"

        return "retry"

    # ── Build the graph ────────────────────────────────────────
    graph.add_node("memory_recall", memory_recall_node)
    graph.add_node("product_manager", product_manager_node)
    graph.add_node("architect", architect_node)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("developer", developer_node)
    graph.add_node("healer", healer_node)
    graph.add_node("tester", tester_node)
    graph.add_node("debugger", debugger_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("judge", judge_node)
    graph.add_node("prepare_retry", prepare_retry)
    graph.add_node("memory_save", memory_save_node)
    graph.add_node("done", done_node)
    graph.add_node("failed", failed_node)

    graph.add_edge(START, "memory_recall")
    graph.add_edge("memory_recall", "product_manager")
    graph.add_edge("product_manager", "architect")
    graph.add_edge("architect", "approval_gate")
    graph.add_conditional_edges("approval_gate", route_after_approval)
    graph.add_edge("developer", "healer")
    graph.add_edge("healer", "tester")
    graph.add_conditional_edges("tester", route_after_tester, {"reviewer": "reviewer", "debugger": "debugger"})
    graph.add_edge("debugger", "reviewer")
    graph.add_edge("reviewer", "judge")
    graph.add_conditional_edges(
        "judge",
        route_after_judge,
        {"done": "memory_save", "failed": "failed", "retry": "prepare_retry"},
    )
    def route_after_retry(state: AutodevState) -> str:
        if state.get("retry_target") == "tester":
            return "tester"
        return "developer"

    graph.add_conditional_edges(
        "prepare_retry",
        route_after_retry,
        {"developer": "developer", "tester": "tester"},
    )
    graph.add_edge("memory_save", "done")
    graph.add_edge("done", END)
    graph.add_edge("failed", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)
