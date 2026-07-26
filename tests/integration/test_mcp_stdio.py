"""Protocol-level tests against a real FastMCP stdio child process."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters

from ecoloop.agent.client import MCPClientError, MCPStdioClient, tools_for_ollama

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
CONTROL_AFFECTING_TOOLS = {
    "generate_candidate_actions",
    "evaluate_candidate_actions",
    "apply_control_action",
    "request_safe_fallback",
}


def fake_server_parameters(
    *,
    allowed_root: Path | None = None,
    audit_file: Path | None = None,
) -> StdioServerParameters:
    """Build explicit test-fake subprocess parameters without secret propagation."""

    root = Path(__file__).resolve().parents[2]
    environment = {
        name: value
        for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if (value := os.environ.get(name))
    }
    environment["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root)])
    arguments = [str(root / "tests" / "support" / "mcp_test_server.py"), "--fake"]
    if allowed_root is not None:
        arguments.extend(("--allowed-root", str(allowed_root.resolve())))
    if audit_file is not None:
        arguments.extend(("--audit-file", str(audit_file.resolve())))
    return StdioServerParameters(
        command=sys.executable,
        args=arguments,
        env=environment,
        cwd=root,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stdio_client_invokes_all_tools_and_captures_audit_trace(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "confined-diagnostics"
    allowed_root.mkdir()
    model = allowed_root / "building.idf"
    weather = allowed_root / "weather.epw"
    error_file = allowed_root / "eplusout.err"
    audit_file = allowed_root / "tool-audit.jsonl"
    model.write_text("Version,26.1;\n", encoding="utf-8")
    weather.write_text("LOCATION,Explicit Test Fake\n", encoding="utf-8")
    error_file.write_text("Program Version,EnergyPlus\n", encoding="utf-8")

    parameters = fake_server_parameters(allowed_root=allowed_root, audit_file=audit_file)
    responses: dict[str, dict[str, Any]] = {}
    async with MCPStdioClient(parameters) as client:
        tools = await client.discover_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        assert all(tool.description for tool in tools)
        converted = tools_for_ollama(tools)
        assert {item["function"]["name"] for item in converted} == {tool.name for tool in tools}

        calls: list[tuple[str, dict[str, Any]]] = [
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
                            "candidate_id": "candidate-1",
                            "heating_setpoint_c": 20.0,
                            "cooling_setpoint_c": 25.0,
                            "hold_minutes": 60,
                        }
                    ],
                },
            ),
            ("get_last_energyplus_errors", {"run_id": "fake-run", "limit": 7}),
            ("inspect_idf", {"path": str(model)}),
            (
                "validate_idf",
                {"path": str(model), "weather_path": str(weather)},
            ),
            (
                "inspect_available_energyplus_points",
                {"run_id": "fake-run", "query": " zone   temperature "},
            ),
            ("parse_energyplus_error_file", {"path": str(error_file)}),
            ("generate_replay_model", {"run_id": "fake-run"}),
            ("request_safe_fallback", {"run_id": "fake-run", "observation_id": 1}),
            (
                "apply_control_action",
                {
                    "run_id": "fake-run",
                    "observation_id": 1,
                    "action": {
                        "candidate_id": "candidate-1",
                        "heating_setpoint_c": 20.0,
                        "cooling_setpoint_c": 25.0,
                        "hold_minutes": 60,
                        "action_generation": 1,
                        "reason_code": "COMFORT_PROTECTION",
                        "explanation": "Reduce occupied overheating.",
                        "model": "explicit-test-model",
                        "latency_ms": 12.5,
                    },
                },
            ),
        ]
        assert {name for name, _ in calls} == EXPECTED_TOOLS
        for name, arguments in calls:
            responses[name] = await client.call_tool(name, arguments)
            assert responses[name]["source"] == "explicit_test_fake"

    assert responses["get_current_building_state"]["observation"]["observation_id"] == 1
    assert responses["get_recent_trends"]["window_steps"] == 8
    assert responses["get_constraints"]["minimum_deadband_c"] == 2.0
    assert responses["get_weather_forecast"]["hours"] == 6
    assert responses["get_grid_signal"]["hours"] == 6
    assert len(responses["generate_candidate_actions"]["candidates"]) == 2
    assert len(responses["evaluate_candidate_actions"]["evaluated_candidates"]) == 1
    assert responses["get_last_energyplus_errors"]["limit"] == 7
    assert Path(responses["inspect_idf"]["path"]) == model
    assert responses["validate_idf"]["valid"] is True
    assert responses["inspect_available_energyplus_points"]["query"] == "zone temperature"
    assert responses["parse_energyplus_error_file"]["counts"]["fatal"] == 0
    assert responses["generate_replay_model"]["generated"] is True
    assert responses["request_safe_fallback"]["terminal"] == "fallback"
    assert responses["apply_control_action"]["terminal"] == "applied"

    audit_rows = [
        json.loads(line)
        for line in audit_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["tool_name"] for row in audit_rows] == [name for name, _ in calls]
    assert all(row["success"] is True for row in audit_rows)
    assert {
        row["tool_name"] for row in audit_rows if row["control_affecting"] is True
    } == CONTROL_AFFECTING_TOOLS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stdio_server_confines_diagnostic_paths_and_audits_rejection(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    audit_file = allowed_root / "tool-audit.jsonl"
    escaped = tmp_path / "outside.idf"
    escaped.write_text("Version,26.1;\n", encoding="utf-8")

    parameters = fake_server_parameters(allowed_root=allowed_root, audit_file=audit_file)
    async with MCPStdioClient(parameters) as client:
        with pytest.raises(MCPClientError, match="outside"):
            await client.call_tool("inspect_idf", {"path": str(escaped)})

    audit_rows = [
        json.loads(line)
        for line in audit_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(audit_rows) == 1
    assert audit_rows[0]["tool_name"] == "inspect_idf"
    assert audit_rows[0]["success"] is False
    assert audit_rows[0]["result"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stdio_server_rejects_invalid_tool_input_through_protocol() -> None:
    async with MCPStdioClient(fake_server_parameters()) as client:
        with pytest.raises(MCPClientError, match=r"failed|validation|Input"):
            await client.call_tool(
                "get_recent_trends",
                {"run_id": "fake-run", "window_steps": 0},
            )
