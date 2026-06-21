"""Verifier — runs tests and returns structured pass/fail results.

Runs the test suite in the sandbox (or locally) and parses output into
a structured result showing exactly which tests passed and failed.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodev.sandbox import Sandbox


@dataclass
class VerifyResult:
    all_passed: bool = False
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    total: int = 0


class Verifier:
    def __init__(self, sandbox: "Sandbox | None" = None):
        self._sandbox = sandbox

    def run(self, workspace: Path) -> VerifyResult:
        if self._sandbox:
            return self._run_sandbox(workspace)
        return self._run_local(workspace)

    def _run_sandbox(self, workspace: Path) -> VerifyResult:
        result = self._sandbox.run(
            workspace=workspace,
            command="python test_runner.py",
        )
        return self._parse_output(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
        )

    def _run_local(self, workspace: Path) -> VerifyResult:
        test_file = workspace / "test_runner.py"
        if not test_file.exists():
            return VerifyResult(
                all_passed=False,
                errors=["test_runner.py not found"],
            )

        try:
            proc = subprocess.run(
                ["python", str(test_file)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(workspace),
            )
            return self._parse_output(
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return VerifyResult(
                all_passed=False,
                errors=["Test execution timed out (30s)"],
                exit_code=-1,
            )
        except FileNotFoundError:
            return VerifyResult(
                all_passed=False,
                errors=["Python interpreter not found"],
                exit_code=-1,
            )

    def _parse_output(self, stdout: str, stderr: str, exit_code: int) -> VerifyResult:
        result = VerifyResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )

        combined = stdout + "\n" + stderr

        for m in re.finditer(r"(test_\w+)\s*\.\.\.\s*(ok|FAIL|ERROR)", combined):
            test_name = m.group(1)
            status = m.group(2)
            if status == "ok":
                result.passed.append(test_name)
            elif status == "FAIL":
                result.failed.append(test_name)
            elif status == "ERROR":
                result.errors.append(test_name)

        for m in re.finditer(r"(test_\w+)\s+(PASSED|FAILED)", combined):
            test_name = m.group(1)
            status = m.group(2)
            if status == "PASSED" and test_name not in result.passed:
                result.passed.append(test_name)
            elif status == "FAILED" and test_name not in result.failed:
                result.failed.append(test_name)

        if not result.passed and not result.failed:
            self._parse_unittest_output(combined, result)

        if not result.passed and not result.failed and not result.errors:
            self._parse_custom_output(combined, result)

        result.total = len(result.passed) + len(result.failed) + len(result.errors)

        if exit_code == 0 and not result.failed and not result.errors:
            result.all_passed = True
        elif result.total > 0 and not result.failed and not result.errors:
            result.all_passed = True

        return result

    def _parse_unittest_output(self, text: str, result: VerifyResult):
        m = re.search(r"Ran (\d+) test", text)
        if m:
            total = int(m.group(1))
            if "OK" in text and "FAILED" not in text:
                result.passed = [f"test_{i}" for i in range(total)]
            else:
                fail_m = re.search(r"failures=(\d+)", text)
                err_m = re.search(r"errors=(\d+)", text)
                n_fail = int(fail_m.group(1)) if fail_m else 0
                n_err = int(err_m.group(1)) if err_m else 0
                n_pass = total - n_fail - n_err

                for i in range(n_pass):
                    result.passed.append(f"test_passed_{i}")
                for i in range(n_fail):
                    result.failed.append(f"test_failed_{i}")
                for i in range(n_err):
                    result.errors.append(f"test_error_{i}")

    def _parse_custom_output(self, text: str, result: VerifyResult):
        for m in re.finditer(r"(?:PASS|OK|✓|✅)\s*[:\-]?\s*(test_\w+|[\w\s]+test)", text, re.I):
            result.passed.append(m.group(1).strip())
        for m in re.finditer(r"(?:FAIL|✗|❌)\s*[:\-]?\s*(test_\w+|[\w\s]+test)", text, re.I):
            result.failed.append(m.group(1).strip())

        pass_m = re.search(r"(\d+)\s*(?:passed|pass|ok)", text, re.I)
        fail_m = re.search(r"(\d+)\s*(?:failed|fail)", text, re.I)
        if pass_m and not result.passed:
            for i in range(int(pass_m.group(1))):
                result.passed.append(f"test_{i}")
        if fail_m and not result.failed:
            for i in range(int(fail_m.group(1))):
                result.failed.append(f"failed_test_{i}")


def quick_verify(workspace: Path) -> VerifyResult:
    return Verifier().run(workspace)
