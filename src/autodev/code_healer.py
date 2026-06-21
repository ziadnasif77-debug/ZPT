"""Deterministic code healer — rule-based, zero LLM, runs in <1 second.

Runs AFTER Developer writes files and BEFORE Tester. Fixes the most common
errors that small LLMs produce, eliminating retries for trivial issues.

Subsystems:
1. Import Healer — auto-add missing imports, fix wrong import styles
2. Syntax Healer — fix f-string quote conflicts, brackets, strings
3. Dependency Healer — classify imports as builtin/local/external
4. Kwarg Healer — fix keyword argument name mismatches
5. Pytest Healer — extract exact failing assertions for precise feedback
"""

from __future__ import annotations

import ast
import os
import py_compile
import re
import sys
import tempfile
from pathlib import Path

from autodev.import_fixer import fix_imports
from autodev.kwarg_fixer import fix_kwarg_mismatches
from autodev.syntax_fixer import fix_syntax


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. IMPORT HEALER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_WRONG_STYLE_PATTERNS: dict[str, tuple[str, str]] = {
    # name_used: (wrong_import_pattern, correct_import)
    # When code uses datetime() as constructor but has `import datetime`
    "datetime": (r"^import datetime$", "from datetime import datetime"),
    "date": (r"^import date$", "from datetime import date"),
    "timedelta": (r"^import timedelta$", "from datetime import timedelta"),
    "Path": (r"^import pathlib$", "from pathlib import Path"),
    "Enum": (r"^import enum$", "from enum import Enum"),
    "dataclass": (r"^import dataclasses$", "from dataclasses import dataclass"),
    "defaultdict": (r"^import collections$", "from collections import defaultdict"),
    "Counter": (r"^import collections$", "from collections import Counter"),
    "ABC": (r"^import abc$", "from abc import ABC"),
    "abstractmethod": (r"^import abc$", "from abc import abstractmethod"),
}


def fix_import_style(source: str) -> str:
    """Fix wrong import style: `import datetime` + `datetime()` usage.

    When code calls `datetime(2024, 1, 1)` but has `import datetime`,
    the correct form is `from datetime import datetime`.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    used_as_callable: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            used_as_callable.add(node.func.id)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used_as_callable.add(node.id)

    lines = source.split("\n")
    changed = False

    for name, (wrong_pattern, correct_import) in _WRONG_STYLE_PATTERNS.items():
        if name not in used_as_callable:
            continue

        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(wrong_pattern, stripped):
                if not _has_from_import(source, name):
                    lines[i] = correct_import
                    changed = True
                break

    if changed:
        return "\n".join(lines)
    return source


def _has_from_import(source: str, name: str) -> bool:
    """Check if source already has `from X import name`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. SYNTAX HEALER — f-string quote conflicts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fix_fstring_quotes(source: str) -> str:
    """Fix f-strings where dict/list access uses the same quote as the f-string.

    WRONG: f'value: {d['key']}'   ← single quote conflicts
    RIGHT: f"value: {d['key']}"   ← switch outer to double quotes

    WRONG: f"value: {d["key"]}"   ← double quote conflicts
    RIGHT: f'value: {d["key"]}'   ← switch outer to single quotes
    """
    lines = source.split("\n")
    fixed_lines = []
    changed = False

    for line in lines:
        fixed_line = _fix_fstring_line(line)
        if fixed_line != line:
            changed = True
        fixed_lines.append(fixed_line)

    if changed:
        return "\n".join(fixed_lines)
    return source


def _fix_fstring_line(line: str) -> str:
    """Fix f-string quote conflicts on a single line."""
    result = line

    # Pattern: f'...{...[']...}...' — single-quoted f-string with ['...'] inside
    # We look for f-strings that would cause SyntaxError due to quote conflicts
    result = _fix_fstring_single_to_double(result)
    result = _fix_fstring_double_to_single(result)

    return result


def _fix_fstring_single_to_double(line: str) -> str:
    """Fix f'...{d['key']}...' → f\"...{d['key']}...\" """
    # Match f'...' that contains {... ['...'] ...}
    pattern = r"""((?:^|[^'"]))(f')((?:[^'\\]|\\.)*)(\{[^}]*\[')((?:[^'\\]|\\.)*)('])([^}]*\})((?:[^'\\]|\\.)*)(')\b"""

    # Simpler approach: find f'...' strings and check if they contain ['
    matches = list(re.finditer(r"f'", line))
    if not matches:
        return line

    for m in matches:
        start = m.start()
        fstr_content_start = m.end()

        # Find the closing single quote (not escaped, not inside {})
        depth = 0
        i = fstr_content_start
        end = -1
        while i < len(line):
            ch = line[i]
            if ch == '\\':
                i += 2
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == "'" and depth == 0:
                end = i
                break
            i += 1

        if end < 0:
            continue

        inner = line[fstr_content_start:end]

        # Check if inner contains [' which would conflict
        if re.search(r"\['", inner) or re.search(r"'\]", inner):
            # Switch outer quotes to double
            if '"' not in inner or inner.count('"') == inner.count('\\"'):
                new_fstr = 'f"' + inner + '"'
                line = line[:start] + new_fstr + line[end + 1:]

    return line


def _fix_fstring_double_to_single(line: str) -> str:
    """Fix f\"...{d[\"key\"]}...\" → f'...{d[\"key\"]}...'"""
    matches = list(re.finditer(r'f"', line))
    if not matches:
        return line

    for m in matches:
        start = m.start()
        fstr_content_start = m.end()

        depth = 0
        i = fstr_content_start
        end = -1
        while i < len(line):
            ch = line[i]
            if ch == '\\':
                i += 2
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == '"' and depth == 0:
                end = i
                break
            i += 1

        if end < 0:
            continue

        inner = line[fstr_content_start:end]

        if re.search(r'\["', inner) or re.search(r'"\]', inner):
            if "'" not in inner or inner.count("'") == inner.count("\\'"):
                new_fstr = "f'" + inner + "'"
                line = line[:start] + new_fstr + line[end + 1:]

    return line


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. DEPENDENCY HEALER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_STDLIB_MODULES: set[str] | None = None


def _get_stdlib_modules() -> set[str]:
    """Get the set of stdlib module names."""
    global _STDLIB_MODULES
    if _STDLIB_MODULES is None:
        _STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", set()))
        _STDLIB_MODULES.update({
            "json", "os", "sys", "re", "math", "random", "pathlib", "typing",
            "datetime", "collections", "enum", "dataclasses", "abc", "io",
            "functools", "itertools", "hashlib", "uuid", "tempfile", "shutil",
            "csv", "string", "textwrap", "pprint", "traceback", "inspect",
            "platform", "argparse", "unittest", "copy", "glob", "subprocess",
            "threading", "logging", "sqlite3", "pickle", "struct", "socket",
            "http", "urllib", "base64", "hmac", "secrets", "contextlib",
            "operator", "time", "calendar", "decimal", "fractions",
            "statistics", "heapq", "bisect", "array", "queue",
            "configparser", "tomllib", "xml", "html", "email",
            "ast", "dis", "py_compile", "compileall",
        })
    return _STDLIB_MODULES


def classify_imports(workspace: Path) -> dict[str, list[str]]:
    """Classify all imports in workspace as builtin, local, or external."""
    stdlib = _get_stdlib_modules()
    local_modules = {p.stem for p in workspace.glob("*.py")}

    all_imports: set[str] = set()
    for py_file in workspace.glob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    all_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                all_imports.add(node.module.split(".")[0])

    result: dict[str, list[str]] = {
        "builtin": [],
        "local": [],
        "external": [],
    }

    for mod in sorted(all_imports):
        if mod in stdlib:
            result["builtin"].append(mod)
        elif mod in local_modules:
            result["local"].append(mod)
        else:
            result["external"].append(mod)

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. COMPILE CHECKER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compile_check(filepath: Path) -> tuple[bool, str]:
    """Run py_compile on a file. Returns (ok, error_message)."""
    try:
        py_compile.compile(str(filepath), doraise=True)
        return True, ""
    except py_compile.PyCompileError as exc:
        return False, str(exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. PYTEST HEALER — extract exact failing assertions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_test_failures(stderr: str, stdout: str) -> list[dict[str, str]]:
    """Extract exact failing assertion details from test output.

    Returns list of dicts with:
      - file: the test file
      - line: the line number
      - assertion: the exact assertion that failed
      - actual: the actual value (if extractable)
      - expected: the expected value (if extractable)
      - message: human-readable failure description
    """
    failures: list[dict[str, str]] = []
    combined = stderr + "\n" + stdout

    # Pattern: "FAIL: test_name - assertion message"
    for m in re.finditer(r"FAIL:\s*(\w+)\s*-\s*(.+)", combined):
        failures.append({
            "test": m.group(1),
            "message": m.group(2).strip(),
        })

    # Pattern: AssertionError with details from traceback
    for m in re.finditer(
        r'File "([^"]+)", line (\d+).*?\n\s+(assert .+?)(?:\n|$)',
        combined, re.DOTALL,
    ):
        filename = m.group(1).replace("\\", "/").split("/")[-1]
        lineno = m.group(2)
        assertion = m.group(3).strip()
        failures.append({
            "file": filename,
            "line": lineno,
            "assertion": assertion,
            "message": f"Line {lineno} in {filename}: {assertion}",
        })

    # Pattern: "assert X == Y" with "AssertionError: expected X but got Y"
    for m in re.finditer(
        r"AssertionError:\s*(.+?)(?:\n|$)", combined
    ):
        detail = m.group(1).strip()
        expected = ""
        actual = ""
        em = re.search(r"expected\s+(.+?)\s+but\s+got\s+(.+)", detail, re.I)
        if em:
            expected = em.group(1)
            actual = em.group(2)
        failures.append({
            "message": detail,
            "expected": expected,
            "actual": actual,
        })

    # Pattern: "AssertionError" from Python traceback with value comparison
    for m in re.finditer(
        r"assert\s+(.+?)\s*==\s*(.+?)(?:\n|,|$)", combined
    ):
        left = m.group(1).strip()
        right = m.group(2).strip()
        failures.append({
            "assertion": f"assert {left} == {right}",
            "message": f"Comparison failed: {left} == {right}",
        })

    # Deduplicate by message
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for f in failures:
        msg = f.get("message", "") + f.get("assertion", "")
        if msg and msg not in seen:
            seen.add(msg)
            unique.append(f)

    return unique


def format_test_failures(failures: list[dict[str, str]]) -> str:
    """Format extracted failures into precise developer feedback."""
    if not failures:
        return ""

    parts = ["EXACT TEST FAILURES (fix these specific issues):"]
    for i, f in enumerate(failures, 1):
        line = f"  {i}. "
        if f.get("file") and f.get("line"):
            line += f"{f['file']}:{f['line']} — "
        if f.get("assertion"):
            line += f"`{f['assertion']}` "
        if f.get("message"):
            line += f"→ {f['message']}"
        if f.get("expected") and f.get("actual"):
            line += f" (expected: {f['expected']}, got: {f['actual']})"
        parts.append(line)

    return "\n".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN HEALER — unified entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def heal_workspace(workspace: Path) -> dict[str, list[str]]:
    """Run all deterministic healers on the workspace.

    Returns a report dict with lists of actions taken per category.
    """
    report: dict[str, list[str]] = {
        "syntax": [],
        "imports": [],
        "import_style": [],
        "fstring": [],
        "kwargs": [],
        "compile_errors": [],
    }

    if not workspace.is_dir():
        return report

    for py_file in workspace.glob("*.py"):
        try:
            source = py_file.read_text(encoding="utf-8")
            fixed = source

            # 1. Syntax fixes (brackets, strings, etc.)
            after_syntax = fix_syntax(fixed)
            if after_syntax != fixed:
                report["syntax"].append(py_file.name)
                fixed = after_syntax

            # 2. f-string quote conflicts
            after_fstring = fix_fstring_quotes(fixed)
            if after_fstring != fixed:
                report["fstring"].append(py_file.name)
                fixed = after_fstring

            # 3. Missing imports
            after_imports = fix_imports(fixed)
            if after_imports != fixed:
                report["imports"].append(py_file.name)
                fixed = after_imports

            # 4. Wrong import style (import datetime → from datetime import datetime)
            after_style = fix_import_style(fixed)
            if after_style != fixed:
                report["import_style"].append(py_file.name)
                fixed = after_style

            # 5. Keyword argument mismatches
            after_kwargs = fix_kwarg_mismatches(fixed)
            if after_kwargs != fixed:
                report["kwargs"].append(py_file.name)
                fixed = after_kwargs

            # Write back if changed
            if fixed != source:
                py_file.write_text(fixed, encoding="utf-8")

            # 6. Final compile check
            ok, err = compile_check(py_file)
            if not ok:
                report["compile_errors"].append(f"{py_file.name}: {err}")

        except Exception as exc:
            report["compile_errors"].append(f"{py_file.name}: {exc}")

    return report
