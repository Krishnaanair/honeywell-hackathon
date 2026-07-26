"""Local-only deterministic Ollama inference adapter."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from httpx import HTTPError, TimeoutException
from ollama import AsyncClient, ResponseError

from ecoloop.agent.models import ModelResponse, ToolRequest


class LocalModelError(RuntimeError):
    """Raised for an unavailable or malformed local Ollama response."""


@runtime_checkable
class ModelBackend(Protocol):
    """Inference surface used by the host and deterministic tests."""

    @property
    def model_name(self) -> str:
        """Return the configured local model identifier."""

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        """Request one non-streaming tool-calling turn."""


class OllamaModelBackend:
    """Call a loopback Ollama API with deterministic tool-calling options."""

    def __init__(
        self,
        *,
        host: str,
        model: str,
        timeout_seconds: float,
        keep_alive: str = "30m",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        _validate_local_ollama_host(host)
        if os.environ.get("OLLAMA_API_KEY"):
            raise ValueError(
                "OLLAMA_API_KEY must be unset; EcoLoop permits only unauthenticated "
                "loopback Ollama inference"
            )
        if not model.strip():
            raise ValueError("model must not be empty")
        self._model = model
        self._keep_alive = keep_alive
        self._client = AsyncClient(
            host=host,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    @property
    def model_name(self) -> str:
        """Return the configured local Ollama model name."""

        return self._model

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        """Request a deterministic turn and discard any hidden-thinking field."""

        try:
            response = await self._client.chat(
                model=self._model,
                messages=list(messages),
                tools=list(tools),
                stream=False,
                think=False,
                keep_alive=self._keep_alive,
                options={"temperature": 0, "seed": 0},
            )
        except TimeoutException as exc:
            raise TimeoutError(f"local Ollama request timed out: {exc}") from exc
        except (ResponseError, HTTPError) as exc:
            raise LocalModelError(f"local Ollama request failed: {exc}") from exc
        if not hasattr(response, "message"):
            raise LocalModelError("Ollama returned no message")
        message = response.message
        tool_calls = [
            ToolRequest(
                name=call.function.name,
                arguments=dict(call.function.arguments),
            )
            for call in (message.tool_calls or ())
        ]
        return ModelResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            model=response.model or self._model,
        )

    async def available_models(self) -> set[str]:
        """Return locally installed model names from the loopback API."""

        try:
            response = await self._client.list()
        except (ResponseError, HTTPError) as exc:
            raise LocalModelError(f"local Ollama model listing failed: {exc}") from exc
        names: set[str] = set()
        for model in response.models:
            if model.model:
                names.add(model.model)
        return names


def _validate_local_ollama_host(host: str) -> None:
    parsed = urlparse(host)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("OLLAMA_HOST must use http or https")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("OLLAMA_HOST must target a loopback address")
    if parsed.username or parsed.password:
        raise ValueError("OLLAMA_HOST must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("OLLAMA_HOST must not contain a path, query, or fragment")
