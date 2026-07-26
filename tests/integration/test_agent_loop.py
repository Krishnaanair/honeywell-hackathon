"""Required-tool enforcement and corrective-reprompt integration tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ecoloop.agent.audit import SQLiteDecisionSink
from ecoloop.agent.loop import AgentHost, AgentHostConfig
from ecoloop.agent.models import ModelResponse, ToolRequest
from ecoloop.agent.reliability import ActionCache
from ecoloop.db.store import SQLiteStore
from ecoloop.schemas import RunStatus, RunType
from tests.support.agent_fakes import (
    DirectFakeMCPClient,
    ScriptedModel,
    valid_tool_sequence,
)
from tests.unit._factories import observation_input


class RejectingApplyClient(DirectFakeMCPClient):
    """Return one explicit safety rejection while keeping fallback available."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "apply_control_action":
            self.calls.append((name, arguments))
            return {
                "success": False,
                "status": "rejected",
                "terminal": None,
                "validation": {"accepted": False},
            }
        return await super().call_tool(name, arguments)


class StateWithoutActionGenerationClient(DirectFakeMCPClient):
    """Expose authoritative generation only through the constraint response."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await super().call_tool(name, arguments)
        if name != "get_current_building_state":
            return result
        observation = dict(result["observation"])
        observation.pop("action_generation", None)
        return {**result, "observation": observation}


class StagedSchemaModel:
    """Follow one required tool at a time and record the schemas offered."""

    def __init__(self) -> None:
        self.call_count = 0
        self.offered: list[set[str]] = []

    @property
    def model_name(self) -> str:
        return "staged-schema-test-model"

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del messages
        self.offered.append(
            {
                str(tool["function"]["name"])
                for tool in tools
                if isinstance(tool.get("function"), Mapping)
            }
        )
        responses = (
            ToolRequest(
                name="get_current_building_state",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(name="get_constraints", arguments={"run_id": "fake-run"}),
            ToolRequest(
                name="generate_candidate_actions",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(
                name="apply_control_action",
                arguments={
                    "run_id": "fake-run",
                    "observation_id": 1,
                    "action": {
                        "heating_setpoint_c": 20.0,
                        "cooling_setpoint_c": 25.0,
                        "hold_minutes": 60,
                        "action_generation": 1,
                        "reason_code": "ENERGY_OPTIMIZATION",
                        "explanation": "Use the lowest-scored bounded candidate.",
                    },
                },
            ),
        )
        request = responses[self.call_count]
        self.call_count += 1
        return ModelResponse(model=self.model_name, tool_calls=[request])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_exposes_only_tools_that_advance_the_protocol_stage() -> None:
    model = StagedSchemaModel()
    host = AgentHost(
        mcp_client=DirectFakeMCPClient(),
        model=model,
        config=AgentHostConfig(enable_action_cache=False),
    )

    decision = await host.decide("fake-run")

    assert decision.status == "applied"
    assert model.offered[0] == {
        "get_current_building_state",
        "get_constraints",
        "generate_candidate_actions",
        "evaluate_candidate_actions",
        "apply_control_action",
        "request_safe_fallback",
    }
    assert "get_current_building_state" not in model.offered[1]
    assert "get_constraints" in model.offered[1]
    assert "apply_control_action" not in model.offered[1]
    assert model.offered[2] >= {
        "generate_candidate_actions",
        "evaluate_candidate_actions",
    }
    assert "get_constraints" not in model.offered[2]
    assert model.offered[3] == {
        "apply_control_action",
        "request_safe_fallback",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_rejects_early_apply_then_corrects_once() -> None:
    early_apply = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="apply_control_action",
                arguments={
                    "run_id": "fake-run",
                    "observation_id": 1,
                    "action": {
                        "heating_setpoint_c": 20.0,
                        "cooling_setpoint_c": 25.0,
                        "hold_minutes": 60,
                        "action_generation": 1,
                        "reason_code": "COMFORT_PROTECTION",
                        "explanation": "Too early.",
                    },
                },
            )
        ]
    )
    model = ScriptedModel([early_apply, valid_tool_sequence()])
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=False),
    )
    decision = await host.decide("fake-run")
    assert decision.status == "applied"
    assert decision.corrective_reprompt_used is True
    assert model.call_count == 2
    assert [name for name, _ in client.calls] == [
        "get_current_building_state",
        "get_constraints",
        "generate_candidate_actions",
        "apply_control_action",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integer_json_setpoints_match_float_generated_candidate() -> None:
    response = valid_tool_sequence()
    action = response.tool_calls[-1].arguments["action"]
    assert isinstance(action, dict)
    action["heating_setpoint_c"] = 20
    action["cooling_setpoint_c"] = 25
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=ScriptedModel([response]),
        config=AgentHostConfig(enable_action_cache=False),
    )

    decision = await host.decide("fake-run")

    assert decision.status == "applied"
    assert decision.trace[-1].tool_name == "apply_control_action"
    assert client.service.applied[0]["heating_setpoint_c"] == 20.0
    assert client.service.applied[0]["cooling_setpoint_c"] == 25.0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flat_local_model_apply_arguments_are_narrowly_normalized() -> None:
    candidate_tools = ModelResponse(
        tool_calls=valid_tool_sequence().tool_calls[:3],
    )
    flat_apply = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="apply_control_action",
                arguments={
                    "observation_id": 1,
                    "candidate_id": "model-label",
                    "heating_setpoint_c": 20,
                    "cooling_setpoint_c": 25,
                    "hold_minutes": 60,
                    "action_generation": 1,
                    "reason_code": "ENERGY_OPTIMIZATION",
                    "explanation": "Use the lowest-scored bounded candidate.",
                },
            )
        ]
    )
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=ScriptedModel([candidate_tools, flat_apply]),
        config=AgentHostConfig(enable_action_cache=False),
    )

    decision = await host.decide("fake-run")

    assert decision.status == "applied"
    name, arguments = client.calls[-1]
    assert name == "apply_control_action"
    assert arguments["run_id"] == "fake-run"
    assert arguments["observation_id"] == 1
    assert arguments["action"]["heating_setpoint_c"] == 20
    assert "heating_setpoint_c" not in {key for key in arguments if key != "action"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_falls_back_after_second_missing_tool_attempt() -> None:
    model = ScriptedModel([ModelResponse(), ModelResponse()])
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=False),
    )
    decision = await host.decide("fake-run")
    assert decision.status == "fallback"
    assert decision.corrective_reprompt_used is True
    assert decision.fallback_status == "tool_sequence_rejected"
    assert [name for name, _ in client.calls] == [
        "get_current_building_state",
        "get_constraints",
        "request_safe_fallback",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_reprompts_once_before_fallback_when_ranked_candidate_exists() -> None:
    candidate_then_fallback = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="get_current_building_state",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(name="get_constraints", arguments={"run_id": "fake-run"}),
            ToolRequest(
                name="generate_candidate_actions",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(
                name="request_safe_fallback",
                arguments={"run_id": "fake-run", "observation_id": 1},
            ),
        ]
    )
    selected_candidate = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="apply_control_action",
                arguments={
                    "run_id": "fake-run",
                    "observation_id": 1,
                    "action": {
                        "heating_setpoint_c": 20.0,
                        "cooling_setpoint_c": 25.0,
                        "hold_minutes": 60,
                        "action_generation": 1,
                        "reason_code": "ENERGY_OPTIMIZATION",
                        "explanation": "Use the lowest scored bounded candidate.",
                    },
                },
            )
        ]
    )
    model = ScriptedModel([candidate_then_fallback, selected_candidate])
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=False),
    )

    decision = await host.decide("fake-run")

    assert decision.status == "applied"
    assert model.call_count == 2
    assert [name for name, _ in client.calls] == [
        "get_current_building_state",
        "get_constraints",
        "generate_candidate_actions",
        "apply_control_action",
    ]
    assert not any(name == "request_safe_fallback" for name, _ in client.calls)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_reprompts_empty_model_turn_when_ranked_candidate_exists() -> None:
    candidate_tools = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="get_current_building_state",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(name="get_constraints", arguments={"run_id": "fake-run"}),
            ToolRequest(
                name="generate_candidate_actions",
                arguments={"run_id": "fake-run"},
            ),
        ]
    )
    selected_candidate = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="apply_control_action",
                arguments={
                    "run_id": "fake-run",
                    "observation_id": 1,
                    "action": {
                        "heating_setpoint_c": 20.0,
                        "cooling_setpoint_c": 25.0,
                        "hold_minutes": 60,
                        "action_generation": 1,
                        "reason_code": "COMFORT_PROTECTION",
                        "explanation": "Use the best bounded comfort candidate.",
                    },
                },
            )
        ]
    )
    model = ScriptedModel([candidate_tools, ModelResponse(), selected_candidate])
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=False),
    )

    decision = await host.decide("fake-run")

    assert decision.status == "applied"
    assert model.call_count == 3
    assert [name for name, _ in client.calls] == [
        "get_current_building_state",
        "get_constraints",
        "generate_candidate_actions",
        "apply_control_action",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_honors_repeated_fallback_after_candidate_selection_reprompt() -> None:
    candidate_then_fallback = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="get_current_building_state",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(name="get_constraints", arguments={"run_id": "fake-run"}),
            ToolRequest(
                name="generate_candidate_actions",
                arguments={"run_id": "fake-run"},
            ),
            ToolRequest(
                name="request_safe_fallback",
                arguments={"run_id": "fake-run", "observation_id": 1},
            ),
        ]
    )
    repeated_fallback = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="request_safe_fallback",
                arguments={"run_id": "fake-run", "observation_id": 1},
            )
        ]
    )
    model = ScriptedModel([candidate_then_fallback, repeated_fallback])
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=False),
    )

    decision = await host.decide("fake-run")

    assert decision.status == "fallback"
    assert model.call_count == 2
    assert host.circuit_breaker.failure_count("fake-run") == 0
    assert decision.fallback_status == "model_requested"
    assert [name for name, _ in client.calls] == [
        "get_current_building_state",
        "get_constraints",
        "generate_candidate_actions",
        "request_safe_fallback",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_run_id_is_never_sent_to_mcp_service() -> None:
    wrong = ModelResponse(
        tool_calls=[
            ToolRequest(
                name="get_current_building_state",
                arguments={"run_id": "another-run"},
            )
        ]
    )
    model = ScriptedModel([wrong, wrong])
    client = DirectFakeMCPClient()
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=False),
    )
    decision = await host.decide("fake-run")
    assert decision.status == "fallback"
    assert all(arguments.get("run_id") == "fake-run" for _, arguments in client.calls)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nearly_identical_state_reuses_candidate_without_inference() -> None:
    client = DirectFakeMCPClient()
    cache = ActionCache()
    state_hint = dict(client.service.state)
    cache.put(
        "fake-run",
        state_hint,
        {
            "heating_setpoint_c": 20.0,
            "cooling_setpoint_c": 25.0,
            "hold_minutes": 60,
        },
    )
    model = ScriptedModel([valid_tool_sequence()])
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=True),
        action_cache=cache,
    )
    decision = await host.decide("fake-run", state_hint=state_hint)
    assert decision.status == "applied"
    assert decision.cache_hit is True
    assert model.call_count == 0
    assert [name for name, _ in client.calls] == [
        "get_current_building_state",
        "get_constraints",
        "evaluate_candidate_actions",
        "apply_control_action",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cached_action_uses_authoritative_advanced_generation() -> None:
    client = StateWithoutActionGenerationClient()
    client.service.state["action_generation"] = 7
    state_hint = dict(client.service.state)
    state_hint.pop("action_generation")
    cache = ActionCache()
    cache.put(
        "fake-run",
        state_hint,
        {
            "heating_setpoint_c": 20.0,
            "cooling_setpoint_c": 25.0,
            "hold_minutes": 60,
        },
    )
    model = ScriptedModel([valid_tool_sequence()])
    host = AgentHost(
        mcp_client=client,
        model=model,
        config=AgentHostConfig(enable_action_cache=True),
        action_cache=cache,
    )

    decision = await host.decide("fake-run", state_hint=state_hint)

    assert decision.status == "applied"
    assert decision.cache_hit is True
    assert model.call_count == 0
    assert client.service.applied[-1]["action_generation"] == 8
    assert client.calls[-1][1]["action"]["action_generation"] == 8


@pytest.mark.integration
@pytest.mark.asyncio
async def test_safety_rejected_action_immediately_uses_fallback() -> None:
    client = RejectingApplyClient()
    host = AgentHost(
        mcp_client=client,
        model=ScriptedModel([valid_tool_sequence()]),
        config=AgentHostConfig(enable_action_cache=False),
    )
    decision = await host.decide("fake-run")
    assert decision.status == "fallback"
    assert decision.corrective_reprompt_used is False
    assert decision.trace[-2].tool_name == "apply_control_action"
    assert decision.trace[-1].tool_name == "request_safe_fallback"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terminal_host_decision_is_durably_recorded(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bus.db")
    store.create_run("fake-run", RunType.FAKE_TEST, is_fake=True)
    store.set_run_status("fake-run", RunStatus.RUNNING)
    now = datetime.now(UTC)
    store.record_observation(
        observation_input(
            run_id="fake-run",
            timestamp=now,
            simulation_timestamp=now,
        )
    )
    host = AgentHost(
        mcp_client=DirectFakeMCPClient(),
        model=ScriptedModel([valid_tool_sequence()]),
        config=AgentHostConfig(enable_action_cache=False),
        decision_sink=SQLiteDecisionSink(store),
    )
    decision = await host.decide("fake-run")
    connection = sqlite3.connect(store.path)
    try:
        row = connection.execute(
            """
            SELECT run_id, observation_id, model, completed, reason_code, explanation
            FROM agent_decisions
            """
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        "fake-run",
        decision.observation_id,
        "explicit-test-model",
        1,
        "comfort_protection",
        "Reduce occupied overheating.",
    )
