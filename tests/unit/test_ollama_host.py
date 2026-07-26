from __future__ import annotations

from typing import Any

import pytest

from ecoloop.agent.ollama_host import OllamaModelBackend


def test_runtime_client_disables_redirects_and_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr("ecoloop.agent.ollama_host.AsyncClient", RecordingClient)

    OllamaModelBackend(
        host="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout_seconds=5.0,
    )

    assert captured["host"] == "http://127.0.0.1:11434"
    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False


def test_runtime_rejects_ambient_ollama_api_key_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_client(**kwargs: Any) -> None:
        pytest.fail(f"client must not be constructed with ambient auth: {sorted(kwargs)}")

    monkeypatch.setenv("OLLAMA_API_KEY", "ambient-test-value")
    monkeypatch.setattr("ecoloop.agent.ollama_host.AsyncClient", unexpected_client)

    with pytest.raises(ValueError, match="OLLAMA_API_KEY must be unset"):
        OllamaModelBackend(
            host="http://127.0.0.1:11434",
            model="qwen3:8b",
            timeout_seconds=5.0,
        )
