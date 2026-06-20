"""Sandboxed code execution via Docker — the security boundary.

If Docker is unavailable, raises SandboxUnavailable. Never falls back to subprocess.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from autodev.schemas import TestResult

if TYPE_CHECKING:
    from autodev.config import SandboxConfig


class SandboxUnavailable(RuntimeError):
    pass


class Sandbox:
    def __init__(self, config: "SandboxConfig") -> None:
        if config.backend != "docker":
            raise SandboxUnavailable(
                f"Unsupported sandbox backend '{config.backend}'. "
                "Only 'docker' is allowed — code must run in an isolated container."
            )
        try:
            import docker
        except ImportError:
            raise SandboxUnavailable(
                "The 'docker' Python package is not installed. "
                "Run: pip install docker"
            )
        try:
            self._client = docker.from_env()
            self._client.ping()
        except docker.errors.DockerException as exc:
            raise SandboxUnavailable(
                f"Docker daemon is not running or not accessible: {exc}"
            )
        self._config = config
        self._ensure_image()

    def _ensure_image(self) -> None:
        try:
            self._client.images.get(self._config.image)
        except Exception:
            self._client.images.pull(self._config.image)

    def run(
        self,
        workspace: Path,
        command: str = "python test_runner.py",
        extra_setup: str | None = None,
    ) -> TestResult:
        workspace = workspace.resolve()
        if not workspace.is_dir():
            return TestResult(
                passed=False,
                exit_code=-1,
                stderr=f"Workspace directory does not exist: {workspace}",
            )

        shell_cmd = command
        if extra_setup:
            shell_cmd = f"{extra_setup} && {command}"

        cpu_nano = int(float(self._config.cpu_limit) * 1e9)
        t0 = time.perf_counter()
        container = None
        try:
            container = self._client.containers.run(
                image=self._config.image,
                command=["bash", "-c", shell_cmd],
                volumes={
                    str(workspace): {
                        "bind": "/app",
                        "mode": "ro" if self._config.workspace_mount_readonly else "rw",
                    }
                },
                working_dir="/app",
                environment={"PYTHONPATH": "/app"},
                network_disabled=not self._config.network,
                mem_limit=self._config.memory_limit,
                nano_cpus=cpu_nano,
                pids_limit=64,
                tmpfs={"/tmp": "size=64m"},
                detach=True,
                stdout=True,
                stderr=True,
            )

            exit_info = container.wait(timeout=self._config.timeout_seconds)
            duration = time.perf_counter() - t0
            exit_code = exit_info.get("StatusCode", -1)
            stdout = container.logs(stdout=True, stderr=False).decode(
                "utf-8", errors="replace"
            )
            stderr = container.logs(stdout=False, stderr=True).decode(
                "utf-8", errors="replace"
            )

            return TestResult(
                passed=(exit_code == 0),
                exit_code=exit_code,
                stdout=stdout[:10000],
                stderr=stderr[:10000],
                test_summary="",
                duration_seconds=round(duration, 2),
            )

        except Exception as exc:
            duration = time.perf_counter() - t0
            err_name = type(exc).__name__
            if "Read timed out" in str(exc) or "timed out" in str(exc).lower():
                return TestResult(
                    passed=False,
                    exit_code=-1,
                    stderr=(
                        f"Execution timed out after {self._config.timeout_seconds}s. "
                        "The code may contain an infinite loop."
                    ),
                    duration_seconds=round(duration, 2),
                )
            return TestResult(
                passed=False,
                exit_code=-1,
                stderr=f"Sandbox error ({err_name}): {exc}",
                duration_seconds=round(duration, 2),
            )
        finally:
            if container is not None:
                try:
                    container.kill()
                except Exception:
                    pass
                try:
                    container.remove(force=True)
                except Exception:
                    pass
