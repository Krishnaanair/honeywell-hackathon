"""Real local Ollama tool-calling smoke test over genuine MCP stdio."""

from __future__ import annotations

import os

import pytest

from ecoloop.agent.client import MCPStdioClient
from ecoloop.agent.loop import AgentHost, AgentHostConfig
from ecoloop.agent.ollama_host import OllamaModelBackend
from tests.integration.test_mcp_stdio import fake_server_parameters


@pytest.mark.real_ollama
@pytest.mark.asyncio
async def test_real_ollama_calls_mcp_backed_control_tools() -> None:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    model_name = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
    backend = OllamaModelBackend(
        host=host,
        model=model_name,
        timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
        keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE", "30m"),
    )
    try:
        installed = await backend.available_models()
    except (OSError, RuntimeError) as exc:
        pytest.skip(f"Ollama API unavailable at {host}: {exc}")
    if model_name not in installed:
        pytest.skip(
            f"configured Ollama model is not installed: {model_name}; "
            f"run `ollama pull {model_name}`"
        )

    async with MCPStdioClient(fake_server_parameters()) as client:
        state = await client.call_tool(
            "get_current_building_state",
            {"run_id": "fake-run"},
        )
        controller = AgentHost(
            mcp_client=client,
            model=backend,
            config=AgentHostConfig(
                timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
                maximum_tool_rounds=8,
                enable_action_cache=False,
            ),
        )
        decision = await controller.decide(
            "fake-run",
            state_hint=state["observation"],
        )

    names = [item.tool_name for item in decision.trace]
    assert "get_current_building_state" in names
    assert "get_constraints" in names
    assert "generate_candidate_actions" in names or "evaluate_candidate_actions" in names
    assert decision.status == "applied"
    assert names[-1] == "apply_control_action"
