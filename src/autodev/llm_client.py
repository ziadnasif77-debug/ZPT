"""Single LLM abstraction — every agent calls the model through here."""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from autodev.observability import ObservabilityLogger

if TYPE_CHECKING:
    from autodev.config import AppConfig

T = TypeVar("T", bound=BaseModel)

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(m.get("content", "")) for m in messages)


class LLMClient:
    def __init__(self, config: "AppConfig", logger: ObservabilityLogger) -> None:
        self._config = config
        self._logger = logger
        self._client = OpenAI(
            base_url=config.llm.base_url,
            api_key=config.llm.api_key,
            timeout=config.llm.timeout_seconds,
        )
        self._cache: dict[str, str] = {}

    def _cache_key(self, model: str, messages: list[dict]) -> str:
        blob = json.dumps({"model": model, "messages": messages}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def chat(
        self,
        agent: str,
        messages: list[dict],
        response_model: Type[T] | None = None,
        temperature: float = 0.2,
    ) -> str | T:
        model = self._config.resolve_model(agent)
        budget = self._config.get_token_budget(model)
        prompt_tokens = estimate_messages_tokens(messages)
        if prompt_tokens > budget:
            raise TokenBudgetExceeded(
                f"Prompt ({prompt_tokens} est. tokens) exceeds budget ({budget}) for model {model}"
            )

        cache_key = self._cache_key(model, messages)
        if self._config.llm.cache_enabled and cache_key in self._cache:
            raw = self._cache[cache_key]
            self._logger.log_call(
                agent=agent,
                model=model,
                messages=messages,
                response=raw,
                prompt_tokens=prompt_tokens,
                completion_tokens=estimate_tokens(raw),
                latency_ms=0,
                cached=True,
            )
        else:
            t0 = time.perf_counter()
            resp = self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            raw = resp.choices[0].message.content or ""
            completion_tokens = estimate_tokens(raw)

            if resp.usage:
                prompt_tokens = resp.usage.prompt_tokens
                completion_tokens = resp.usage.completion_tokens

            self._logger.log_call(
                agent=agent,
                model=model,
                messages=messages,
                response=raw,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=round(latency_ms, 1),
                cached=False,
            )
            if self._config.llm.cache_enabled:
                self._cache[cache_key] = raw

        if response_model is not None:
            return _parse_model(raw, response_model)
        return raw


def _parse_model(raw: str, model_cls: Type[T]) -> T:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # drop opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return model_cls.model_validate_json(text)


class TokenBudgetExceeded(Exception):
    pass
