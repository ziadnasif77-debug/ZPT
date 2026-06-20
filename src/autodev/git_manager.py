"""Git integration — auto-commit snapshots and rollback on repeated errors.

Uses subprocess for git operations (no extra dependencies needed).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_SUCCESS_TAG = "autodev-success"


def _run_git(workspace: Path, *args: str) -> tuple[int, str]:
    """Run a git command in the workspace directory."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout.strip()
    except FileNotFoundError:
        return -1, "git not found"
    except subprocess.TimeoutExpired:
        return -1, "git command timed out"
    except Exception as e:
        return -1, str(e)


def init_repo(workspace: Path) -> bool:
    """Initialize a git repo in the workspace if one doesn't exist."""
    git_dir = workspace / ".git"
    if git_dir.is_dir():
        return True
    code, _ = _run_git(workspace, "init")
    if code != 0:
        return False
    _run_git(workspace, "config", "user.email", "autodev@local")
    _run_git(workspace, "config", "user.name", "autodev")
    return True


def commit_snapshot(workspace: Path, message: str) -> str | None:
    """Stage all files and commit. Returns the commit hash or None on failure."""
    if not init_repo(workspace):
        return None

    _run_git(workspace, "add", "-A")

    code, status = _run_git(workspace, "status", "--porcelain")
    if code != 0 or not status.strip():
        return None

    code, _ = _run_git(workspace, "commit", "-m", message)
    if code != 0:
        return None

    code, sha = _run_git(workspace, "rev-parse", "HEAD")
    return sha if code == 0 else None


def tag_success(workspace: Path, iteration: int) -> None:
    """Tag the current commit as a successful build."""
    tag = f"{_SUCCESS_TAG}-{iteration}"
    _run_git(workspace, "tag", "-f", tag)
    _run_git(workspace, "tag", "-f", _SUCCESS_TAG)


def get_last_stable_commit(workspace: Path) -> str | None:
    """Get the commit hash of the last successful build."""
    code, sha = _run_git(workspace, "rev-parse", _SUCCESS_TAG)
    if code == 0 and sha:
        return sha
    return None


def rollback_to_last_success(workspace: Path) -> bool:
    """Reset the workspace to the last successful commit."""
    stable = get_last_stable_commit(workspace)
    if not stable:
        print("[GIT] No stable commit to rollback to", flush=True)
        return False

    code, _ = _run_git(workspace, "reset", "--hard", stable)
    if code == 0:
        print(f"[GIT] Rolled back to {stable[:8]}", flush=True)
        return True
    return False


def get_commit_log(workspace: Path, n: int = 10) -> list[str]:
    """Get the last N commit messages."""
    code, output = _run_git(workspace, "log", f"--oneline", f"-{n}")
    if code != 0:
        return []
    return [line for line in output.split("\n") if line.strip()]
