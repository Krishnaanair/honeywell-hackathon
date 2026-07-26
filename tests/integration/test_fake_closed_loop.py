"""Explicit fake plant response exercised through genuine MCP stdio."""

from __future__ import annotations

import pytest

from ecoloop.agent.client import MCPStdioClient
from ecoloop.agent.loop import AgentHost, AgentHostConfig
from tests.integration.test_mcp_stdio import fake_server_parameters
from tests.support.agent_fakes import ScriptedModel, valid_tool_sequence


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_fake_observation_to_action_to_physical_response() -> None:
    async with MCPStdioClient(fake_server_parameters()) as client:
        before = await client.call_tool(
            "get_current_building_state",
            {"run_id": "fake-run"},
        )
        before_temperature = before["observation"]["zone_temperature_mean_c"]
        host = AgentHost(
            mcp_client=client,
            model=ScriptedModel([valid_tool_sequence()]),
            config=AgentHostConfig(enable_action_cache=False),
        )
        decision = await host.decide("fake-run", state_hint=before["observation"])
        after = await client.call_tool(
            "get_current_building_state",
            {"run_id": "fake-run"},
        )

    assert decision.status == "applied"
    assert decision.result["source"] == "explicit_test_fake"
    assert after["observation"]["observation_id"] == 2
    assert after["observation"]["cooling_setpoint_c"] == 25.0
    assert after["observation"]["zone_temperature_mean_c"] < before_temperature
