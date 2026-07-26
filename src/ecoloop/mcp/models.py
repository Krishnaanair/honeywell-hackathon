"""Strict input and audit models for MCP tools."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RunId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
        description="Opaque run identifier returned by EcoLoop.",
    ),
]
ObservationId = Annotated[int, Field(ge=1)]
OperationalReasonCode = Literal[
    "ENERGY_OPTIMIZATION",
    "COMFORT_PROTECTION",
    "IAQ_PROTECTION",
    "DEMAND_RESPONSE",
    "OCCUPANCY_SETBACK",
    "SCHEDULE_TRACKING",
    "FREEZE_PROTECTION",
    "OVERHEAT_PROTECTION",
]
OPERATIONAL_REASON_CODES: tuple[OperationalReasonCode, ...] = (
    "ENERGY_OPTIMIZATION",
    "COMFORT_PROTECTION",
    "IAQ_PROTECTION",
    "DEMAND_RESPONSE",
    "OCCUPANCY_SETBACK",
    "SCHEDULE_TRACKING",
    "FREEZE_PROTECTION",
    "OVERHEAT_PROTECTION",
)


class StrictToolModel(BaseModel):
    """Base class that rejects unexpected model-provided fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class CandidateActionInput(StrictToolModel):
    """Bounded thermostat action offered to the candidate evaluator."""

    candidate_id: str | None = Field(default=None, min_length=1, max_length=255)
    heating_setpoint_c: float
    cooling_setpoint_c: float
    hold_minutes: int = Field(ge=15, le=120)
    ventilation_multiplier: float | None = Field(default=None, ge=0.0, le=2.0)
    lighting_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    supply_air_temperature_c: float | None = Field(default=None, ge=8.0, le=30.0)
    shading_state: int | None = Field(default=None, ge=0, le=1)

    @field_validator(
        "heating_setpoint_c",
        "cooling_setpoint_c",
        "ventilation_multiplier",
        "lighting_fraction",
        "supply_air_temperature_c",
        "shading_state",
    )
    @classmethod
    def require_finite(cls, value: float | None) -> float | None:
        """Reject NaN and infinity before the safety layer is reached."""

        if value is not None and not math.isfinite(value):
            raise ValueError("control values must be finite")
        return value

    @model_validator(mode="after")
    def require_ordered_setpoints(self) -> CandidateActionInput:
        """Reject an inverted thermostat pair at the protocol boundary."""

        if self.heating_setpoint_c >= self.cooling_setpoint_c:
            raise ValueError("heating_setpoint_c must be below cooling_setpoint_c")
        return self


class ControlActionInput(CandidateActionInput):
    """A selected candidate plus audit fields required for application."""

    action_generation: int = Field(ge=1)
    reason_code: OperationalReasonCode
    explanation: str = Field(min_length=1, max_length=500)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    latency_ms: float | None = Field(default=None, ge=0)


class AuditEvent(StrictToolModel):
    """One complete tool invocation suitable for durable auditing."""

    run_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    success: bool
    error: str | None
    started_at: datetime
    completed_at: datetime
    latency_ms: float = Field(ge=0)
    control_affecting: bool

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Normalize timestamps to UTC and reject naive values."""

        if value.tzinfo is None:
            raise ValueError("audit timestamps must be timezone-aware")
        return value.astimezone(UTC)


class TerminalToolResult(StrictToolModel):
    """Normalized success marker returned by apply and fallback tools."""

    success: bool
    terminal: Literal["applied", "fallback"]
    run_id: str
    observation_id: int
    action: dict[str, Any] | None = None
    fallback_status: str | None = None
