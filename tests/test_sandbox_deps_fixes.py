"""Tests for the sandbox/deps bug fixes:
1. deps.py: resolve_packages skips local .py modules
2. sandbox.py: PYTHONPATH=/app set in container environment
3. reviewer.md: guidance on local module pip failures
"""

from pathlib import Path

import pytest

from autodev.deps import resolve_packages, scan_workspace, extract_imports


class TestLocalModuleFiltering:
    """BUG 1: resolve_packages must skip modules that exist as local .py files."""

    def test_skips_local_module(self, tmp_path: Path):
        (tmp_path / "prime_checker.py").write_text("def is_prime(n): pass")
        modules = {"prime_checker", "pytest", "os"}
        packages = resolve_packages(modules, workspace=tmp_path)
        assert "prime_checker" not in packages
        assert "pytest" in packages
        assert "os" not in packages  # stdlib

    def test_no_workspace_keeps_all(self):
        modules = {"prime_checker", "pytest"}
        packages = resolve_packages(modules)
        assert "prime_checker" in packages
        assert "pytest" in packages

    def test_workspace_none_keeps_all(self):
        modules = {"prime_checker", "pytest"}
        packages = resolve_packages(modules, workspace=None)
        assert "prime_checker" in packages
        assert "pytest" in packages

    def test_prime_checker_scenario(self, tmp_path: Path):
        """Full scenario: workspace has prime_checker.py and test_prime.py,
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
        assert "pytest" in packages


class TestSandboxPythonpath:
    """BUG 2: sandbox.py must set PYTHONPATH=/app in the container."""

    def test_environment_set_in_container_run(self):
        from unittest.mock import MagicMock

        from autodev.sandbox import Sandbox

        mock_config = MagicMock()
        mock_config.backend = "docker"
        mock_config.image = "python:3.11-slim"
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
    """BUG 3: reviewer.md should mention local module PYTHONPATH guidance."""

    def test_reviewer_prompt_has_pythonpath_guidance(self):
        prompt_path = Path(__file__).parent.parent / "prompts" / "reviewer.md"
        content = prompt_path.read_text()
        assert "PYTHONPATH" in content
        assert "local .py file" in content or "local module" in content
