"""Unit coverage for every narrow MCP tool and trust-boundary validator."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from ecoloop.config import Settings
from ecoloop.control.safety import SafetyContext, SafetyValidator
from ecoloop.db.store import SQLiteStore
from ecoloop.exceptions import RunStateError
from ecoloop.mcp.models import OPERATIONAL_REASON_CODES, ControlActionInput
from ecoloop.mcp.path_policy import PathPolicy
from ecoloop.mcp.server import create_mcp_server
from ecoloop.mcp.sqlite_service import SQLiteMCPService
from ecoloop.schemas import ControlAction, RunStatus, RunType, ValidationResult
from tests.support.fake_mcp import FakeMCPService
from tests.unit._factories import observation_input

EXPECTED_TOOLS = {
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
}


class TerminalDuringValidation(SafetyValidator):
    """Cross the terminal boundary after validation but before persistence."""

    def __init__(self, store: SQLiteStore) -> None:
        super().__init__()
        self._store = store

    def validate(
        self,
        proposal: ControlAction,
        context: SafetyContext,
    ) -> ValidationResult:
        result = super().validate(proposal, context)
        self._store.set_run_status(proposal.run_id, RunStatus.COMPLETED)
        return result


@pytest.fixture
def tool_server(tmp_path: Path) -> tuple[Any, FakeMCPService, dict[str, Path]]:
    model = tmp_path / "building.idf"
    weather = tmp_path / "weather.epw"
    error_file = tmp_path / "eplusout.err"
    model.write_text("Version,26.1;", encoding="utf-8")
    weather.write_text("LOCATION,Explicit Test Fake", encoding="utf-8")
    error_file.write_text("Program Version,EnergyPlus", encoding="utf-8")
    service = FakeMCPService()
    server = create_mcp_server(service, path_policy=PathPolicy(tmp_path))
    return server, service, {"model": model, "weather": weather, "error": error_file}


@pytest.mark.asyncio
async def test_server_exposes_exact_narrow_tool_set(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
) -> None:
    server, _, _ = tool_server
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    descriptions = {tool.name: tool.description for tool in tools}
    assert all(descriptions.values())


@pytest.mark.asyncio
async def test_apply_tool_schema_exposes_only_supported_operational_reason_codes(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
) -> None:
    server, _, _ = tool_server
    tools = await server.list_tools()
    apply_tool = next(tool for tool in tools if tool.name == "apply_control_action")
    action_schema = apply_tool.inputSchema["$defs"]["ControlActionInput"]
    assert action_schema["properties"]["reason_code"]["enum"] == list(OPERATIONAL_REASON_CODES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_current_building_state", {"run_id": "fake-run"}),
        ("get_recent_trends", {"run_id": "fake-run", "window_steps": 8}),
        ("get_constraints", {"run_id": "fake-run"}),
        ("get_weather_forecast", {"run_id": "fake-run", "hours": 6}),
        ("get_grid_signal", {"run_id": "fake-run", "hours": 6}),
        ("generate_candidate_actions", {"run_id": "fake-run"}),
        (
            "evaluate_candidate_actions",
            {
                "run_id": "fake-run",
                "candidates": [
                    {
                        "heating_setpoint_c": 20.0,
                        "cooling_setpoint_c": 25.0,
                        "hold_minutes": 60,
                    }
                ],
            },
        ),
        (
            "apply_control_action",
            {
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
        ("request_safe_fallback", {"run_id": "fake-run", "observation_id": 1}),
        ("get_last_energyplus_errors", {"run_id": "fake-run", "limit": 10}),
        (
            "inspect_available_energyplus_points",
            {"run_id": "fake-run", "query": " zone   temperature "},
        ),
        ("generate_replay_model", {"run_id": "fake-run"}),
    ],
)
async def test_runtime_and_run_diagnostic_tools_are_callable_and_audited(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
    name: str,
    arguments: dict[str, Any],
) -> None:
    server, service, _ = tool_server
    _, result = await server.call_tool(name, arguments)
    assert isinstance(result, dict)
    assert service.audits[-1].tool_name == name
    assert service.audits[-1].success is True
    assert service.audits[-1].control_affecting is (
        name
        in {
            "generate_candidate_actions",
            "evaluate_candidate_actions",
            "apply_control_action",
            "request_safe_fallback",
        }
    )


@pytest.mark.asyncio
async def test_confined_file_diagnostic_tools_are_callable(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
) -> None:
    server, service, paths = tool_server
    calls = [
        ("inspect_idf", {"path": str(paths["model"])}),
        (
            "validate_idf",
            {"path": str(paths["model"]), "weather_path": str(paths["weather"])},
        ),
        ("parse_energyplus_error_file", {"path": str(paths["error"])}),
    ]
    for name, arguments in calls:
        _, result = await server.call_tool(name, arguments)
        assert isinstance(result, dict)
        assert service.audits[-1].tool_name == name


@pytest.mark.asyncio
async def test_file_tool_rejects_path_escape_and_audits_failure(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
    tmp_path: Path,
) -> None:
    server, service, _ = tool_server
    escaped = tmp_path.parent / "outside.idf"
    escaped.write_text("Version,26.1;", encoding="utf-8")
    with pytest.raises(ToolError, match="outside"):
        await server.call_tool("inspect_idf", {"path": str(escaped)})
    assert service.audits[-1].success is False
    assert service.audits[-1].result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"run_id": "../wrong"},
        {"run_id": "fake-run", "window_steps": 0},
        {"run_id": "fake-run", "hours": 169},
    ],
)
async def test_tool_input_schema_rejects_invalid_values(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
    arguments: dict[str, Any],
) -> None:
    server, _, _ = tool_server
    name = (
        "get_recent_trends"
        if "window_steps" in arguments
        else "get_weather_forecast"
        if "hours" in arguments
        else "get_current_building_state"
    )
    with pytest.raises(Exception, match=r"validation|Input|run_id|window_steps|hours"):
        await server.call_tool(name, arguments)


@pytest.mark.asyncio
async def test_apply_schema_rejects_inverted_setpoints_before_service(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
) -> None:
    server, service, _ = tool_server
    with pytest.raises(Exception, match="heating_setpoint_c"):
        await server.call_tool(
            "apply_control_action",
            {
                "run_id": "fake-run",
                "observation_id": 1,
                "action": {
                    "heating_setpoint_c": 25.0,
                    "cooling_setpoint_c": 24.0,
                    "hold_minutes": 60,
                    "action_generation": 1,
                    "reason_code": "COMFORT_PROTECTION",
                    "explanation": "Invalid on purpose.",
                },
            },
        )
    assert not service.applied


@pytest.mark.asyncio
async def test_apply_schema_rejects_unknown_reason_code_before_service(
    tool_server: tuple[Any, FakeMCPService, dict[str, Path]],
) -> None:
    server, service, _ = tool_server
    with pytest.raises(Exception, match=r"reason_code|literal"):
        await server.call_tool(
            "apply_control_action",
            {
                "run_id": "fake-run",
                "observation_id": 1,
                "action": {
                    "heating_setpoint_c": 20.0,
                    "cooling_setpoint_c": 25.0,
                    "hold_minutes": 60,
                    "action_generation": 1,
                    "reason_code": "ACTION_SELECTED",
                    "explanation": "Unsupported reason code on purpose.",
                },
            },
        )
    assert not service.applied


@pytest.mark.asyncio
async def test_sqlite_service_generates_validates_applies_and_audits_real_store(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bus.db")
    store.create_run("run-agent-1", RunType.AGENT, energyplus_version="26.1.0")
    store.set_run_status("run-agent-1", RunStatus.RUNNING)
    now = datetime.now(UTC)
    observation = store.record_observation(
        observation_input(timestamp=now, simulation_timestamp=now)
    )
    settings = Settings(
        ECOLOOP_DATABASE_PATH=tmp_path / "bus.db",
        ECOLOOP_RUNS_DIR=tmp_path / "runs",
    )
    service = SQLiteMCPService(store=store, settings=settings)
    server = create_mcp_server(service, path_policy=PathPolicy(tmp_path))

    _, generated = await server.call_tool(
        "generate_candidate_actions",
        {"run_id": "run-agent-1"},
    )
    assert isinstance(generated, dict)
    assert generated["scores"]
    selected = generated["scores"][0]["candidate"]
    _, applied = await server.call_tool(
        "apply_control_action",
        {
            "run_id": "run-agent-1",
            "observation_id": observation.observation_id,
            "action": {
                **selected,
                "action_generation": 1,
                "reason_code": "ENERGY_OPTIMIZATION",
                "explanation": "Selected the lowest scored safe candidate.",
                "model": "qwen3:8b",
                "latency_ms": 125.0,
            },
        },
    )
    assert isinstance(applied, dict)
    assert applied["success"] is True
    persisted = store.get_last_applied_action("run-agent-1")
    assert persisted is not None
    assert persisted[0].model == "qwen3:8b"
    traces = store.get_recent_tool_calls("run-agent-1")
    assert [trace.tool_name for trace in traces] == [
        "generate_candidate_actions",
        "apply_control_action",
    ]
    assert all(trace.control_affecting for trace in traces)


@pytest.mark.asyncio
async def test_sqlite_service_fallback_advances_after_rejected_proposal(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bus.db")
    store.create_run("run-agent-1", RunType.AGENT, energyplus_version="26.1.0")
    store.set_run_status("run-agent-1", RunStatus.RUNNING)
    now = datetime.now(UTC)
    observation = store.record_observation(
        observation_input(timestamp=now, simulation_timestamp=now)
    )
    service = SQLiteMCPService(
        store=store,
        settings=Settings(
            ECOLOOP_DATABASE_PATH=tmp_path / "bus.db",
            ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ),
    )
    server = create_mcp_server(service, path_policy=PathPolicy(tmp_path))
    _, rejected = await server.call_tool(
        "apply_control_action",
        {
            "run_id": "run-agent-1",
            "observation_id": observation.observation_id,
            "action": {
                "heating_setpoint_c": 20.0,
                "cooling_setpoint_c": 24.0,
                "hold_minutes": 60,
                "ventilation_multiplier": 0.5,
                "action_generation": 1,
                "reason_code": "ENERGY_OPTIMIZATION",
                "explanation": "Unsupported ventilation request for rejection test.",
            },
        },
    )
    assert isinstance(rejected, dict)
    assert rejected["success"] is False
    _, fallback = await server.call_tool(
        "request_safe_fallback",
        {
            "run_id": "run-agent-1",
            "observation_id": observation.observation_id,
        },
    )
    assert isinstance(fallback, dict)
    assert fallback["success"] is True
    persisted = store.get_last_applied_action("run-agent-1")
    assert persisted is not None
    assert persisted[0].action_generation == 2


@pytest.mark.asyncio
async def test_terminal_run_rejects_control_tools_without_proposals(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bus.db")
    store.create_run("run-agent-1", RunType.AGENT, energyplus_version="26.1.0")
    store.set_run_status("run-agent-1", RunStatus.RUNNING)
    now = datetime.now(UTC)
    observation = store.record_observation(
        observation_input(timestamp=now, simulation_timestamp=now)
    )
    store.set_run_status("run-agent-1", RunStatus.COMPLETED)
    service = SQLiteMCPService(
        store=store,
        settings=Settings(
            ECOLOOP_DATABASE_PATH=tmp_path / "bus.db",
            ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ),
    )
    action = ControlActionInput(
        heating_setpoint_c=20.0,
        cooling_setpoint_c=25.0,
        hold_minutes=60,
        action_generation=1,
        reason_code="ENERGY_OPTIMIZATION",
        explanation="This terminal-boundary action must not persist.",
    )

    with pytest.raises(RunStateError, match="require a running simulation"):
        await service.apply_control_action(
            "run-agent-1",
            observation.observation_id,
            action,
        )
    with pytest.raises(RunStateError, match="require a running simulation"):
        await service.request_safe_fallback(
            "run-agent-1",
            observation.observation_id,
        )

    assert store.get_applied_actions("run-agent-1") == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposed_actions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_terminal_transition_during_validation_leaves_no_orphan_proposal(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "bus.db")
    store.create_run("run-agent-1", RunType.AGENT, energyplus_version="26.1.0")
    store.set_run_status("run-agent-1", RunStatus.RUNNING)
    now = datetime.now(UTC)
    observation = store.record_observation(
        observation_input(timestamp=now, simulation_timestamp=now)
    )
    service = SQLiteMCPService(
        store=store,
        settings=Settings(
            ECOLOOP_DATABASE_PATH=tmp_path / "bus.db",
            ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ),
        validator=TerminalDuringValidation(store),
    )
    action = ControlActionInput(
        heating_setpoint_c=20.0,
        cooling_setpoint_c=25.0,
        hold_minutes=60,
        action_generation=1,
        reason_code="ENERGY_OPTIMIZATION",
        explanation="Validation completes exactly as the run reaches terminal.",
    )

    result = await service.apply_control_action(
        "run-agent-1",
        observation.observation_id,
        action,
    )

    assert result["success"] is False
    assert result["application"]["rejection_code"] == "run_not_active"
    assert store.get_applied_actions("run-agent-1") == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposed_actions").fetchone()[0] == 0
