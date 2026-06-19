"""Dependency management — installs pip packages inside the sandbox container."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_STDLIB_MODULES: set[str] = set(sys.stdlib_module_names)

_MODULE_TO_PACKAGE: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "attr": "attrs",
    "gi": "PyGObject",
    "serial": "pyserial",
}


def extract_imports(source: str) -> set[str]:
    """Extract top-level module names from Python source code."""
    modules: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return modules
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
    return modules


def resolve_packages(
    modules: set[str], declared_deps: list[str] | None = None
) -> list[str]:
    """Map module names to pip package names, filtering out stdlib."""
    declared = set(declared_deps or [])
    packages: set[str] = set()
    for mod in modules:
        if mod in _STDLIB_MODULES:
            continue
        pkg = _MODULE_TO_PACKAGE.get(mod, mod)
        packages.add(pkg)
    packages.update(declared)
    return sorted(packages)


def pip_install_command(packages: list[str]) -> str | None:
    """Return a pip install command string, or None if no packages needed."""
    if not packages:
        return None
    escaped = [p.replace("'", "") for p in packages]
    return f"pip install --no-cache-dir --quiet {' '.join(escaped)}"


def scan_workspace(workspace: Path) -> set[str]:
    """Extract all imports from Python files in the workspace."""
    all_imports: set[str] = set()
    for py_file in workspace.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8", errors="replace")
        all_imports.update(extract_imports(source))
    return all_imports
