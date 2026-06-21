"""Autonomous Package Manager — resolves, installs, and maintains sandbox packages.

Scans workspace imports, classifies them (skip/ready/install), auto-installs
missing packages into the Docker sandbox, and rebuilds the image when needed.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodev.config import AppConfig

_STDLIB_MODULES: set[str] = set(getattr(sys, "stdlib_module_names", set()))
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

IMPORT_TO_PIP: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "attr": "attrs",
    "magic": "python-magic",
    "serial": "pyserial",
    "gi": "PyGObject",
    "wx": "wxPython",
    "crypto": "pycryptodome",
    "Crypto": "pycryptodome",
}

_DEFAULT_PREINSTALLED: set[str] = {
    "pytest", "requests", "numpy", "pandas",
}

_PREINSTALLED_ALIASES: set[str] = {
    "np", "pd", "_pytest", "urllib3", "charset_normalizer",
    "certifi", "idna", "pytz", "dateutil",
}


@dataclass
class PackageReport:
    skip: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)


@dataclass
class InstallResult:
    package: str
    version: str = ""
    success: bool = False
    error: str = ""


@dataclass
class AuditReport:
    skip: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)
    local: list[str] = field(default_factory=list)


class AutonomousPackageManager:
    def __init__(self, config: "AppConfig | None" = None):
        self._preinstalled = set(_DEFAULT_PREINSTALLED)
        self._preinstalled_aliases = set(_PREINSTALLED_ALIASES)
        self._docker_image = "autodev-sandbox:latest"
        self._dockerfile_path = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile"

        if config:
            self._docker_image = config.sandbox.image
            pkg_config = getattr(config, "packages", None)
            if pkg_config:
                self._auto_install = pkg_config.auto_install
                self._auto_update = pkg_config.auto_update
                self._rebuild_on_install = pkg_config.rebuild_on_install
                self._audit_before_run = pkg_config.audit_before_run
            else:
                self._auto_install = True
                self._auto_update = False
                self._rebuild_on_install = True
                self._audit_before_run = True
        else:
            self._auto_install = True
            self._auto_update = False
            self._rebuild_on_install = True
            self._audit_before_run = True

        self._installed_this_session: set[str] = set()

    def resolve(self, imports: list[str], workspace: Path | None = None) -> PackageReport:
        """Classify each import as SKIP (built-in/local), READY (preinstalled), or INSTALL."""
        local_modules: set[str] = set()
        if workspace and workspace.is_dir():
            local_modules = {p.stem for p in workspace.glob("*.py")}

        report = PackageReport()

        for mod in sorted(set(imports)):
            if mod in _STDLIB_MODULES:
                report.skip.append(mod)
            elif mod in local_modules:
                report.skip.append(mod)
            elif mod in self._preinstalled or mod in self._preinstalled_aliases:
                report.ready.append(mod)
            elif IMPORT_TO_PIP.get(mod, mod) in self._preinstalled:
                report.ready.append(mod)
            elif mod in self._installed_this_session:
                report.ready.append(mod)
            else:
                report.install.append(mod)

        return report

    def install(self, package: str) -> InstallResult:
        """Install a package into the Docker sandbox image."""
        pip_name = IMPORT_TO_PIP.get(package, package)

        try:
            result = subprocess.run(
                ["docker", "run", "--rm", self._docker_image,
                 "pip", "install", "--no-cache-dir", pip_name],
                capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            return InstallResult(package=pip_name, success=False, error="Docker not found")
        except subprocess.TimeoutExpired:
            return InstallResult(package=pip_name, success=False, error="Install timed out")

        if result.returncode != 0:
            return InstallResult(
                package=pip_name, success=False,
                error=result.stderr[:500] if result.stderr else "Unknown error",
            )

        version = _extract_version(result.stdout, pip_name)

        if self._rebuild_on_install:
            self._add_to_dockerfile(pip_name)
            rebuild_ok = self._rebuild_image()
            if not rebuild_ok:
                return InstallResult(
                    package=pip_name, version=version, success=False,
                    error="Package installed but Docker rebuild failed",
                )

        self._preinstalled.add(package)
        self._installed_this_session.add(package)
        print(f"[PKG] Installed {pip_name} {version}", flush=True)
        return InstallResult(package=pip_name, version=version, success=True)

    def audit(self, workspace: Path) -> AuditReport:
        """Scan all .py files and classify every import."""
        all_imports = self._scan_imports(workspace)
        local_modules = {p.stem for p in workspace.glob("*.py")} if workspace.is_dir() else set()

        report = AuditReport()
        for mod in sorted(all_imports):
            if mod in _STDLIB_MODULES:
                report.skip.append(mod)
            elif mod in local_modules:
                report.local.append(mod)
            elif mod in self._preinstalled or mod in self._preinstalled_aliases:
                report.ready.append(mod)
            elif mod in self._installed_this_session:
                report.ready.append(mod)
            else:
                report.install.append(mod)

        return report

    def auto_resolve(self, workspace: Path) -> AuditReport:
        """Audit + auto-install missing packages if enabled."""
        report = self.audit(workspace)

        if not self._auto_install or not report.install:
            return report

        print(f"[PKG] Scanning imports...", flush=True)
        if report.skip:
            print(f"[PKG] Skip (built-in): {', '.join(report.skip[:10])}", flush=True)
        if report.ready:
            print(f"[PKG] Ready: {', '.join(report.ready)}", flush=True)

        newly_installed = []
        still_missing = []

        for mod in report.install:
            result = self.install(mod)
            if result.success:
                newly_installed.append(mod)
            else:
                still_missing.append(mod)
                print(f"[PKG] Failed to install {mod}: {result.error}", flush=True)

        report.ready.extend(newly_installed)
        report.install = still_missing

        return report

    def _scan_imports(self, workspace: Path) -> set[str]:
        """Extract all imports from Python files in workspace."""
        all_imports: set[str] = set()
        if not workspace.is_dir():
            return all_imports

        for py_file in workspace.glob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (SyntaxError, Exception):
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        all_imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    all_imports.add(node.module.split(".")[0])

        return all_imports

    def _add_to_dockerfile(self, package: str):
        """Add a package to the Dockerfile's pip install line."""
        if not self._dockerfile_path.exists():
            return

        content = self._dockerfile_path.read_text(encoding="utf-8")

        if package in content:
            return

        content = content.replace(
            "&& mkdir -p /workspace",
            f"{package} \\\n    && mkdir -p /workspace",
        )

        self._dockerfile_path.write_text(content, encoding="utf-8")
        print(f"[PKG] Added {package} to Dockerfile", flush=True)

    def _rebuild_image(self) -> bool:
        """Rebuild the Docker sandbox image."""
        docker_dir = self._dockerfile_path.parent
        if not docker_dir.exists():
            return False

        print("[PKG] Rebuilding Docker image...", flush=True)
        try:
            result = subprocess.run(
                ["docker", "build", "-t", self._docker_image, str(docker_dir)],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                print(f"[PKG] Docker image rebuilt successfully", flush=True)
                return True
            else:
                print(f"[PKG] Docker rebuild failed: {result.stderr[:300]}", flush=True)
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"[PKG] Docker rebuild error: {e}", flush=True)
            return False


def _extract_version(pip_output: str, package: str) -> str:
    """Extract installed version from pip output."""
    import re
    m = re.search(rf"Successfully installed.*{re.escape(package)}-(\S+)", pip_output)
    if m:
        return m.group(1)
    return ""
