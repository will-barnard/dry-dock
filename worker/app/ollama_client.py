"""Provider-agnostic inference abstraction.

This is the seam where dry-dock will eventually grow MLX, vLLM, and cloud
backends. Today everything routes to the local Ollama HTTP API. The function
signatures here are the contract everything else codes against.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from app.config import get_settings

log = structlog.get_logger()


class InferenceProvider:
    """Abstract interface for chat + embed."""

    async def list_models(self) -> list[str]:
        raise NotImplementedError

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def embed(self, model: str, text: str) -> list[float]:
        raise NotImplementedError


class OllamaProvider(InferenceProvider):
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().ollama_base_url).rstrip("/")

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            data = r.json()
        return [m["name"] for m in data.get("models", [])]

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False,
            "options": options or {},
        }
        # Ollama returns message.tool_calls when tools are supplied AND the
        # model supports tool calling. Models that don't just ignore the field.
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            r = await client.post(f"{self.base_url}/api/chat", json=body)
            r.raise_for_status()
            return r.json()

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        body = {"model": model, "messages": messages, "stream": True, "options": options or {}}
        timeout = httpx.Timeout(None, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("ollama.bad_chunk", line=line[:120])

    async def embed(self, model: str, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self.base_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
            r.raise_for_status()
            return r.json().get("embedding", [])


# Module-level singleton picked up by runners.
_provider: InferenceProvider | None = None


def get_provider() -> InferenceProvider:
    global _provider
    if _provider is None:
        _provider = OllamaProvider()
    return _provider
