"""Single LLM abstraction — every agent calls the model through here."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from autodev.observability import ObservabilityLogger

if TYPE_CHECKING:
    from autodev.config import AppConfig

T = TypeVar("T", bound=BaseModel)

_CHARS_PER_TOKEN = 4
_MAX_PARSE_RETRIES = 2
_JSON_HINT = "\n\nIMPORTANT: Return ONLY valid JSON. No markdown fences, no explanation, no text before or after the JSON object."


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

    def _call_llm(
        self,
        agent: str,
        model: str,
        messages: list[dict],
        temperature: float,
    ) -> str:
        prompt_tokens = estimate_messages_tokens(messages)

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
            return raw

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

        return raw

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

        if response_model is None:
            return self._call_llm(agent, model, messages, temperature)

        msgs = _inject_json_hint(messages)
        last_error: Exception | None = None

        for attempt in range(_MAX_PARSE_RETRIES + 1):
            raw = self._call_llm(agent, model, msgs, temperature)

            try:
                return _parse_model(raw, response_model)
            except (JSONParseError, Exception) as exc:
                last_error = exc
                self._cache.pop(self._cache_key(model, msgs), None)
                if attempt < _MAX_PARSE_RETRIES:
                    msgs = _append_retry_feedback(msgs, raw, exc)

        raise JSONParseError(
            f"Failed to parse {response_model.__name__} after {_MAX_PARSE_RETRIES + 1} attempts. "
            f"Last error: {last_error}"
        )


def _inject_json_hint(messages: list[dict]) -> list[dict]:
    """Add JSON-only instruction to the system prompt without mutating the original."""
    msgs = [m.copy() for m in messages]
    for m in msgs:
        if m["role"] == "system":
            if _JSON_HINT.strip() not in m["content"]:
                m["content"] += _JSON_HINT
            return msgs
    msgs.insert(0, {"role": "system", "content": _JSON_HINT.strip()})
    return msgs


def _append_retry_feedback(messages: list[dict], raw: str, error: Exception) -> list[dict]:
    """Add a user message explaining the parse failure so the LLM can fix it."""
    msgs = [m.copy() for m in messages]
    feedback = (
        f"Your previous response was not valid JSON. Error: {error}\n\n"
        f"Your response started with: {raw[:200]!r}\n\n"
        "Please respond with ONLY a valid JSON object. "
        "No markdown code fences, no explanatory text, just the raw JSON."
    )
    msgs.append({"role": "user", "content": feedback})
    return msgs


def _fix_triple_quotes(text: str) -> str:
    """Replace Python-style triple-quoted strings with properly escaped JSON strings.

    LLMs sometimes output: "content": \"""some code\nhere\"""
    which is Python syntax, not valid JSON. Convert to: "content": "some code\\nhere"
    """
    result = []
    i = 0
    while i < len(text):
        for quote in ('"""', "'''"):
            if text[i:i+3] == quote:
                end = text.find(quote, i + 3)
                if end == -1:
                    inner = text[i+3:]
                    i = len(text)
                else:
                    inner = text[i+3:end]
                    i = end + 3
                escaped = inner.replace("\\", "\\\\")
                escaped = escaped.replace('"', '\\"')
                escaped = escaped.replace("\n", "\\n")
                escaped = escaped.replace("\r", "\\r")
                escaped = escaped.replace("\t", "\\t")
                result.append('"')
                result.append(escaped)
                result.append('"')
                break
        else:
            result.append(text[i])
            i += 1
    return "".join(result)


def _extract_json(raw: str) -> str:
    """Best-effort extraction of a JSON object from messy LLM output."""
    text = raw.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    text = re.sub(r"^```(?:json|JSON)?\s*\n?", "", text)
    text = re.sub(r"\n?\s*```\s*$", "", text)
    text = text.strip()

    # If there are still fences embedded mid-string, try to extract between them
    fence_match = re.search(r"```(?:json|JSON)?\s*\n([\s\S]*?)\n\s*```", text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Fix Python triple-quoted strings before extracting braces
    if '"""' in text or "'''" in text:
        text = _fix_triple_quotes(text)

    # Strip any text before the first { and after the last }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        text = text[first_brace : last_brace + 1]

    return text


def _parse_model(raw: str, model_cls: Type[T]) -> T:
    """Parse LLM output into a Pydantic model, with progressive cleanup."""
    text = raw.strip()

    # Attempt 1: try raw text directly (fast path for well-behaved models)
    try:
        return model_cls.model_validate_json(text)
    except Exception:
        pass

    # Attempt 2: extract JSON from markdown/surrounding text
    cleaned = _extract_json(text)
    try:
        return model_cls.model_validate_json(cleaned)
    except Exception:
        pass

    # Attempt 3: parse as Python dict (handles single quotes, trailing commas, etc.)
    try:
        parsed = json.loads(cleaned)
        return model_cls.model_validate(parsed)
    except Exception:
        pass

    # Attempt 4: try fixing common issues (trailing commas, unquoted keys)
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", cleaned)  # trailing commas
        parsed = json.loads(fixed)
        return model_cls.model_validate(parsed)
    except Exception:
        pass

    # Attempt 5: fix Python triple-quoted strings ("""...""" or '''...''') in JSON
    if '"""' in cleaned or "'''" in cleaned:
        try:
            fixed = _fix_triple_quotes(cleaned)
            fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
            parsed = json.loads(fixed)
            return model_cls.model_validate(parsed)
        except Exception:
            pass

    raise JSONParseError(
        f"Cannot parse LLM response as {model_cls.__name__}.\n"
        f"Cleaned text: {cleaned[:500]!r}"
    )


class TokenBudgetExceeded(Exception):
    pass


class JSONParseError(Exception):
    pass
