"""Structured JSON logging for every LLM call."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


class ObservabilityLogger:
    def __init__(self, log_dir: str, session_id: str | None = None) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._session_id = session_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._summary_path = self._log_dir / f"{self._session_id}.jsonl"
        self._detail_path = self._log_dir / f"{self._session_id}_detail.jsonl"

    @property
    def session_id(self) -> str:
        return self._session_id

    def log_call(
        self,
        agent: str,
        model: str,
        messages: list[dict],
        response: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        cached: bool = False,
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        summary = {
            "timestamp": ts,
            "session_id": self._session_id,
            "agent": agent,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "cached": cached,
        }
        self._append(self._summary_path, summary)

        detail = {
            "timestamp": ts,
            "session_id": self._session_id,
            "agent": agent,
            "messages": messages,
            "response": response,
        }
        self._append(self._detail_path, detail)

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
