"""Tests for the sandbox/deps system:
1. deps.py: resolve_packages skips local .py modules and pre-installed packages
2. sandbox.py: PYTHONPATH=/app set in container environment
3. reviewer.md: guidance on local module pip failures
"""

from pathlib import Path

import pytest

from autodev.deps import check_imports, resolve_packages, scan_workspace, extract_imports


class TestLocalModuleFiltering:
    """resolve_packages must skip modules that exist as local .py files."""

    def test_skips_local_module(self, tmp_path: Path):
        (tmp_path / "prime_checker.py").write_text("def is_prime(n): pass")
        modules = {"prime_checker", "os"}
        packages = resolve_packages(modules, workspace=tmp_path)
        assert "prime_checker" not in packages
        assert "os" not in packages  # stdlib

    def test_no_workspace_keeps_non_preinstalled(self):
        modules = {"prime_checker", "somelib"}
        packages = resolve_packages(modules)
        assert "prime_checker" in packages
        assert "somelib" in packages

    def test_workspace_none_keeps_non_preinstalled(self):
        modules = {"prime_checker", "somelib"}
        packages = resolve_packages(modules, workspace=None)
        assert "prime_checker" in packages

    def test_prime_checker_scenario(self, tmp_path: Path):
        """Full scenario: workspace has prime_checker.py and test_runner.py,
        test imports prime_checker — it should NOT become a pip package."""
        (tmp_path / "prime_checker.py").write_text(
            "def is_prime(n):\n    if n < 2: return False\n    return all(n % i for i in range(2, int(n**0.5)+1))\n"
        )
        (tmp_path / "test_runner.py").write_text(
            "import pytest\nfrom prime_checker import is_prime\n"
            "def test_prime():\n    assert is_prime(7)\n"
        )

        imports = scan_workspace(tmp_path)
        assert "prime_checker" in imports
        assert "pytest" in imports

        packages = resolve_packages(imports, workspace=tmp_path)
        assert "prime_checker" not in packages
        assert "test_runner" not in packages
        assert "pytest" not in packages  # pre-installed in sandbox image


class TestPreinstalledFiltering:
    """Pre-installed packages (pytest, requests, numpy, pandas) must not be pip installed."""

    def test_preinstalled_packages_filtered(self):
        modules = {"pytest", "requests", "numpy", "pandas"}
        packages = resolve_packages(modules)
        assert packages == []

    def test_preinstalled_not_in_check_imports_missing(self, tmp_path: Path):
        modules = {"pytest", "requests", "numpy", "pandas", "os", "sys"}
        missing = check_imports(modules, workspace=tmp_path)
        assert missing == []

    def test_unknown_module_is_missing(self, tmp_path: Path):
        modules = {"pytest", "some_unknown_lib"}
        missing = check_imports(modules, workspace=tmp_path)
        assert "some_unknown_lib" in missing

    def test_declared_deps_skip_preinstalled(self):
        packages = resolve_packages(set(), declared_deps=["pytest", "requests", "flask"])
        assert "pytest" not in packages
        assert "requests" not in packages
        assert "flask" in packages


class TestSandboxPythonpath:
    """sandbox.py must set PYTHONPATH=/app in the container."""

    def test_environment_set_in_container_run(self):
        from unittest.mock import MagicMock

        from autodev.sandbox import Sandbox

        mock_config = MagicMock()
        mock_config.backend = "docker"
        mock_config.image = "autodev-sandbox:latest"
        mock_config.cpu_limit = "1.0"
        mock_config.memory_limit = "512m"
        mock_config.timeout_seconds = 30
        mock_config.network = False
        mock_config.workspace_mount_readonly = False

        mock_client = MagicMock()
        sandbox = object.__new__(Sandbox)
        sandbox._client = mock_client
        sandbox._config = mock_config

        mock_container = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b""

        workspace = Path("/tmp/test_workspace")
        workspace.mkdir(exist_ok=True)

        sandbox.run(workspace, command="python test.py")

        call_kwargs = mock_client.containers.run.call_args
        assert call_kwargs is not None
        env = call_kwargs.kwargs.get("environment") or call_kwargs[1].get("environment")
        assert env is not None, "environment kwarg not passed to containers.run"
        assert env.get("PYTHONPATH") == "/app", f"PYTHONPATH not set correctly: {env}"


class TestReviewerPrompt:
    """reviewer.md should mention local module PYTHONPATH guidance."""

    def test_reviewer_prompt_has_pythonpath_guidance(self):
        prompt_path = Path(__file__).parent.parent / "prompts" / "reviewer.md"
        content = prompt_path.read_text()
        assert "PYTHONPATH" in content
        assert "local .py file" in content or "local module" in content
