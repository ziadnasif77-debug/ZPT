"""Dependency management — validates imports against pre-installed sandbox packages.

The sandbox Docker image (autodev-sandbox:latest) ships with common packages
pre-installed. No pip install happens at runtime — the container has no network.
If a package is missing from the image, add it to docker/Dockerfile and rebuild:
    docker build -t autodev-sandbox:latest ./docker/
"""

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

_PREINSTALLED_PACKAGES: set[str] = {
    "pytest",
    "requests",
    "numpy",
    "pandas",
}

_PREINSTALLED_MODULES: set[str] = _PREINSTALLED_PACKAGES | {
    "np",
    "pd",
    "_pytest",
    "urllib3",
    "charset_normalizer",
    "certifi",
    "idna",
    "pytz",
    "dateutil",
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


def check_imports(
    modules: set[str],
    workspace: Path | None = None,
) -> list[str]:
    """Return list of modules that are NOT available in the sandbox.

    A module is considered available if it is:
    - a stdlib module
    - a local .py file in the workspace
    - pre-installed in the sandbox Docker image
    """
    local_modules: set[str] = set()
    if workspace and workspace.is_dir():
        local_modules = {p.stem for p in workspace.glob("*.py")}

    missing: list[str] = []
    for mod in sorted(modules):
        if mod in _STDLIB_MODULES:
            continue
        if mod in local_modules:
            continue
        if mod in _PREINSTALLED_MODULES:
            continue
        pkg = _MODULE_TO_PACKAGE.get(mod, mod)
        if pkg in _PREINSTALLED_PACKAGES:
            continue
        missing.append(mod)
    return missing


def resolve_packages(
    modules: set[str],
    declared_deps: list[str] | None = None,
    workspace: Path | None = None,
) -> list[str]:
    """Map module names to pip package names, filtering out stdlib, local, and pre-installed."""
    declared = set(declared_deps or [])
    local_modules: set[str] = set()
    if workspace and workspace.is_dir():
        local_modules = {p.stem for p in workspace.glob("*.py")}

    packages: set[str] = set()
    for mod in modules:
        if mod in _STDLIB_MODULES:
            continue
        if mod in local_modules:
            continue
        if mod in _PREINSTALLED_MODULES:
            continue
        pkg = _MODULE_TO_PACKAGE.get(mod, mod)
        if pkg in _PREINSTALLED_PACKAGES:
            continue
        packages.add(pkg)
    for dep in declared:
        dep_base = dep.split(".")[0].lower()
        if dep_base in _STDLIB_MODULES:
            continue
        if dep_base in local_modules:
            continue
        if dep_base in _PREINSTALLED_MODULES:
            continue
        pkg = _MODULE_TO_PACKAGE.get(dep_base, dep)
        if pkg.lower() in _PREINSTALLED_PACKAGES:
            continue
        packages.add(pkg)
    return sorted(packages)


def pip_install_command(packages: list[str]) -> str | None:
    """Return a pip install command string, or None if no packages needed.

    With the pre-built sandbox image this should almost always return None.
    If it returns a command, the package is missing from the image and the
    install will likely fail (no network). Add it to docker/Dockerfile instead.
    """
    if not packages:
        return None
    escaped = [p.replace("'", "") for p in packages]
    return f"pip install --no-cache-dir --quiet {' '.join(escaped)}"


def audit_dependencies(workspace: Path) -> dict[str, list[str]]:
    """Classify all imports in workspace into builtin, preinstalled, local, and missing."""
    all_imports = scan_workspace(workspace)
    local_modules = {p.stem for p in workspace.glob("*.py")} if workspace.is_dir() else set()

    result: dict[str, list[str]] = {
        "builtin": [],
        "preinstalled": [],
        "local": [],
        "missing": [],
    }
    for mod in sorted(all_imports):
        if mod in _STDLIB_MODULES:
            result["builtin"].append(mod)
        elif mod in local_modules:
            result["local"].append(mod)
        elif mod in _PREINSTALLED_MODULES:
            result["preinstalled"].append(mod)
        else:
            result["missing"].append(mod)
    return result


def scan_workspace(workspace: Path) -> set[str]:
    """Extract all imports from Python files in the workspace."""
    all_imports: set[str] = set()
    for py_file in workspace.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8", errors="replace")
        all_imports.update(extract_imports(source))
    return all_imports
