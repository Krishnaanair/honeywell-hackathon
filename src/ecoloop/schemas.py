"""Typed domain schemas shared by control, persistence, MCP, and reporting.

The schemas deliberately distinguish proposed actions from safety-applied actions.
Missing optional telemetry remains ``None``; it is never coerced to zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    NonNegativeFloat,
    PositiveFloat,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, Field(min_length=1, max_length=255)]
RunId = Annotated[str, Field(min_length=1, max_length=128)]
JsonObject = dict[str, Any]


def _as_utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _optional_as_utc(value: datetime | None) -> datetime | None:
    """Normalize an optional aware timestamp to UTC."""

    return _as_utc(value) if value is not None else None


class SchemaModel(BaseModel):
    """Strict base settings for domain data crossing a trust boundary."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )


class RunType(StrEnum):
    """Supported run/controller types."""

    BASELINE = "baseline"
    RULE = "rule"
    AGENT = "agent"
    REPLAY = "replay"
    FIXED_OVERRIDE = "fixed_override"
    FAKE_TEST = "fake_test"


class RunStatus(StrEnum):
    """Lifecycle state for a simulation run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class MessageSeverity(StrEnum):
    """Normalized EnergyPlus message severity."""

    INFORMATION = "information"
    WARNING = "warning"
    SEVERE = "severe"
    FATAL = "fatal"


class ReasonCode(StrEnum):
    """Operational, non-chain-of-thought reason attached to a decision."""

    ENERGY_OPTIMIZATION = "energy_optimization"
    COMFORT_PROTECTION = "comfort_protection"
    IAQ_PROTECTION = "iaq_protection"
    DEMAND_RESPONSE = "demand_response"
    OCCUPANCY_SETBACK = "occupancy_setback"
    SCHEDULE_TRACKING = "schedule_tracking"
    SAFE_FALLBACK = "safe_fallback"
    TIMEOUT_FALLBACK = "timeout_fallback"
    INVALID_ACTION_FALLBACK = "invalid_action_fallback"
    CIRCUIT_BREAKER_FALLBACK = "circuit_breaker_fallback"
    FREEZE_PROTECTION = "freeze_protection"
    OVERHEAT_PROTECTION = "overheat_protection"
    MANUAL_OVERRIDE = "manual_override"


class ValidationCode(StrEnum):
    """Stable machine-readable action validation outcome codes."""

    ACCEPTED = "accepted"
    WRONG_RUN = "wrong_run"
    UNKNOWN_OBSERVATION = "unknown_observation"
    STALE_OBSERVATION = "stale_observation"
    FUTURE_OBSERVATION = "future_observation"
    EXPIRED_ACTION = "expired_action"
    INVALID_EXPIRY = "invalid_expiry"
    NON_MONOTONIC_GENERATION = "non_monotonic_generation"
    DUPLICATE_ACTION = "duplicate_action"
    RUN_NOT_ACTIVE = "run_not_active"
    NON_FINITE = "non_finite"
    UNSUPPORTED_ACTUATOR = "unsupported_actuator"
    INVALID_DEADBAND = "invalid_deadband"
    HOLD_CLAMPED = "hold_clamped"
    SETPOINT_CLAMPED = "setpoint_clamped"
    RATE_CLAMPED = "rate_clamped"
    DEMAND_OVERRIDE = "demand_override"
    FREEZE_OVERRIDE = "freeze_override"
    OVERHEAT_OVERRIDE = "overheat_override"


class RunRecord(SchemaModel):
    """Persistent run metadata."""

    run_id: RunId
    timestamp: AwareDatetime
    updated_at: AwareDatetime
    run_type: RunType
    status: RunStatus = RunStatus.PENDING
    is_fake: bool = False
    energyplus_version: str | None = None
    model_path: str | None = None
    weather_path: str | None = None
    period_name: str | None = None
    parent_run_id: str | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    progress_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    error_summary: str | None = None
    metadata: JsonObject = Field(default_factory=dict)

    _normalize_timestamp = field_validator("timestamp", "updated_at", mode="after")(_as_utc)
    _normalize_optional_timestamp = field_validator("started_at", "completed_at", mode="after")(
        _optional_as_utc
    )


class ActuatorCapabilities(SchemaModel):
    """Actuator flags discovered from the active EnergyPlus model."""

    heating_setpoint: bool = False
    cooling_setpoint: bool = False
    ventilation_multiplier: bool = False
    lighting_fraction: bool = False
    supply_air_temperature: bool = False
    shading_state: bool = False
    actuator_names: dict[str, str] = Field(default_factory=dict)


class ZoneTelemetry(SchemaModel):
    """One zone's values at one deterministic zone timestep."""

    run_id: RunId
    timestamp: AwareDatetime
    simulation_timestamp: AwareDatetime
    timestep_key: Identifier
    environment: str
    zone_name: Identifier
    mean_air_temperature_c: FiniteFloat
    operative_temperature_c: FiniteFloat | None = None
    relative_humidity_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    occupant_count: NonNegativeFloat | None = None
    pmv: FiniteFloat | None = None
    ppd_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    co2_ppm: NonNegativeFloat | None = None
    heating_setpoint_c: FiniteFloat | None = None
    cooling_setpoint_c: FiniteFloat | None = None

    _normalize_timestamp = field_validator("timestamp", "simulation_timestamp", mode="after")(
        _as_utc
    )


class BuildingTelemetry(SchemaModel):
    """Facility-level telemetry at one deterministic zone timestep."""

    run_id: RunId
    timestamp: AwareDatetime
    simulation_timestamp: AwareDatetime
    timestep_key: Identifier
    environment: str
    outdoor_temperature_c: FiniteFloat | None = None
    facility_demand_kw: NonNegativeFloat | None = None
    timestep_electricity_kwh: NonNegativeFloat | None = None
    cumulative_electricity_kwh: NonNegativeFloat | None = None
    hvac_electricity_kwh: NonNegativeFloat | None = None
    heating_setpoint_c: FiniteFloat | None = None
    cooling_setpoint_c: FiniteFloat | None = None

    _normalize_timestamp = field_validator("timestamp", "simulation_timestamp", mode="after")(
        _as_utc
    )


class TrendSummary(SchemaModel):
    """Compact recent trend sent to the supervisory controller."""

    current: FiniteFloat
    mean: FiniteFloat
    slope_per_hour: FiniteFloat
    minimum: FiniteFloat
    maximum: FiniteFloat
    sample_count: int = Field(ge=1)


class WeatherForecastPoint(SchemaModel):
    """One local weather forecast point."""

    timestamp: AwareDatetime
    drybulb_temperature_c: FiniteFloat
    relative_humidity_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    global_horizontal_solar_w_m2: NonNegativeFloat | None = None
    direct_normal_solar_w_m2: NonNegativeFloat | None = None

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)


class WeatherForecast(SchemaModel):
    """Forecast returned by the bounded runtime weather tool."""

    run_id: RunId
    timestamp: AwareDatetime
    source: str
    points: tuple[WeatherForecastPoint, ...]

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)


class GridSignalPoint(SchemaModel):
    """One tariff and carbon-intensity point."""

    timestamp: AwareDatetime
    tariff_per_kwh: NonNegativeFloat
    carbon_kg_per_kwh: NonNegativeFloat

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)


class GridSignal(SchemaModel):
    """Configured grid signal returned by a bounded runtime tool."""

    run_id: RunId
    timestamp: AwareDatetime
    source: str
    points: tuple[GridSignalPoint, ...]

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)


class BaselineReference(SchemaModel):
    """Reference values aligned to the same simulated timestamp."""

    run_id: RunId
    simulation_timestamp: AwareDatetime
    heating_setpoint_c: FiniteFloat | None = None
    cooling_setpoint_c: FiniteFloat | None = None
    facility_demand_kw: NonNegativeFloat | None = None
    cumulative_electricity_kwh: NonNegativeFloat | None = None

    _normalize_timestamp = field_validator("simulation_timestamp", mode="after")(_as_utc)


class ObservationInput(SchemaModel):
    """Aggregate building state before a database observation ID is assigned."""

    run_id: RunId
    timestamp: AwareDatetime
    simulation_timestamp: AwareDatetime
    timestep_key: Identifier
    environment: str
    occupied: bool
    occupancy_count: NonNegativeFloat
    zone_temperature_mean_c: FiniteFloat
    zone_temperature_min_c: FiniteFloat
    zone_temperature_max_c: FiniteFloat
    operative_temperature_mean_c: FiniteFloat | None = None
    operative_temperature_min_c: FiniteFloat | None = None
    operative_temperature_max_c: FiniteFloat | None = None
    relative_humidity_mean_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    relative_humidity_max_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    pmv_mean: FiniteFloat | None = None
    pmv_max_abs: NonNegativeFloat | None = None
    ppd_mean_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    ppd_max_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    co2_mean_ppm: NonNegativeFloat | None = None
    co2_max_ppm: NonNegativeFloat | None = None
    outdoor_temperature_c: FiniteFloat | None = None
    forecast_temperature_mean_c: FiniteFloat | None = None
    forecast_temperature_max_c: FiniteFloat | None = None
    forecast_solar_mean_w_m2: NonNegativeFloat | None = None
    heating_setpoint_c: FiniteFloat
    cooling_setpoint_c: FiniteFloat
    facility_demand_kw: NonNegativeFloat | None = None
    timestep_electricity_kwh: NonNegativeFloat | None = None
    cumulative_electricity_kwh: NonNegativeFloat | None = None
    hvac_electricity_kwh: NonNegativeFloat | None = None
    tariff_per_kwh: NonNegativeFloat
    carbon_kg_per_kwh: NonNegativeFloat
    baseline_reference: BaselineReference | None = None
    actuator_capabilities: ActuatorCapabilities
    previous_action: CandidateAction | None = None
    recent_trends: dict[str, TrendSummary] = Field(default_factory=dict)
    zones: tuple[ZoneTelemetry, ...] = ()

    _normalize_timestamp = field_validator("timestamp", "simulation_timestamp", mode="after")(
        _as_utc
    )

    @model_validator(mode="after")
    def validate_aggregates(self) -> ObservationInput:
        """Require coherent min/mean/max aggregates and thermostat order."""

        if not (
            self.zone_temperature_min_c
            <= self.zone_temperature_mean_c
            <= self.zone_temperature_max_c
        ):
            raise ValueError("zone temperature aggregates must satisfy min <= mean <= max")
        operative = (
            self.operative_temperature_min_c,
            self.operative_temperature_mean_c,
            self.operative_temperature_max_c,
        )
        if any(value is not None for value in operative):
            if any(value is None for value in operative):
                raise ValueError(
                    "operative temperature min, mean, and max must be supplied together"
                )
            op_min, op_mean, op_max = operative
            if not (op_min <= op_mean <= op_max):  # type: ignore[operator]
                raise ValueError("operative temperature aggregates must satisfy min <= mean <= max")
        if self.heating_setpoint_c >= self.cooling_setpoint_c:
            raise ValueError("current heating setpoint must be below cooling setpoint")
        zone_names = [zone.zone_name.casefold() for zone in self.zones]
        if len(zone_names) != len(set(zone_names)):
            raise ValueError("zone telemetry entries must have unique zone names")
        if any(zone.run_id != self.run_id for zone in self.zones):
            raise ValueError("all zone telemetry must belong to the observation run")
        if any(zone.timestep_key != self.timestep_key for zone in self.zones):
            raise ValueError("all zone telemetry must match the observation timestep")
        return self


class BuildingObservation(ObservationInput):
    """Persisted aggregate building state with a monotonic observation ID."""

    observation_id: int = Field(ge=1)


class SetpointLimits(SchemaModel):
    """Bounds applicable to one occupancy mode."""

    heating_min_c: FiniteFloat
    heating_max_c: FiniteFloat
    cooling_min_c: FiniteFloat
    cooling_max_c: FiniteFloat
    minimum_deadband_c: PositiveFloat
    maximum_change_c: PositiveFloat
    operative_min_c: FiniteFloat | None = None
    operative_max_c: FiniteFloat | None = None
    absolute_pmv_max: PositiveFloat | None = None
    co2_max_ppm: PositiveFloat | None = None

    @model_validator(mode="after")
    def validate_ranges(self) -> SetpointLimits:
        """Require an ordered and feasible thermostat envelope."""

        if self.heating_min_c > self.heating_max_c:
            raise ValueError("heating_min_c must not exceed heating_max_c")
        if self.cooling_min_c > self.cooling_max_c:
            raise ValueError("cooling_min_c must not exceed cooling_max_c")
        if self.heating_min_c + self.minimum_deadband_c > self.cooling_max_c:
            raise ValueError("setpoint limits cannot satisfy the minimum deadband")
        if (
            self.operative_min_c is not None
            and self.operative_max_c is not None
            and self.operative_min_c > self.operative_max_c
        ):
            raise ValueError("operative_min_c must not exceed operative_max_c")
        return self


class ControlConstraints(SchemaModel):
    """Complete deterministic constraints for one decision."""

    run_id: RunId
    timestamp: AwareDatetime
    occupied: bool
    occupied_limits: SetpointLimits
    unoccupied_limits: SetpointLimits
    capabilities: ActuatorCapabilities
    maximum_hold_minutes: int = Field(gt=0, le=24 * 60)
    observation_max_age_minutes: int = Field(gt=0, le=24 * 60)
    demand_threshold_kw: PositiveFloat
    freeze_protection_temperature_c: FiniteFloat = 8.0
    overheat_protection_temperature_c: FiniteFloat = 35.0
    next_action_generation: int = Field(ge=1)

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)

    @property
    def active_limits(self) -> SetpointLimits:
        """Return occupied or unoccupied bounds for this decision."""

        return self.occupied_limits if self.occupied else self.unoccupied_limits


class CandidateAction(SchemaModel):
    """Bounded set of requested actuator values.

    Ordering and deadband are intentionally checked by the independent safety
    validator rather than by this transport schema.
    """

    candidate_id: Identifier
    heating_setpoint_c: FiniteFloat
    cooling_setpoint_c: FiniteFloat
    hold_minutes: int = Field(gt=0, le=24 * 60)
    ventilation_multiplier: Annotated[float, Field(ge=0, le=2)] | None = None
    lighting_fraction: Annotated[float, Field(ge=0, le=1)] | None = None
    supply_air_temperature_c: FiniteFloat | None = None
    shading_state: Annotated[int, Field(ge=0, le=1)] | None = None


class ScoreComponents(SchemaModel):
    """Transparent components of the deterministic candidate heuristic."""

    comfort_penalty: NonNegativeFloat
    pmv_penalty: NonNegativeFloat
    demand_penalty: NonNegativeFloat
    energy_penalty: NonNegativeFloat
    tariff_penalty: NonNegativeFloat
    carbon_penalty: NonNegativeFloat
    action_change_penalty: NonNegativeFloat
    forecast_penalty: NonNegativeFloat

    @property
    def total(self) -> float:
        """Return the sum of all penalty components."""

        return float(
            self.comfort_penalty
            + self.pmv_penalty
            + self.demand_penalty
            + self.energy_penalty
            + self.tariff_penalty
            + self.carbon_penalty
            + self.action_change_penalty
            + self.forecast_penalty
        )


class CandidateScore(SchemaModel):
    """Evaluated action candidate; lower total score is preferred."""

    candidate: CandidateAction
    components: ScoreComponents
    total_score: NonNegativeFloat
    predicted_operative_temperature_c: FiniteFloat
    predicted_demand_kw: NonNegativeFloat | None = None
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_total(self) -> CandidateScore:
        """Keep the materialized total consistent with its components."""

        if abs(float(self.total_score) - self.components.total) > 1e-8:
            raise ValueError("total_score must equal the sum of score components")
        return self


class ControlAction(SchemaModel):
    """A model/rule-proposed action with explicit clock domains.

    ``timestamp`` and ``expires_at`` are wall-clock UTC audit/freshness values.
    ``action.hold_minutes`` is the EnergyPlus simulated-time duration. The
    runtime derives its active simulated expiry from the callback simulation
    clock and that hold duration; it never compares wall time to simulation time.
    """

    action_id: Identifier
    run_id: RunId
    observation_id: int = Field(ge=1)
    action_generation: int = Field(ge=1)
    timestamp: AwareDatetime
    expires_at: AwareDatetime
    action: CandidateAction
    model: str = Field(min_length=1, max_length=255)
    latency_ms: NonNegativeFloat
    reason_code: ReasonCode
    explanation: str = Field(min_length=1, max_length=500)
    fallback: bool = False
    cache_hit: bool = False

    _normalize_timestamp = field_validator("timestamp", "expires_at", mode="after")(_as_utc)


class ValidationIssue(SchemaModel):
    """One validator rejection or warning."""

    code: ValidationCode
    message: str = Field(min_length=1, max_length=500)
    field: str | None = None


class ClampDetail(SchemaModel):
    """One transparent deterministic modification to a proposed value."""

    code: ValidationCode
    field: str
    proposed_value: FiniteFloat | int
    applied_value: FiniteFloat | int
    message: str = Field(min_length=1, max_length=500)


class ValidationResult(SchemaModel):
    """Safety result preserving both proposed and applied values.

    ``timestamp`` and ``applied_expires_at`` are wall-clock values. Simulated
    hold expiry is derived by the Runtime API integration from
    ``applied_action.hold_minutes``.
    """

    run_id: RunId
    observation_id: int = Field(ge=1)
    action_generation: int = Field(ge=1)
    timestamp: AwareDatetime
    accepted: bool
    proposed_action: CandidateAction
    applied_action: CandidateAction | None
    applied_expires_at: AwareDatetime | None = None
    issues: tuple[ValidationIssue, ...] = ()
    clamps: tuple[ClampDetail, ...] = ()
    fallback_status: str | None = None

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)
    _normalize_optional_timestamp = field_validator("applied_expires_at", mode="after")(
        _optional_as_utc
    )

    @model_validator(mode="after")
    def validate_acceptance(self) -> ValidationResult:
        """Require an applied action only for an accepted result."""

        if self.accepted and self.applied_action is None:
            raise ValueError("accepted validation requires an applied action")
        if not self.accepted and self.applied_action is not None:
            raise ValueError("rejected validation cannot include an applied action")
        if not self.accepted and self.applied_expires_at is not None:
            raise ValueError("rejected validation cannot include an applied expiry")
        return self


class ActionApplicationResult(SchemaModel):
    """Atomic database action-application outcome."""

    run_id: RunId
    observation_id: int = Field(ge=1)
    action_id: Identifier
    timestamp: AwareDatetime
    applied: bool
    idempotent_duplicate: bool = False
    rejection_code: ValidationCode | None = None
    message: str

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)


class PhysicalActuatorApplication(SchemaModel):
    """Acknowledgement written only after Runtime API actuator calls succeed.

    The simulation timestamps are the actual early-zone callback clock, not the
    observation or database-acceptance time. ``action_id`` is absent for fixed
    override and replay actions, which do not originate in ``applied_actions``.
    """

    run_id: RunId
    timestamp: AwareDatetime
    simulation_timestamp: AwareDatetime
    action_id: Identifier | None = None
    observation_id: int = Field(ge=1)
    action_generation: int = Field(ge=1)
    heating_setpoint_c: FiniteFloat
    cooling_setpoint_c: FiniteFloat
    hold_minutes: int = Field(ge=1, le=24 * 60)
    simulation_expires_at: AwareDatetime
    wall_expires_at: AwareDatetime | None = None
    validation_result: str = Field(min_length=1, max_length=100)
    fallback_status: str | None = Field(default=None, max_length=100)
    source: str = Field(min_length=1, max_length=100)
    acknowledgement_source: str = Field(
        default="runtime_callback",
        pattern=r"^(runtime_callback|legacy_verified_runtime_message)$",
    )
    heating_actuator_handles: tuple[int, ...]
    cooling_actuator_handles: tuple[int, ...]

    _normalize_timestamps = field_validator(
        "timestamp",
        "simulation_timestamp",
        "simulation_expires_at",
        mode="after",
    )(_as_utc)
    _normalize_optional_wall_expiry = field_validator("wall_expires_at", mode="after")(
        _optional_as_utc
    )

    @field_validator("heating_actuator_handles", "cooling_actuator_handles")
    @classmethod
    def validate_handles(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Require at least one valid EnergyPlus handle for each write."""

        if any(handle < 0 for handle in value):
            raise ValueError("actuator handles must be non-negative")
        if len(value) != len(set(value)):
            raise ValueError("actuator handles must be unique")
        return value

    @model_validator(mode="after")
    def validate_physical_interval(self) -> PhysicalActuatorApplication:
        """Keep setpoints ordered and simulated expiry tied to the callback."""

        if self.heating_setpoint_c >= self.cooling_setpoint_c:
            raise ValueError("physical heating setpoint must be below cooling setpoint")
        if self.acknowledgement_source == "runtime_callback" and (
            not self.heating_actuator_handles or not self.cooling_actuator_handles
        ):
            raise ValueError(
                "a runtime callback acknowledgement requires both actuator handle sets"
            )
        expected_expiry = self.simulation_timestamp + timedelta(minutes=self.hold_minutes)
        if self.simulation_expires_at != expected_expiry:
            raise ValueError(
                "simulation_expires_at must equal the physical callback timestamp plus hold_minutes"
            )
        return self


class ToolCallTrace(SchemaModel):
    """Auditable MCP tool call without private reasoning."""

    call_id: Identifier
    run_id: RunId
    timestamp: AwareDatetime
    observation_id: int | None = Field(default=None, ge=1)
    sequence: int = Field(ge=1)
    tool_name: Identifier
    arguments: JsonObject
    result: JsonObject | None = None
    success: bool
    error: str | None = None
    duration_ms: NonNegativeFloat
    control_affecting: bool = False

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)

    @model_validator(mode="after")
    def validate_error(self) -> ToolCallTrace:
        """Require an error message for a failed call."""

        if not self.success and not self.error:
            raise ValueError("failed tool call must include an error")
        return self


class EnergyCrossCheck(SchemaModel):
    """Comparison between official output and accumulated API telemetry."""

    official_kwh: NonNegativeFloat
    telemetry_kwh: NonNegativeFloat
    absolute_difference_kwh: NonNegativeFloat
    difference_percent: NonNegativeFloat
    tolerance_percent: PositiveFloat
    passed: bool


class FinalRunMetrics(SchemaModel):
    """Verified metrics for one completed real or explicitly fake test run."""

    run_id: RunId
    timestamp: AwareDatetime
    run_type: RunType
    status: RunStatus
    is_fake: bool
    simulation_start: AwareDatetime
    simulation_end: AwareDatetime
    facility_electricity_kwh: NonNegativeFloat
    hvac_electricity_kwh: NonNegativeFloat | None = None
    other_fuels_kwh: dict[str, NonNegativeFloat] = Field(default_factory=dict)
    peak_electrical_demand_kw: NonNegativeFloat
    cost: NonNegativeFloat
    operational_carbon_kg: NonNegativeFloat
    occupied_temperature_violation_percent: Annotated[float, Field(ge=0, le=100)]
    occupied_temperature_violation_degree_hours: NonNegativeFloat
    pmv_compliance_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    mean_ppd_percent: Annotated[float, Field(ge=0, le=100)] | None = None
    maximum_occupied_co2_ppm: NonNegativeFloat | None = None
    llm_decision_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    average_decision_latency_ms: NonNegativeFloat | None = None
    p95_decision_latency_ms: NonNegativeFloat | None = None
    timeout_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    invalid_action_count: int = Field(ge=0)
    safety_clamp_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    severe_count: int = Field(ge=0)
    fatal_count: int = Field(ge=0)
    energy_cross_check: EnergyCrossCheck
    source_artifacts: tuple[str, ...]

    _normalize_timestamp = field_validator(
        "timestamp", "simulation_start", "simulation_end", mode="after"
    )(_as_utc)

    @model_validator(mode="after")
    def validate_completed_metrics(self) -> FinalRunMetrics:
        """Prevent publication of metrics from an invalid production run."""

        if self.simulation_end <= self.simulation_start:
            raise ValueError("simulation_end must be after simulation_start")
        if self.status is not RunStatus.COMPLETED:
            raise ValueError("final run metrics require a completed run")
        if self.fatal_count:
            raise ValueError("completed final metrics cannot contain fatal errors")
        return self


class ComparisonMetrics(SchemaModel):
    """Verified comparison of compatible completed baseline and controlled runs."""

    baseline_run_id: RunId
    agent_run_id: RunId
    timestamp: AwareDatetime
    electricity_saving_percent: FiniteFloat
    peak_reduction_percent: FiniteFloat
    cost_saving: FiniteFloat
    cost_saving_percent: FiniteFloat
    carbon_saving_kg: FiniteFloat
    carbon_saving_percent: FiniteFloat
    baseline_facility_electricity_kwh: NonNegativeFloat
    agent_facility_electricity_kwh: NonNegativeFloat
    baseline_peak_demand_kw: NonNegativeFloat
    agent_peak_demand_kw: NonNegativeFloat
    baseline_hvac_electricity_kwh: NonNegativeFloat | None = None
    agent_hvac_electricity_kwh: NonNegativeFloat | None = None
    baseline_comfort_compliance_percent: Annotated[float, Field(ge=0, le=100)]
    agent_comfort_compliance_percent: Annotated[float, Field(ge=0, le=100)]
    other_fuels: dict[str, tuple[NonNegativeFloat, NonNegativeFloat]] = Field(default_factory=dict)

    _normalize_timestamp = field_validator("timestamp", mode="after")(_as_utc)
