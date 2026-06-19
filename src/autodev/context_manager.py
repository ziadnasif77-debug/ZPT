"""Context manager — selects relevant files and enforces token budget.

Never silently truncates. When over budget, summarizes via LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from autodev.llm_client import estimate_tokens

if TYPE_CHECKING:
    from autodev.config import AppConfig
    from autodev.llm_client import LLMClient

_PROMPT_OVERHEAD_TOKENS = 1500
_RESPONSE_RESERVE_RATIO = 0.30
_SUMMARY_TARGET_CHARS = 800


class ContextManager:
    def __init__(self, config: "AppConfig", llm: "LLMClient") -> None:
        self._config = config
        self._llm = llm

    def build_context(
        self,
        agent: str,
        request: str,
        workspace: Path,
        extra_files: dict[str, str] | None = None,
    ) -> str:
        if not self._config.context.relevant_files_only:
            return self._dump_all(workspace, extra_files)

        model = self._config.resolve_model(agent)
        total_budget = self._config.get_token_budget(model)
        available = int(total_budget * (1 - _RESPONSE_RESERVE_RATIO)) - _PROMPT_OVERHEAD_TOKENS

        files = self._collect_files(workspace, extra_files)
        ranked = self._rank_by_relevance(files, request)

        blocks: list[str] = []
        used = 0
        deferred: list[tuple[str, str]] = []

        for name, content in ranked:
            tokens = estimate_tokens(content)
            if used + tokens <= available:
                blocks.append(self._format_file(name, content))
                used += tokens
            else:
                deferred.append((name, content))

        if deferred and self._config.context.summarize_when_over_budget:
            for name, content in deferred:
                summary = self._summarize(agent, name, content)
                summary_tokens = estimate_tokens(summary)
                if used + summary_tokens <= available:
                    blocks.append(f"### {name} (summarized)\n{summary}")
                    used += summary_tokens

        return "\n\n".join(blocks) if blocks else "(no relevant files in workspace)"

    def _collect_files(
        self, workspace: Path, extra: dict[str, str] | None
    ) -> dict[str, str]:
        files: dict[str, str] = {}
        if workspace.exists():
            for p in sorted(workspace.rglob("*")):
                if p.is_file() and p.suffix in (".py", ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh"):
                    try:
                        files[str(p.relative_to(workspace))] = p.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except (OSError, UnicodeDecodeError):
                        pass
        if extra:
            files.update(extra)
        return files

    def _rank_by_relevance(
        self, files: dict[str, str], request: str
    ) -> list[tuple[str, str]]:
        request_lower = request.lower()
        keywords = set(request_lower.split())

        def score(name: str, content: str) -> float:
            s = 0.0
            name_lower = name.lower()
            content_lower = content.lower()
            for kw in keywords:
                if kw in name_lower:
                    s += 10.0
                if kw in content_lower:
                    s += 1.0
            if name.endswith(".py"):
                s += 2.0
            s -= len(content) / 50000.0
            return s

        items = [(name, content) for name, content in files.items()]
        items.sort(key=lambda x: score(x[0], x[1]), reverse=True)
        return items

    def _summarize(self, agent: str, filename: str, content: str) -> str:
        messages = [
            {
                "role": "user",
                "content": (
                    f"Summarize this file in under {_SUMMARY_TARGET_CHARS} characters. "
                    f"Focus on public functions, classes, and their purpose.\n\n"
                    f"File: {filename}\n```\n{content[:8000]}\n```"
                ),
            }
        ]
        return self._llm.chat(agent=agent, messages=messages)

    @staticmethod
    def _format_file(name: str, content: str) -> str:
        ext = Path(name).suffix.lstrip(".")
        lang = ext if ext else "text"
        return f"### {name}\n```{lang}\n{content}\n```"

    @staticmethod
    def _dump_all(
        workspace: Path, extra: dict[str, str] | None
    ) -> str:
        blocks: list[str] = []
        if workspace.exists():
            for p in sorted(workspace.rglob("*.py")):
                if p.is_file():
                    blocks.append(
                        f"### {p.relative_to(workspace)}\n```python\n"
                        f"{p.read_text(encoding='utf-8', errors='replace')}\n```"
                    )
        if extra:
            for name, content in extra.items():
                blocks.append(f"### {name}\n```\n{content}\n```")
        return "\n\n".join(blocks) if blocks else "(empty workspace)"
