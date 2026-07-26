"""Dependency-injected services behind the MCP protocol boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ecoloop.mcp.models import (
    AuditEvent,
    CandidateActionInput,
    ControlActionInput,
    RunId,
)

JsonObject = dict[str, Any]


@runtime_checkable
class EcoLoopToolService(Protocol):
    """Application operations permitted to the local supervisory controller.

    Implementations own all database transactions and deterministic safety
    validation. The protocol server never gives the model a store, shell, network
    client, or general filesystem primitive.
    """

    async def get_current_building_state(self, run_id: RunId) -> JsonObject:
        """Return the latest compact observation for a run."""

    async def get_recent_trends(self, run_id: RunId, window_steps: int) -> JsonObject:
        """Return bounded rolling trends for recent observations."""

    async def get_constraints(self, run_id: RunId) -> JsonObject:
        """Return active deterministic constraints and actuator capabilities."""

    async def get_weather_forecast(self, run_id: RunId, hours: int) -> JsonObject:
        """Return the configured simulation weather forecast."""

    async def get_grid_signal(self, run_id: RunId, hours: int) -> JsonObject:
        """Return bounded tariff and carbon signals."""

    async def generate_candidate_actions(self, run_id: RunId) -> JsonObject:
        """Generate a safe-sized candidate grid without applying it."""

    async def evaluate_candidate_actions(
        self,
        run_id: RunId,
        candidates: list[CandidateActionInput],
    ) -> JsonObject:
        """Score candidates transparently without applying one."""

    async def apply_control_action(
        self,
        run_id: RunId,
        observation_id: int,
        action: ControlActionInput,
    ) -> JsonObject:
        """Validate, persist, and make one idempotent action available."""

    async def request_safe_fallback(
        self,
        run_id: RunId,
        observation_id: int,
    ) -> JsonObject:
        """Select and persist a deterministic fallback action."""

    async def get_last_energyplus_errors(self, run_id: RunId, limit: int) -> JsonObject:
        """Return only bounded, classified EnergyPlus errors."""

    async def inspect_idf(self, path: Path) -> JsonObject:
        """Inspect a confined IDF or epJSON file."""

    async def validate_idf(self, path: Path, weather_path: Path) -> JsonObject:
        """Validate a confined model using the configured EnergyPlus executable."""

    async def inspect_available_energyplus_points(
        self,
        run_id: RunId,
        query: str,
    ) -> JsonObject:
        """Search a run's captured API-point catalogue."""

    async def parse_energyplus_error_file(self, path: Path) -> JsonObject:
        """Parse a confined eplusout.err file."""

    async def generate_replay_model(self, run_id: RunId) -> JsonObject:
        """Generate a schedule-driven replay from an audited completed run."""

    async def audit_tool_call(self, event: AuditEvent) -> None:
        """Durably record one tool call without private model reasoning."""
