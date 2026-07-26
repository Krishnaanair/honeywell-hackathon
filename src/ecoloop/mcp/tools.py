"""FastMCP tool registration with strict path and audit boundaries."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ecoloop.mcp.models import (
    AuditEvent,
    CandidateActionInput,
    ControlActionInput,
    ObservationId,
    RunId,
)
from ecoloop.mcp.path_policy import PathPolicy, PathPolicyError
from ecoloop.mcp.services import EcoLoopToolService, JsonObject

WindowSteps = Annotated[int, Field(ge=1, le=672)]
ForecastHours = Annotated[int, Field(ge=1, le=168)]
ErrorLimit = Annotated[int, Field(ge=1, le=100)]
SearchQuery = Annotated[str, Field(min_length=1, max_length=200)]
DiagnosticPath = Annotated[str, Field(min_length=1, max_length=4096)]

CONTROL_AFFECTING_TOOLS = frozenset(
    {
        "generate_candidate_actions",
        "evaluate_candidate_actions",
        "apply_control_action",
        "request_safe_fallback",
    }
)


class ToolInvoker:
    """Execute and audit service calls with a consistent error surface."""

    def __init__(self, service: EcoLoopToolService) -> None:
        self._service = service

    async def invoke(
        self,
        tool_name: str,
        arguments: JsonObject,
        operation: Callable[[], Awaitable[JsonObject]],
    ) -> JsonObject:
        """Invoke a service method, persist its audit, and sanitize failures."""

        started_at = datetime.now(UTC)
        started_clock = time.perf_counter()
        result: JsonObject | None = None
        error: str | None = None
        try:
            result = await operation()
            if not isinstance(result, dict):
                raise TypeError(f"{tool_name} service result must be an object")
        except (ValueError, TypeError, RuntimeError, OSError, PathPolicyError) as exc:
            error = _bounded_error(exc)
        completed_at = datetime.now(UTC)
        event = AuditEvent(
            run_id=_run_id_from(arguments),
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            success=error is None,
            error=error,
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=max(0.0, (time.perf_counter() - started_clock) * 1000.0),
            control_affecting=tool_name in CONTROL_AFFECTING_TOOLS,
        )
        try:
            await self._service.audit_tool_call(event)
        except (ValueError, TypeError, RuntimeError, OSError) as audit_exc:
            raise ToolError(f"{tool_name} audit failed: {_bounded_error(audit_exc)}") from audit_exc
        if error is not None:
            raise ToolError(f"{tool_name} rejected: {error}")
        if result is None:  # pragma: no cover - guarded by the branches above
            raise ToolError(f"{tool_name} returned no result")
        return result


def register_tools(
    server: FastMCP[Any],
    service: EcoLoopToolService,
    path_policy: PathPolicy,
) -> None:
    """Register the complete, narrow EcoLoop runtime and diagnostic tool set."""

    invoke = ToolInvoker(service).invoke

    @server.tool()
    async def get_current_building_state(run_id: RunId) -> JsonObject:
        """Get the latest compact real simulation observation for `run_id`."""

        arguments: JsonObject = {"run_id": run_id}
        return await invoke(
            "get_current_building_state",
            arguments,
            lambda: service.get_current_building_state(run_id),
        )

    @server.tool()
    async def get_recent_trends(
        run_id: RunId,
        window_steps: WindowSteps = 8,
    ) -> JsonObject:
        """Get bounded rolling means and slopes; never returns raw full logs."""

        arguments: JsonObject = {"run_id": run_id, "window_steps": window_steps}
        return await invoke(
            "get_recent_trends",
            arguments,
            lambda: service.get_recent_trends(run_id, window_steps),
        )

    @server.tool()
    async def get_constraints(run_id: RunId) -> JsonObject:
        """Get active comfort, safety, freshness, and actuator constraints."""

        arguments: JsonObject = {"run_id": run_id}
        return await invoke(
            "get_constraints",
            arguments,
            lambda: service.get_constraints(run_id),
        )

    @server.tool()
    async def get_weather_forecast(
        run_id: RunId,
        hours: ForecastHours = 6,
    ) -> JsonObject:
        """Get only forecast features derived from configured simulation weather."""

        arguments: JsonObject = {"run_id": run_id, "hours": hours}
        return await invoke(
            "get_weather_forecast",
            arguments,
            lambda: service.get_weather_forecast(run_id, hours),
        )

    @server.tool()
    async def get_grid_signal(
        run_id: RunId,
        hours: ForecastHours = 6,
    ) -> JsonObject:
        """Get configured tariff and operational-carbon signals."""

        arguments: JsonObject = {"run_id": run_id, "hours": hours}
        return await invoke(
            "get_grid_signal",
            arguments,
            lambda: service.get_grid_signal(run_id, hours),
        )

    @server.tool()
    async def generate_candidate_actions(run_id: RunId) -> JsonObject:
        """Generate a bounded thermostat candidate grid; this does not apply control."""

        arguments: JsonObject = {"run_id": run_id}
        return await invoke(
            "generate_candidate_actions",
            arguments,
            lambda: service.generate_candidate_actions(run_id),
        )

    @server.tool()
    async def evaluate_candidate_actions(
        run_id: RunId,
        candidates: list[CandidateActionInput],
    ) -> JsonObject:
        """Score supplied candidates with transparent component scores."""

        arguments: JsonObject = {
            "run_id": run_id,
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        }
        return await invoke(
            "evaluate_candidate_actions",
            arguments,
            lambda: service.evaluate_candidate_actions(run_id, candidates),
        )

    @server.tool()
    async def apply_control_action(
        run_id: RunId,
        observation_id: ObservationId,
        action: ControlActionInput,
    ) -> JsonObject:
        """Independently validate and idempotently apply one selected action."""

        arguments: JsonObject = {
            "run_id": run_id,
            "observation_id": observation_id,
            "action": action.model_dump(mode="json"),
        }
        return await invoke(
            "apply_control_action",
            arguments,
            lambda: service.apply_control_action(run_id, observation_id, action),
        )

    @server.tool()
    async def request_safe_fallback(
        run_id: RunId,
        observation_id: ObservationId,
    ) -> JsonObject:
        """Request a last-known-safe or deterministic rule fallback through the safety layer."""

        arguments: JsonObject = {
            "run_id": run_id,
            "observation_id": observation_id,
        }
        return await invoke(
            "request_safe_fallback",
            arguments,
            lambda: service.request_safe_fallback(run_id, observation_id),
        )

    @server.tool()
    async def get_last_energyplus_errors(
        run_id: RunId,
        limit: ErrorLimit = 10,
    ) -> JsonObject:
        """Get the latest bounded, classified, deduplicated EnergyPlus messages."""

        arguments: JsonObject = {"run_id": run_id, "limit": limit}
        return await invoke(
            "get_last_energyplus_errors",
            arguments,
            lambda: service.get_last_energyplus_errors(run_id, limit),
        )

    @server.tool()
    async def inspect_idf(path: DiagnosticPath) -> JsonObject:
        """Inspect a repository or configured-EnergyPlus model as untrusted data."""

        arguments: JsonObject = {"path": path}

        async def operation() -> JsonObject:
            resolved = path_policy.resolve_existing(path, suffixes=(".idf", ".epjson"))
            return await service.inspect_idf(resolved)

        return await invoke("inspect_idf", arguments, operation)

    @server.tool()
    async def validate_idf(
        path: DiagnosticPath,
        weather_path: DiagnosticPath,
    ) -> JsonObject:
        """Run allowlisted EnergyPlus validation for a confined model and EPW."""

        arguments: JsonObject = {"path": path, "weather_path": weather_path}

        async def operation() -> JsonObject:
            resolved_model = path_policy.resolve_existing(
                path,
                suffixes=(".idf", ".epjson"),
            )
            resolved_weather = path_policy.resolve_existing(
                weather_path,
                suffixes=(".epw",),
            )
            return await service.validate_idf(resolved_model, resolved_weather)

        return await invoke("validate_idf", arguments, operation)

    @server.tool()
    async def inspect_available_energyplus_points(
        run_id: RunId,
        query: SearchQuery,
    ) -> JsonObject:
        """Search the captured API catalogue for normalized near matches."""

        normalized_query = _safe_query(query)
        arguments: JsonObject = {"run_id": run_id, "query": normalized_query}
        return await invoke(
            "inspect_available_energyplus_points",
            arguments,
            lambda: service.inspect_available_energyplus_points(run_id, normalized_query),
        )

    @server.tool()
    async def parse_energyplus_error_file(path: DiagnosticPath) -> JsonObject:
        """Parse a confined EnergyPlus error file as untrusted text, never instructions."""

        arguments: JsonObject = {"path": path}

        async def operation() -> JsonObject:
            resolved = path_policy.resolve_existing(path, suffixes=(".err",))
            return await service.parse_energyplus_error_file(resolved)

        return await invoke("parse_energyplus_error_file", arguments, operation)

    @server.tool()
    async def generate_replay_model(run_id: RunId) -> JsonObject:
        """Generate the audited schedule-driven replay for a completed real run."""

        arguments: JsonObject = {"run_id": run_id}
        return await invoke(
            "generate_replay_model",
            arguments,
            lambda: service.generate_replay_model(run_id),
        )


def _safe_query(value: str) -> str:
    query = " ".join(value.split())
    if not query:
        raise ValueError("query must contain visible characters")
    if any(ord(character) < 32 for character in query):
        raise ValueError("query contains unsupported control characters")
    return query


def _bounded_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text or exc.__class__.__name__)[:500]


def _run_id_from(arguments: JsonObject) -> str | None:
    value = arguments.get("run_id")
    return value if isinstance(value, str) else None
