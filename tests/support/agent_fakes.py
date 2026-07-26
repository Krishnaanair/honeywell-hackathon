"""Explicit model/client fakes for hermetic agent reliability tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ecoloop.agent.models import ModelResponse, ToolRequest, ToolSpec
from ecoloop.mcp.models import CandidateActionInput, ControlActionInput
from tests.support.fake_mcp import FakeMCPService

TOOL_NAMES = (
    "get_current_building_state",
    "get_recent_trends",
    "get_constraints",
    "get_weather_forecast",
    "get_grid_signal",
    "generate_candidate_actions",
    "evaluate_candidate_actions",
    "apply_control_action",
    "request_safe_fallback",
    "get_last_energyplus_errors",
    "inspect_idf",
    "validate_idf",
    "inspect_available_energyplus_points",
    "parse_energyplus_error_file",
    "generate_replay_model",
)


class DirectFakeMCPClient:
    """Protocol-shaped test double; transport itself is tested separately."""

    def __init__(self, service: FakeMCPService | None = None) -> None:
        self.service = service or FakeMCPService()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def discover_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=name,
                description=f"Explicit test fake for {name}.",
                input_schema={"type": "object"},
            )
            for name in TOOL_NAMES
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        run_id = str(arguments.get("run_id", ""))
        if name == "get_current_building_state":
            return await self.service.get_current_building_state(run_id)
        if name == "get_recent_trends":
            return await self.service.get_recent_trends(run_id, int(arguments["window_steps"]))
        if name == "get_constraints":
            return await self.service.get_constraints(run_id)
        if name == "get_weather_forecast":
            return await self.service.get_weather_forecast(run_id, int(arguments["hours"]))
        if name == "get_grid_signal":
            return await self.service.get_grid_signal(run_id, int(arguments["hours"]))
        if name == "generate_candidate_actions":
            return await self.service.generate_candidate_actions(run_id)
        if name == "evaluate_candidate_actions":
            candidates = [
                CandidateActionInput.model_validate(item) for item in arguments["candidates"]
            ]
            return await self.service.evaluate_candidate_actions(run_id, candidates)
        if name == "apply_control_action":
            action = ControlActionInput.model_validate(arguments["action"])
            return await self.service.apply_control_action(
                run_id,
                int(arguments["observation_id"]),
                action,
            )
        if name == "request_safe_fallback":
            return await self.service.request_safe_fallback(
                run_id,
                int(arguments["observation_id"]),
            )
        if name == "get_last_energyplus_errors":
            return await self.service.get_last_energyplus_errors(run_id, int(arguments["limit"]))
        if name == "inspect_idf":
            return await self.service.inspect_idf(Path(str(arguments["path"])))
        if name == "validate_idf":
            return await self.service.validate_idf(
                Path(str(arguments["path"])),
                Path(str(arguments["weather_path"])),
            )
        if name == "inspect_available_energyplus_points":
            return await self.service.inspect_available_energyplus_points(
                run_id, str(arguments["query"])
            )
        if name == "parse_energyplus_error_file":
            return await self.service.parse_energyplus_error_file(Path(str(arguments["path"])))
        if name == "generate_replay_model":
            return await self.service.generate_replay_model(run_id)
        raise ValueError(f"unknown fake tool: {name}")


class ScriptedModel:
    """Return predetermined operational tool calls without generated reasoning."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.call_count = 0

    @property
    def model_name(self) -> str:
        return "explicit-test-model"

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del messages, tools
        index = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[index]


class TimeoutModel:
    """Never completes before the host's deliberately tiny test timeout."""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def model_name(self) -> str:
        return "explicit-timeout-test-model"

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del messages, tools
        self.call_count += 1
        await asyncio.sleep(60)
        return ModelResponse()


def valid_tool_sequence() -> ModelResponse:
    """Return one valid four-call fake model turn."""

    return ModelResponse(
        model="explicit-test-model",
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
                        "explanation": "Reduce occupied overheating.",
                    },
                },
            ),
        ],
    )
