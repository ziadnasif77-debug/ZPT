"""Ollama connection — streaming chat completions via OpenAI-compatible API."""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from config import get_ollama_base_url


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
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{base}/api/tags")
            r.raise_for_status()
            data = r.json()
            return [
                {"name": m["name"], "size": m.get("size", 0)}
                for m in data.get("models", [])
            ]
    except Exception:
        return []


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
