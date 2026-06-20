"""Minimal-diff patcher — applies targeted code changes without rewriting entire files.

On first attempt the developer writes complete files. On retries, only changed
portions are rewritten. This prevents the common failure mode where fixing one
bug introduces another by overwriting working code.
"""

from __future__ import annotations

import difflib
from pathlib import Path


def apply_code_preserving(
    workspace: Path,
    new_files: list[dict],
    is_retry: bool,
) -> list[str]:
    """Write files to workspace, preserving unchanged files on retries.

    On first attempt (is_retry=False): writes all files unconditionally.
    On retries: only overwrites files whose content actually changed.

    Returns a list of filenames that were actually written/modified.
    """
    modified: list[str] = []

    for f in new_files:
        fp = workspace / f["path"]
        fp.parent.mkdir(parents=True, exist_ok=True)
        new_content = f["content"]

        if is_retry and fp.exists():
            try:
                old_content = fp.read_text(encoding="utf-8")
            except Exception:
                old_content = ""

            if old_content.strip() == new_content.strip():
                continue

            if _is_subset_change(old_content, new_content):
                fp.write_text(new_content, encoding="utf-8")
                modified.append(f["path"])
            else:
                fp.write_text(new_content, encoding="utf-8")
                modified.append(f["path"])
        else:
            fp.write_text(new_content, encoding="utf-8")
            modified.append(f["path"])

    return modified


def _is_subset_change(old: str, new: str) -> bool:
    """Check if the new content is a targeted change (not a complete rewrite)."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()

    if not old_lines or not new_lines:
        return False

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    ratio = matcher.ratio()
    return ratio > 0.3


def compute_diff_summary(old_content: str, new_content: str) -> str:
    """Return a human-readable summary of what changed between two versions."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    if not diff:
        return "No changes"

    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return f"{added} lines added, {removed} lines removed"


def preserve_working_files(
    workspace: Path,
    new_files: list[dict],
    failed_files: set[str] | None = None,
) -> list[dict]:
    """On retry, only include files that were implicated in the failure.

    Files not in failed_files are preserved as-is from the workspace.
    This prevents the developer from accidentally breaking working code.
    """
    if failed_files is None:
        return new_files

    result = []
    for f in new_files:
        if f["path"] in failed_files:
            result.append(f)
        else:
            fp = workspace / f["path"]
            if fp.exists():
                try:
                    existing = fp.read_text(encoding="utf-8")
                    result.append({"path": f["path"], "content": existing})
                except Exception:
                    result.append(f)
            else:
                result.append(f)
    return result
