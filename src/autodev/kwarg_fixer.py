"""Auto-fix keyword argument mismatches between class definitions and call sites.

Detects when a method call passes keyword arguments that don't match the
method's parameter names, and renames them at the call site.
"""

from __future__ import annotations

import ast
import re


def fix_kwarg_mismatches(source: str) -> str:
    """Fix keyword argument name mismatches in a single Python file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    class_params: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    params = [
                        a.arg for a in item.args.args if a.arg != "self"
                    ]
                    class_params[node.name] = params

    if not class_params:
        return source

    fixed = source
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        class_name = _get_call_name(node)
        if class_name not in class_params:
            continue

        params = class_params[class_name]
        if not params:
            continue

        for kw in node.keywords:
            if kw.arg and kw.arg not in params:
                best = _best_match(kw.arg, params)
                if best:
                    fixed = _rename_kwarg_at_line(
                        fixed, kw.arg, best, node.lineno
                    )

    return fixed


def _get_call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _best_match(wrong: str, candidates: list[str]) -> str | None:
    wrong_low = wrong.lower()
    semantically_similar = {
        "title": ["description", "name", "label", "text", "summary"],
        "description": ["title", "name", "label", "text", "summary", "desc"],
        "name": ["title", "description", "label"],
        "label": ["title", "name", "description"],
        "text": ["title", "description", "content", "message"],
        "content": ["text", "description", "body"],
        "priority": ["prio", "urgency", "importance", "level"],
        "prio": ["priority"],
    }

    related = semantically_similar.get(wrong_low, [])
    for candidate in candidates:
        if candidate.lower() in related:
            return candidate

    if len(candidates) == 1:
        return candidates[0]

    for candidate in candidates:
        if wrong_low in candidate.lower() or candidate.lower() in wrong_low:
            return candidate

    return None


def _rename_kwarg_at_line(source: str, old_kwarg: str, new_kwarg: str, lineno: int) -> str:
    lines = source.split("\n")
    if lineno < 1 or lineno > len(lines):
        return source

    for i in range(max(0, lineno - 2), min(len(lines), lineno + 3)):
        line = lines[i]
        pattern = rf'\b{re.escape(old_kwarg)}\s*='
        if re.search(pattern, line):
            lines[i] = re.sub(
                rf'\b{re.escape(old_kwarg)}(\s*=)', f'{new_kwarg}\\1', line, count=1
            )
            break

    return "\n".join(lines)
