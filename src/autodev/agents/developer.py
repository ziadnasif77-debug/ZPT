"""Self-healing Developer agent — inner loop validates and fixes code before submission.

Replaces single-pass generation with up to MAX_INNER_ITERATIONS rounds of:
  1. Generate/fix code via LLM
  2. Self-review (AST, imports, f-strings, structure)
  3. Auto-fix found issues deterministically
  4. Validate (py_compile + import resolution)
  5. If still broken, feed lessons back and retry

Only submits code that passes internal validation, or the best attempt after max tries.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import py_compile
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from autodev.code_healer import (
    classify_imports,
    fix_fstring_quotes,
    fix_import_style,
    heal_workspace,
)
from autodev.error_localizer import ErrorCategory, ErrorLocalizer
from autodev.import_fixer import fix_imports
from autodev.kwarg_fixer import fix_kwarg_mismatches
from autodev.patcher import apply_code_preserving
from autodev.project_mapper import ProjectMapper
from autodev.schemas import CodeBundle
from autodev.surgical_patcher import SurgicalPatcher
from autodev.syntax_fixer import fix_syntax

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient
    from autodev.state import AutodevState

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "developer.md"

MAX_INNER_ITERATIONS = 5

_FSTRING_CHECKLIST = [
    ("ast_parse", "AST parse — zero syntax errors"),
    ("imports_resolved", "All names are imported"),
    ("fstring_clean", "f-string quote consistency"),
    ("compile_clean", "py_compile passes"),
    ("main_block", "__main__ block exists if required"),
    ("no_circular", "No circular imports between files"),
]


def load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def run(state: "AutodevState", config: "AppConfig", llm: "LLMClient") -> dict:
    """Self-healing developer: inner loop that validates before submitting."""
    system_prompt = load_prompt()
    plan = state.get("plan") or {}
    iteration = state.get("iteration", 0)
    is_retry = iteration > 0

    workspace = Path(state.get("workspace_path", "./workspace"))
    workspace.mkdir(parents=True, exist_ok=True)

    user_parts = _build_user_parts(state, plan, iteration, is_retry, workspace, config)

    inner_feedback = None
    best_code_bundle = None
    best_issues_count = float("inf")
    inner_logs: list[str] = []

    for attempt in range(1, MAX_INNER_ITERATIONS + 1):
        messages = _build_messages(system_prompt, user_parts, inner_feedback, attempt)

        code_bundle = llm.chat(agent="developer", messages=messages, response_model=CodeBundle)

        files_data = [f.model_dump() for f in code_bundle.files]

        allowed_files = set(plan.get("files_needed", []))
        allowed_files.add("test_runner.py")
        if allowed_files:
            files_data = [f for f in files_data if f["path"] in allowed_files]

        modified = apply_code_preserving(workspace, files_data, is_retry or attempt > 1)

        total_lines = sum(len(f.content.splitlines()) for f in code_bundle.files)
        inner_logs.append(f"[INNER-{attempt}] Generated {len(code_bundle.files)} file(s) ({total_lines} lines)")

        issues = _self_review(workspace, code_bundle)

        if issues:
            fixed_count = _auto_fix(workspace, issues)
            if fixed_count > 0:
                inner_logs.append(f"[INNER-{attempt}] Self-review found {len(issues)} issue(s), auto-fixed {fixed_count}")
            else:
                inner_logs.append(f"[INNER-{attempt}] Self-review found {len(issues)} issue(s)")

            for issue in issues:
                inner_logs.append(f"  - {issue['description']}")

        healer_report = heal_workspace(workspace)
        healer_fixes = sum(len(v) for k, v in healer_report.items() if k != "compile_errors" and v)
        if healer_fixes > 0:
            inner_logs.append(f"[INNER-{attempt}] Healer applied {healer_fixes} additional fix(es)")

        validation = _validate(workspace)

        remaining_issues = len([i for i in issues if not i.get("auto_fixed")]) if issues else 0
        remaining_issues += len(validation.get("errors", []))

        if remaining_issues < best_issues_count:
            best_issues_count = remaining_issues
            _sync_bundle_from_workspace(workspace, code_bundle)
            best_code_bundle = code_bundle

        if validation["passed"]:
            inner_logs.append(f"[INNER-{attempt}] Validation: CLEAN — submitting to Tester")
            _sync_bundle_from_workspace(workspace, code_bundle)
            _print_logs(inner_logs)
            return {
                "code_bundle": code_bundle.model_dump(),
                "modified_files": modified,
                "inner_iterations": attempt,
            }

        if attempt < MAX_INNER_ITERATIONS:
            inner_feedback = _extract_lesson(validation["errors"], issues or [])
            inner_logs.append(f"[INNER-{attempt}] Still has {len(validation['errors'])} issue(s), retrying...")

    inner_logs.append(f"[INNER] Submitting best attempt after {MAX_INNER_ITERATIONS} inner iterations")
    _print_logs(inner_logs)
    return {
        "code_bundle": best_code_bundle.model_dump() if best_code_bundle else code_bundle.model_dump(),
        "modified_files": modified,
        "inner_iterations": MAX_INNER_ITERATIONS,
    }


def _build_user_parts(
    state: "AutodevState",
    plan: dict,
    iteration: int,
    is_retry: bool,
    workspace: Path,
    config: "AppConfig",
) -> list[str]:
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

    return user_parts


def _build_messages(
    system_prompt: str,
    user_parts: list[str],
    inner_feedback: str | None,
    attempt: int,
) -> list[dict]:
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    if inner_feedback and attempt > 1:
        msgs.append({
            "role": "user",
            "content": (
                f"## Internal Review Feedback (attempt {attempt})\n"
                f"Your previous code had issues. Fix them:\n\n{inner_feedback}"
            ),
        })
    return msgs


def _self_review(workspace: Path, code_bundle: CodeBundle) -> list[dict]:
    """Run the self-review checklist on all generated files."""
    issues: list[dict] = []

    all_classes: dict[str, list[str]] = {}
    all_files: dict[str, str] = {}

    for py_file in workspace.glob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            all_files[py_file.name] = source
        except Exception:
            continue

        # Run text-based checks first (work even on broken code)
        _check_fstring_quotes(py_file.name, source, issues)

        # 1. AST parse check
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            issues.append({
                "file": py_file.name,
                "line": e.lineno or 0,
                "type": "syntax",
                "description": f"{py_file.name}:{e.lineno}: SyntaxError: {e.msg}",
                "auto_fixable": True,
            })
            _check_missing_imports_regex(py_file.name, source, issues)
            continue

        # 2. Check all names are imported
        _check_undefined_names(py_file.name, source, tree, issues)

        # 4. Collect class methods for cross-file checking
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                all_classes[node.name] = methods

        # 5. Check __main__ block if file seems like an entry point
        _check_main_block(py_file.name, source, tree, issues)

    # 6. Cross-file: check test references match actual exports
    _check_test_exports(all_files, all_classes, issues)

    # 7. Check for circular imports
    _check_circular_imports(all_files, issues)

    # 8. Priority map ordering
    _check_priority_maps(all_files, issues)

    return issues


def _check_undefined_names(filename: str, source: str, tree: ast.AST, issues: list[dict]):
    """Check for names used but not imported or defined."""
    imported: set[str] = set()
    defined: set[str] = set()
    used: set[str] = set()

    builtins = {
        "print", "len", "range", "str", "int", "float", "bool", "list", "dict",
        "set", "tuple", "type", "super", "property", "staticmethod", "classmethod",
        "isinstance", "issubclass", "hasattr", "getattr", "setattr", "delattr",
        "callable", "iter", "next", "zip", "map", "filter", "sorted", "reversed",
        "enumerate", "min", "max", "sum", "abs", "round", "open", "input", "any",
        "all", "repr", "format", "chr", "ord", "hex", "oct", "bin", "hash", "id",
        "vars", "dir", "help", "exec", "eval", "compile", "globals", "locals",
        "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError",
        "ImportError", "FileNotFoundError", "RuntimeError", "StopIteration",
        "Exception", "BaseException", "NotImplementedError", "ZeroDivisionError",
        "AssertionError", "NameError", "SyntaxError", "OSError", "IOError",
        "PermissionError", "ModuleNotFoundError", "ConnectionError", "TimeoutError",
        "self", "cls", "True", "False", "None", "object", "NotImplemented",
        "__name__", "__file__", "__doc__", "__all__", "__init__", "__main__",
        "breakpoint", "exit", "quit", "complex", "bytes", "bytearray",
        "memoryview", "frozenset", "slice", "pow", "divmod",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                defined.add(arg.arg)
        elif isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                used.add(node.id)
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)

    undefined = used - imported - defined - builtins

    from autodev.import_fixer import _KNOWN_IMPORTS
    for name in sorted(undefined):
        if name in _KNOWN_IMPORTS:
            issues.append({
                "file": filename,
                "type": "missing_import",
                "description": f"{filename}: missing import for '{name}'",
                "auto_fixable": True,
                "fix": _KNOWN_IMPORTS[name],
            })


def _check_fstring_quotes(filename: str, source: str, issues: list[dict]):
    """Check for f-string quote conflicts inside f-string expressions."""
    import re
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.lstrip()
        for prefix in ("f'", "f\"", "F'", "F\""):
            if prefix not in stripped:
                continue
            outer_quote = prefix[-1]
            idx = stripped.index(prefix) + len(prefix)
            depth = 0
            in_expr = False
            expr_chars: list[str] = []
            for ch in stripped[idx:]:
                if ch == '{':
                    depth += 1
                    in_expr = True
                    expr_chars = []
                elif ch == '}' and depth > 0:
                    depth -= 1
                    if depth == 0:
                        expr_text = ''.join(expr_chars)
                        if outer_quote in expr_text:
                            issues.append({
                                "file": filename,
                                "line": i,
                                "type": "fstring",
                                "description": (
                                    f"{filename}:{i}: f-string quote conflict "
                                    f"({outer_quote}-quoted f-string uses "
                                    f"{outer_quote} inside expression)"
                                ),
                                "auto_fixable": True,
                            })
                            break
                        in_expr = False
                elif in_expr:
                    expr_chars.append(ch)
            break


def _check_missing_imports_regex(filename: str, source: str, issues: list[dict]):
    """Fallback import check using regex when AST parse fails."""
    import re
    from autodev.import_fixer import _KNOWN_IMPORTS

    imported = set()
    for m in re.finditer(r'^\s*(?:from\s+(\S+)\s+)?import\s+(.+)', source, re.MULTILINE):
        if m.group(1):
            imported.add(m.group(1).split('.')[0])
        for name in m.group(2).split(','):
            imported.add(name.strip().split(' as ')[0].split('.')[0])

    for name in _KNOWN_IMPORTS:
        if re.search(r'\b' + re.escape(name) + r'\b', source) and name not in imported:
            issues.append({
                "file": filename,
                "type": "missing_import",
                "description": f"{filename}: missing import for '{name}'",
                "auto_fixable": True,
                "fix": _KNOWN_IMPORTS[name],
            })


def _check_main_block(filename: str, source: str, tree: ast.AST, issues: list[dict]):
    """Check if a file that looks like an entry point has __main__ guard."""
    if filename == "test_runner.py" or filename.startswith("test_"):
        return

    has_main_func = False
    has_main_block = '__name__' in source and '__main__' in source

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            has_main_func = True
            break

    if has_main_func and not has_main_block:
        issues.append({
            "file": filename,
            "type": "main_block",
            "description": f"{filename}: has main() function but no if __name__ == '__main__' block",
            "auto_fixable": False,
        })


def _check_test_exports(all_files: dict[str, str], all_classes: dict[str, list[str]], issues: list[dict]):
    """Check that test files reference names that actually exist in code files."""
    test_source = all_files.get("test_runner.py", "")
    if not test_source:
        return

    code_files = {n: s for n, s in all_files.items() if n != "test_runner.py"}
    if not code_files:
        return

    all_defined: set[str] = set()
    for name, source in code_files.items():
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    all_defined.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    all_defined.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    all_defined.add(node.id)
        except SyntaxError:
            continue

    try:
        test_tree = ast.parse(test_source)
    except SyntaxError:
        return

    for node in ast.walk(test_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod_name = node.module.split(".")[0]
            if mod_name in {n.replace(".py", "") for n in code_files}:
                for alias in node.names:
                    name = alias.name
                    if name not in all_defined and name not in all_classes:
                        issues.append({
                            "file": "test_runner.py",
                            "type": "missing_export",
                            "description": f"test_runner.py imports '{name}' from {node.module} but it's not defined there",
                            "auto_fixable": False,
                        })


def _check_circular_imports(all_files: dict[str, str], issues: list[dict]):
    """Detect simple circular imports between workspace files."""
    local_modules = {n.replace(".py", "") for n in all_files}
    imports_graph: dict[str, set[str]] = {}

    for filename, source in all_files.items():
        mod = filename.replace(".py", "")
        imports_graph[mod] = set()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name.split(".")[0]
                    if target in local_modules:
                        imports_graph[mod].add(target)
            elif isinstance(node, ast.ImportFrom) and node.module:
                target = node.module.split(".")[0]
                if target in local_modules:
                    imports_graph[mod].add(target)

    for mod_a, deps_a in imports_graph.items():
        for mod_b in deps_a:
            if mod_b in imports_graph and mod_a in imports_graph.get(mod_b, set()):
                issues.append({
                    "file": f"{mod_a}.py",
                    "type": "circular_import",
                    "description": f"Circular import: {mod_a}.py <-> {mod_b}.py",
                    "auto_fixable": False,
                })


def _check_priority_maps(all_files: dict[str, str], issues: list[dict]):
    """Check priority maps use correct ordering (HIGH=1 or highest, LOW=3 or lowest)."""
    import re
    for filename, source in all_files.items():
        if filename == "test_runner.py":
            continue
        for match in re.finditer(
            r'(?:priority|PRIORITY)\s*[=:]\s*\{([^}]+)\}', source
        ):
            block = match.group(1)
            entries = re.findall(r'["\']?(HIGH|MEDIUM|LOW)["\']?\s*:\s*(\d+)', block, re.I)
            if len(entries) >= 2:
                vals = {k.upper(): int(v) for k, v in entries}
                if "HIGH" in vals and "LOW" in vals:
                    if vals["HIGH"] > vals["LOW"]:
                        issues.append({
                            "file": filename,
                            "type": "priority_order",
                            "description": f"{filename}: Priority map has HIGH={vals['HIGH']} > LOW={vals['LOW']} (HIGH should be lowest number)",
                            "auto_fixable": False,
                        })


def _auto_fix(workspace: Path, issues: list[dict]) -> int:
    """Auto-fix issues that are marked as auto_fixable."""
    fixed_count = 0

    for py_file in workspace.glob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            original = source

            source = fix_syntax(source)
            source = fix_fstring_quotes(source)
            source = fix_imports(source)
            source = fix_import_style(source)
            source = fix_kwarg_mismatches(source)

            if source != original:
                py_file.write_text(source, encoding="utf-8")
                fixed_count += 1
        except Exception:
            continue

    for issue in issues:
        if issue.get("auto_fixable"):
            issue["auto_fixed"] = True

    return fixed_count


def _validate(workspace: Path) -> dict:
    """Validate all files: py_compile + AST parse + import resolution."""
    errors: list[str] = []
    all_passed = True

    local_modules = {p.stem for p in workspace.glob("*.py")}

    for py_file in workspace.glob("*.py"):
        # py_compile check
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"compile: {py_file.name}: {e}")
            all_passed = False
            continue

        # AST parse check
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except SyntaxError as e:
            errors.append(f"syntax: {py_file.name}:{e.lineno}: {e.msg}")
            all_passed = False
            continue

        # Import resolution check
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if not _can_resolve_import(mod, local_modules):
                        errors.append(f"import: {py_file.name}: cannot resolve '{alias.name}'")
                        all_passed = False
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mod = node.module.split(".")[0]
                if not _can_resolve_import(mod, local_modules):
                    errors.append(f"import: {py_file.name}: cannot resolve 'from {node.module}'")
                    all_passed = False

    return {"passed": all_passed, "errors": errors}


def _can_resolve_import(module_name: str, local_modules: set[str]) -> bool:
    """Check if a module can be resolved."""
    if module_name in local_modules:
        return True
    if module_name in getattr(sys, "stdlib_module_names", set()):
        return True
    _KNOWN_AVAILABLE = {
        "pytest", "requests", "numpy", "pandas", "np", "pd",
        "_pytest", "urllib3", "charset_normalizer", "certifi", "idna",
        "pytz", "dateutil",
    }
    if module_name in _KNOWN_AVAILABLE:
        return True
    try:
        spec = importlib.util.find_spec(module_name)
        return spec is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _extract_lesson(errors: list[str], issues: list[dict]) -> str:
    """Build feedback from validation errors to guide the next inner attempt."""
    parts = ["Fix the following issues in your code:"]

    for err in errors:
        parts.append(f"  - {err}")

    unfixed = [i for i in issues if not i.get("auto_fixed")]
    for issue in unfixed:
        parts.append(f"  - {issue['description']}")

    return "\n".join(parts)


def _sync_bundle_from_workspace(workspace: Path, code_bundle: CodeBundle):
    """Update the code bundle with actual file contents from workspace (after fixes)."""
    for f in code_bundle.files:
        fp = workspace / f.path
        if fp.exists():
            try:
                f.content = fp.read_text(encoding="utf-8")
            except Exception:
                pass


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


def _print_logs(logs: list[str]):
    """Print inner loop logs to stdout for observability."""
    for line in logs:
        print(line, flush=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SURGICAL HEALING LOOP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MAX_SURGICAL_ATTEMPTS = 10


def heal_until_passing(
    workspace: Path,
    stderr: str,
    stdout: str,
    llm: "LLMClient | None" = None,
    max_attempts: int = MAX_SURGICAL_ATTEMPTS,
) -> dict:
    """Systematic locate→inspect→patch→verify loop.

    Returns a report dict with healing results and logs.
    """
    localizer = ErrorLocalizer()
    mapper = ProjectMapper()
    patcher = SurgicalPatcher()

    logs: list[str] = []
    patches_applied: list[str] = []
    import os
    saved_cwd = os.getcwd()

    try:
        os.chdir(str(workspace))

        for attempt in range(1, max_attempts + 1):
            error = localizer.localize(stderr, stdout, workspace)
            category = localizer.classify(error)

            logs.append(
                f"[HEAL-{attempt}] {error.error_type}: {error.message[:120]}"
            )

            if category == ErrorCategory.SYNTAX_ERROR:
                heal_workspace(workspace)
                logs.append(f"[HEAL-{attempt}] Applied syntax healer")
                patches_applied.append(f"syntax fix in {error.true_source_file}")
                break

            pmap = mapper.map_workspace(workspace)
            gaps = pmap.gaps

            if not gaps and category in (
                ErrorCategory.MISSING_METHOD,
                ErrorCategory.MISSING_ATTRIBUTE,
                ErrorCategory.MISSING_CLASS,
            ):
                candidate_gaps = _gaps_from_error(error, pmap)
                gaps = _filter_already_resolved(candidate_gaps, pmap)

            if gaps:
                new_patches_this_round = 0
                for gap in gaps:
                    if _gap_already_resolved(gap, pmap):
                        logs.append(
                            f"[HEAL-{attempt}] Skip: {gap.element_name} "
                            f"already exists in {gap.should_be_in}"
                        )
                        continue

                    logs.append(
                        f"[HEAL-{attempt}] Gap: {gap.element_name} "
                        f"missing in {gap.should_be_in}"
                    )
                    patch = patcher.plan_patch(gap, pmap)
                    result = patcher.apply_patch(patch, workspace)

                    if result.success:
                        patches_applied.append(result.description)
                        logs.append(f"[HEAL-{attempt}] Patched: {result.description}")
                        new_patches_this_round += 1
                    else:
                        logs.append(
                            f"[HEAL-{attempt}] Patch failed: {result.error}"
                        )

                if new_patches_this_round == 0:
                    logs.append(f"[HEAL-{attempt}] All gaps already resolved — done")
                    break

                verify_map = mapper.map_workspace(workspace)
                remaining = [g for g in gaps if not _gap_already_resolved(g, verify_map)]
                if not remaining:
                    logs.append(f"[HEAL-{attempt}] All gaps resolved after patching")
                    break

            elif category == ErrorCategory.MISSING_IMPORT:
                heal_workspace(workspace)
                logs.append(f"[HEAL-{attempt}] Applied import healer")
                patches_applied.append(f"import fix for {error.missing_element}")
                break
            elif category == ErrorCategory.WRONG_LOGIC:
                logs.append(f"[HEAL-{attempt}] Logic error — needs LLM")
                break
            else:
                heal_workspace(workspace)
                logs.append(f"[HEAL-{attempt}] Applied general healer")
                break

    finally:
        os.chdir(saved_cwd)

    _print_logs(logs)

    return {
        "patches_applied": patches_applied,
        "logs": logs,
        "attempts": min(attempt, max_attempts) if 'attempt' in dir() else 0,
    }


def _gap_already_resolved(gap, pmap) -> bool:
    """Check if a gap has already been resolved in the current workspace state."""
    fmap = pmap.files.get(gap.should_be_in)
    if not fmap:
        return False

    if gap.gap_type == "missing_method":
        cmap = fmap.classes.get(gap.class_name)
        if cmap and gap.element_name in cmap.methods:
            return True
    elif gap.gap_type == "missing_class":
        if gap.class_name in fmap.classes:
            return True
    elif gap.gap_type == "missing_attribute":
        cmap = fmap.classes.get(gap.class_name)
        if cmap and gap.element_name in cmap.attributes:
            return True
    elif gap.gap_type == "missing_export":
        all_names = set(fmap.classes.keys()) | set(fmap.functions) | set(fmap.top_level_names)
        if gap.element_name in all_names:
            return True

    return False


def _filter_already_resolved(gaps: list, pmap) -> list:
    """Remove gaps that are already resolved in the workspace."""
    return [g for g in gaps if not _gap_already_resolved(g, pmap)]


def _gaps_from_error(error, pmap) -> list:
    """Create gaps from error location when mapper didn't find them."""
    from autodev.project_mapper import Gap

    if not error.missing_element:
        return []

    gap_type = "missing_method"
    if error.element_type == "attribute":
        gap_type = "missing_attribute"
    elif error.element_type == "import":
        gap_type = "missing_import"

    return [Gap(
        gap_type=gap_type,
        expected_by=f"{error.crash_file} line {error.crash_line}",
        should_be_in=error.true_source_file,
        class_name=error.true_source_class,
        element_name=error.missing_element,
        usage_line=error.crash_line,
    )]
