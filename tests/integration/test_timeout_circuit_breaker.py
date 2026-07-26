"""Timeout retry, deterministic fallback, and circuit-breaker tests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from ecoloop.agent.loop import AgentHost, AgentHostConfig
from ecoloop.agent.models import ModelResponse, ToolRequest
from tests.support.agent_fakes import DirectFakeMCPClient, TimeoutModel


class SlowSequentialModel:
    """Make individually valid turns that cannot exceed the total decision budget."""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def model_name(self) -> str:
        return "explicit-slow-test-model"

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del messages, tools
        await asyncio.sleep(0.02)
        calls = (
            ToolRequest(
                name="get_current_building_state",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(name="get_constraints", arguments={"run_id": "fake-run"}),
            ToolRequest(
                name="generate_candidate_actions",
                arguments={"run_id": "fake-run"},
            ),
        )
        request = calls[min(self.call_count, len(calls) - 1)]
        self.call_count += 1
        return ModelResponse(tool_calls=[request])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timeout_retries_once_then_falls_back() -> None:
    model = TimeoutModel()
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(
            timeout_seconds=0.01,
            maximum_consecutive_failures=3,
            enable_action_cache=False,
        ),
    )
    decision = await host.decide("fake-run")
    assert decision.status == "fallback"
    assert decision.timeout_count == 2
    assert model.call_count == 2
    assert decision.trace[-1].tool_name == "request_safe_fallback"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repeated_timeouts_open_circuit_and_disable_model() -> None:
    model = TimeoutModel()
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(
            timeout_seconds=0.01,
            maximum_consecutive_failures=2,
            enable_action_cache=False,
        ),
    )
    first = await host.decide("fake-run")
    second = await host.decide("fake-run")
    calls_after_open = model.call_count
    third = await host.decide("fake-run")
    assert first.status == second.status == third.status == "fallback"
    assert host.circuit_breaker.is_open("fake-run")
    assert model.call_count == calls_after_open
    assert third.tool_rounds == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_rounds_share_one_bounded_decision_budget() -> None:
    model = SlowSequentialModel()
    host = AgentHost(
        mcp_client=DirectFakeMCPClient(),
        model=model,
        config=AgentHostConfig(
            timeout_seconds=0.03,
            maximum_tool_rounds=8,
            retry_once=True,
            enable_action_cache=False,
        ),
    )
    started = time.perf_counter()

    decision = await host.decide("fake-run")

    assert decision.status == "fallback"
    assert time.perf_counter() - started < 0.2
    assert model.call_count <= 3
    assert decision.tool_rounds < 8
