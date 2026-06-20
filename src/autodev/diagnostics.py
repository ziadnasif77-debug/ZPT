"""Deterministic diagnosis and pre-flight validation — the self-healing brain.

Instead of relying on an LLM reviewer to guess what went wrong, this module
parses actual Python tracebacks and statically analyzes generated code to
produce precise, actionable feedback and route repairs to the right agent.

This implements the key insight from automated-program-repair research: the
feedback stage is the bottleneck, and deterministic signals (compiler/runtime
errors) are far stronger than an LLM's opinion about its own code.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# Files owned by the tester agent vs the developer agent.
_TEST_FILES = {"test_runner.py"}


@dataclass
class Diagnosis:
    culprit: str  # "developer" | "tester" | "unknown"
    error_type: str  # "syntax" | "import" | "name" | "attribute" | "assertion" | "runtime" | "none"
    message: str  # precise, actionable feedback
    confident: bool = False  # True if we trust this over the LLM reviewer


def extract_public_api(source: str) -> set[str]:
    """Return top-level class, function, and assigned names defined in a module."""
    api: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return api
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            api.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    api.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            api.add(node.target.id)
    return api


def extract_local_imports(test_source: str, local_modules: set[str]) -> dict[str, set[str]]:
    """Return {module_name: {imported_names}} for imports from local modules."""
    result: dict[str, set[str]] = {}
    try:
        tree = ast.parse(test_source)
    except SyntaxError:
        return result
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            base = node.module.split(".")[0]
            if base in local_modules:
                names = {alias.name for alias in node.names if alias.name != "*"}
                result.setdefault(base, set()).update(names)
    return result


def check_api_mismatch(
    code_files: dict[str, str],
    test_source: str,
) -> list[str]:
    """Statically verify the test only imports names the code actually defines.

    Returns a list of precise error messages (empty if everything resolves).
    This catches "from task_manager import Task, Priority" when the code only
    defines TaskManager — BEFORE wasting a sandbox run.
    """
    errors: list[str] = []
    module_api: dict[str, set[str]] = {}
    local_modules: set[str] = set()
    for path, source in code_files.items():
        if path.endswith(".py"):
            mod = path[:-3].replace("/", ".").split(".")[-1]
            local_modules.add(mod)
            module_api[mod] = extract_public_api(source)

    imports = extract_local_imports(test_source, local_modules)
    for mod, names in imports.items():
        available = module_api.get(mod, set())
        for name in sorted(names):
            if name not in available:
                errors.append(
                    f"The test imports '{name}' from '{mod}', but '{mod}.py' does not "
                    f"define it. The code defines: {sorted(available) or 'nothing'}. "
                    f"Either add '{name}' to {mod}.py or fix the test import."
                )
    return errors


def diagnose_traceback(
    stderr: str,
    stdout: str,
    code_files: dict[str, str] | None = None,
) -> Diagnosis:
    """Parse a Python traceback to classify the error and identify the culprit."""
    text = stderr or ""
    code_files = code_files or {}

    # Which file does the deepest frame point at? That's usually the culprit.
    culprit_file = _last_file_in_traceback(text)
    culprit = _file_owner(culprit_file)

    # ── Syntax errors ──────────────────────────────────────────
    m = re.search(r"(SyntaxError|IndentationError|TabError): (.+)", text)
    if m:
        err_file = _syntax_error_file(text) or culprit_file
        return Diagnosis(
            culprit=_file_owner(err_file),
            error_type="syntax",
            message=(
                f"There is a {m.group(1)} in {err_file or 'the code'}: {m.group(2)}. "
                f"Fix the syntax error. Common causes: unterminated strings "
                f"(use \\n inside a single-line string instead of a real line break), "
                f"missing colons, or wrong indentation."
            ),
            confident=True,
        )

    # ── ImportError: cannot import name X from Y ───────────────
    m = re.search(r"cannot import name ['\"](\w+)['\"] from ['\"]([\w.]+)['\"]", text)
    if m:
        name, module = m.group(1), m.group(2).split(".")[-1]
        available = sorted(extract_public_api(code_files.get(f"{module}.py", "")))
        return Diagnosis(
            culprit="developer",
            error_type="import",
            message=(
                f"The test needs '{name}' from '{module}', but {module}.py does not "
                f"define it. {module}.py currently defines: {available or 'nothing'}. "
                f"Add a '{name}' class/function to {module}.py so the test can import it."
            ),
            confident=True,
        )

    # ── ModuleNotFoundError ────────────────────────────────────
    m = re.search(r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]", text)
    if m:
        return Diagnosis(
            culprit=culprit,
            error_type="import",
            message=(
                f"Module '{m.group(1)}' was not found. If it is a local file, make sure "
                f"the filename matches the import. Do NOT import third-party packages "
                f"that are not in the plan's dependencies."
            ),
            confident=True,
        )

    # ── NameError ──────────────────────────────────────────────
    m = re.search(r"NameError: name ['\"](\w+)['\"] is not defined", text)
    if m:
        return Diagnosis(
            culprit=culprit,
            error_type="name",
            message=(
                f"'{m.group(1)}' is used but never defined or imported in "
                f"{culprit_file or 'the code'}. Add the missing import or definition."
            ),
            confident=True,
        )

    # ── AttributeError ─────────────────────────────────────────
    m = re.search(r"AttributeError: (.+)", text)
    if m:
        return Diagnosis(
            culprit="developer",
            error_type="attribute",
            message=(
                f"AttributeError: {m.group(1)}. The code is missing a method or "
                f"attribute that the test expects. Make the implementation match the "
                f"API the test uses."
            ),
            confident=True,
        )

    # ── AssertionError (test ran, logic is wrong) ──────────────
    m = re.search(r"AssertionError:?\s*(.*)", text)
    if m:
        detail = m.group(1).strip()
        return Diagnosis(
            culprit="developer",
            error_type="assertion",
            message=(
                f"A test assertion failed{': ' + detail if detail else ''}. "
                f"The code runs but produces the wrong result. Re-read the acceptance "
                f"criteria and fix the logic so the expected output is produced."
            ),
            confident=True,
        )

    # ── TypeError ──────────────────────────────────────────────
    m = re.search(r"TypeError: (.+)", text)
    if m:
        return Diagnosis(
            culprit=culprit,
            error_type="runtime",
            message=(
                f"TypeError: {m.group(1)}. Check function signatures and the types "
                f"passed between the code and the test."
            ),
            confident=True,
        )

    # ── Generic runtime error ──────────────────────────────────
    m = re.search(r"(\w*Error): (.+)", text)
    if m:
        return Diagnosis(
            culprit=culprit,
            error_type="runtime",
            message=f"{m.group(1)}: {m.group(2)}. Fix the error shown in the traceback.",
            confident=False,
        )

    return Diagnosis(
        culprit="unknown",
        error_type="none",
        message="",
        confident=False,
    )


def _last_file_in_traceback(text: str) -> str | None:
    """Return the basename of the deepest file referenced in a traceback."""
    matches = re.findall(r'File "([^"]+)", line \d+', text)
    if not matches:
        return None
    last = matches[-1]
    return last.replace("\\", "/").split("/")[-1]


def _syntax_error_file(text: str) -> str | None:
    """SyntaxError points at a single file in its traceback frame."""
    matches = re.findall(r'File "([^"]+)", line \d+', text)
    if matches:
        return matches[-1].replace("\\", "/").split("/")[-1]
    return None


def _file_owner(filename: str | None) -> str:
    if not filename:
        return "unknown"
    base = filename.replace("\\", "/").split("/")[-1]
    if base in _TEST_FILES or base.startswith("test_"):
        return "tester"
    return "developer"
