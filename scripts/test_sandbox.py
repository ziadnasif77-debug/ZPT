#!/usr/bin/env python3
"""Live sandbox tests — run on a machine with Docker daemon running.

Usage: python scripts/test_sandbox.py

Tests:
  1. Normal Python code executes and returns correct output
  2. Infinite loop is killed by timeout
  3. os.system / file deletion is blocked by read-only mount
  4. Network access is blocked
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autodev.config import load_config
from autodev.sandbox import Sandbox, SandboxUnavailable


def main():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")

    try:
        sb = Sandbox(cfg.sandbox)
    except SandboxUnavailable as e:
        print(f"SKIP: Docker not available — {e}")
        return

    print("Docker sandbox ready. Running tests...\n")

    # Test 1: Normal code
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "test_runner.py").write_text(
            'print("hello from sandbox")\nprint(2 + 2)\n'
        )
        result = sb.run(Path(tmp), "python test_runner.py")
        assert result.passed, f"Test 1 FAILED: {result.stderr}"
        assert "hello from sandbox" in result.stdout
        assert "4" in result.stdout
        print(f"PASS [1] Normal code — exit {result.exit_code}, {result.duration_seconds}s")

    # Test 2: Infinite loop — must timeout
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "test_runner.py").write_text("while True: pass\n")
        result = sb.run(Path(tmp), "python test_runner.py")
        assert not result.passed, "Test 2 FAILED: infinite loop should not pass"
        print(f"PASS [2] Infinite loop blocked — {result.stderr[:80]}")

    # Test 3: Malicious file deletion — blocked by read-only or container isolation
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "test_runner.py").write_text(
            'import os\n'
            'try:\n'
            '    os.remove("/etc/hostname")\n'
            '    print("DANGER: file deleted")\n'
            'except Exception as e:\n'
            '    print(f"Blocked: {e}")\n'
        )
        result = sb.run(Path(tmp), "python test_runner.py")
        assert "DANGER" not in result.stdout, "Test 3 FAILED: file deletion succeeded!"
        print(f"PASS [3] File deletion blocked — {result.stdout.strip()}")

    # Test 4: Network access blocked
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "test_runner.py").write_text(
            'import urllib.request\n'
            'try:\n'
            '    urllib.request.urlopen("http://google.com", timeout=5)\n'
            '    print("DANGER: network accessible")\n'
            'except Exception as e:\n'
            '    print(f"Blocked: {e}")\n'
        )
        result = sb.run(Path(tmp), "python test_runner.py")
        assert "DANGER" not in result.stdout, "Test 4 FAILED: network access worked!"
        print(f"PASS [4] Network access blocked")

    print("\nALL SANDBOX TESTS PASSED")


if __name__ == "__main__":
    main()
