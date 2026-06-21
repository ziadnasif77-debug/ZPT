"""Ollama connection — streaming chat completions via OpenAI-compatible API."""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from config import get_default_model, get_ollama_base_url


async def check_health() -> bool:
    base = get_ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(base)
            return r.status_code == 200
    except Exception:
        return False


async def list_models() -> list[dict]:
    base = get_ollama_base_url()
    default_model = get_default_model()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{base}/api/tags")
            r.raise_for_status()
            data = r.json()
            models_raw = data.get("models", [])
            if not models_raw and isinstance(data, list):
                models_raw = data
            models = []
            for m in models_raw:
                if isinstance(m, dict) and "name" in m:
                    models.append({"name": m["name"], "size": m.get("size", 0)})
                elif isinstance(m, str):
                    models.append({"name": m, "size": 0})
            if models:
                return models
    except Exception as exc:
        print(f"[ollama_client] Failed to fetch models: {exc}", flush=True)
    return [{"name": default_model, "size": 0}]


async def stream_chat(
    model: str,
    messages: list[dict],
) -> AsyncIterator[str]:
    base = get_ollama_base_url()
    url = f"{base}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    import json
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except Exception:
                    continue
