"""External PyEnergyPlus Runtime/Data Exchange integration.

Every public ``run_simulation`` call creates a child process, and every child
creates and disposes a fresh EnergyPlus state. The runtime communicates through
an injected durable-store adapter; it never imports control or database internals.
"""

from __future__ import annotations

import csv
import importlib
import json
import math
import multiprocessing
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from multiprocessing.connection import Connection
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol, cast

from ecoloop.energyplus.discovery import (
    EnergyPlusInstallation,
    add_pyenergyplus_to_path,
    inspect_energyplus_root,
    require_energyplus,
)
from ecoloop.energyplus.handles import (
    ExchangeProtocol,
    HandleKind,
    HandleRecord,
    HandleRegistry,
    HandleSpec,
    default_observation_specs,
)
from ecoloop.energyplus.logs import (
    MessageSeverity,
    classify_message,
    message_digest,
    parse_error_file,
    severity_counts,
)
from ecoloop.exceptions import EnergyPlusIntegrationError, HandleDiscoveryError

_WEATHER_RUN_PERIOD_KIND = 3


class SimulationMode(StrEnum):
    """Supported simulation control modes."""

    BASELINE = "baseline"
    RULE = "rule"
    AGENT = "agent"
    REPLAY = "replay"
    FIXED_OVERRIDE = "fixed_override"


@dataclass(frozen=True, slots=True)
class SimulationRequest:
    """Serializable request passed to the fresh EnergyPlus child process."""

    run_id: str
    model_path: Path
    weather_path: Path
    output_directory: Path
    database_path: Path
    mode: SimulationMode = SimulationMode.BASELINE
    energyplus_home: Path | None = None
    store_adapter: str = "ecoloop.energyplus.store_adapter:open_runtime_store"
    actuator_map_path: Path | None = None
    replay_schedule_path: Path | None = None
    include_design_days: bool = False
    maximum_action_hold_minutes: int = 120
    process_timeout_seconds: float = 1800.0
    decision_wait_seconds: float = 0.0
    decision_poll_seconds: float = 0.10
    decision_interval_minutes: int = 60
    demand_event_threshold_kw: float = 75.0
    display_delay_seconds: float = 0.0
    fixed_heating_setpoint_c: float | None = None
    fixed_cooling_setpoint_c: float | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if self.maximum_action_hold_minutes < 1:
            raise ValueError("maximum_action_hold_minutes must be positive")
        if self.process_timeout_seconds <= 0:
            raise ValueError("process_timeout_seconds must be positive")
        if not 0 <= self.decision_wait_seconds <= 300:
            raise ValueError("decision_wait_seconds must be in 0..300")
        if not 0.01 <= self.decision_poll_seconds <= 5:
            raise ValueError("decision_poll_seconds must be in 0.01..5")
        if self.decision_interval_minutes < 1:
            raise ValueError("decision_interval_minutes must be positive")
        if self.demand_event_threshold_kw <= 0:
            raise ValueError("demand_event_threshold_kw must be positive")
        if not 0 <= self.display_delay_seconds <= 10:
            raise ValueError("display_delay_seconds must be in 0..10")
        if self.mode is SimulationMode.REPLAY and self.replay_schedule_path is None:
            raise ValueError("replay mode requires replay_schedule_path")
        if self.mode is SimulationMode.FIXED_OVERRIDE:
            if self.fixed_heating_setpoint_c is None or self.fixed_cooling_setpoint_c is None:
                raise ValueError("fixed_override mode requires heating and cooling values")
            if self.fixed_heating_setpoint_c >= self.fixed_cooling_setpoint_c:
                raise ValueError("fixed override heating must be below cooling")


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Outcome returned by a simulation child process."""

    run_id: str
    status: str
    exit_code: int | None
    output_directory: Path
    elapsed_seconds: float
    progress_percent: int
    warning_count: int
    severe_count: int
    fatal_count: int
    observation_count: int
    applied_action_count: int
    error: str | None = None

    @property
    def completed(self) -> bool:
        """Return whether EnergyPlus exited cleanly without fatal diagnostics."""

        return self.status == "completed" and self.exit_code == 0 and self.fatal_count == 0


@dataclass(frozen=True, slots=True)
class TimestepIdentity:
    """Deterministic zone-timestep identity used for duplicate prevention."""

    run_id: str
    environment: int
    year: int
    month: int
    day: int
    hour: int
    minute: int
    timestep: int

    @property
    def key(self) -> str:
        """Return a stable delimiter-separated identity."""

        return (
            f"{self.run_id}|env={self.environment}|{self.year:04d}-{self.month:02d}-"
            f"{self.day:02d}|h={self.hour:02d}|m={self.minute:02d}|step={self.timestep:02d}"
        )


@dataclass(frozen=True, slots=True)
class RuntimeAction:
    """Minimal already-validated action consumed at the simulation boundary."""

    run_id: str
    observation_id: int
    action_generation: int
    heating_setpoint_c: float
    cooling_setpoint_c: float
    expires_at: datetime | None
    validation_result: str
    hold_minutes: int = 60
    wall_expires_at: datetime | None = None
    fallback_status: bool = False
    source: str = "store"


class RuntimeStore(Protocol):
    """Adapter contract implemented by the SQLite communication bus."""

    def create_run(self, run_id: str, values: Mapping[str, Any]) -> None: ...

    def update_run(self, run_id: str, values: Mapping[str, Any]) -> None: ...

    def insert_telemetry(
        self,
        run_id: str,
        timestamp: str,
        timestep_identity: str,
        values: Mapping[str, Any],
    ) -> None: ...

    def insert_zone_telemetry(
        self,
        run_id: str,
        timestamp: str,
        timestep_identity: str,
        zone_name: str,
        values: Mapping[str, Any],
    ) -> None: ...

    def insert_observation(
        self,
        run_id: str,
        timestamp: str,
        timestep_identity: str,
        values: Mapping[str, Any],
    ) -> int: ...

    def latest_applicable_action(
        self,
        run_id: str,
        observation_id: int,
    ) -> Mapping[str, Any] | None: ...

    def record_applied_action(
        self,
        run_id: str,
        timestamp: str,
        values: Mapping[str, Any],
    ) -> None: ...

    def record_simulation_message(
        self,
        run_id: str,
        timestamp: str,
        severity: str,
        digest: str,
        message: str,
    ) -> None: ...

    def record_error(
        self,
        run_id: str,
        timestamp: str,
        severity: str,
        digest: str,
        message: str,
    ) -> None: ...

    def record_artifact(
        self,
        run_id: str,
        timestamp: str,
        artifact_type: str,
        path: str,
    ) -> None: ...

    def record_metric(
        self,
        run_id: str,
        timestamp: str,
        name: str,
        value: float,
        units: str,
    ) -> None: ...

    def close(self) -> None: ...


class RuntimeProtocol(Protocol):
    """Subset of the Runtime API used by a session."""

    def callback_message(self, state: object, callback: Callable[[bytes], None]) -> None: ...

    def callback_progress(self, state: object, callback: Callable[[int], None]) -> None: ...

    def callback_end_zone_timestep_after_zone_reporting(
        self, state: object, callback: Callable[[object], None]
    ) -> None: ...

    def callback_begin_zone_timestep_before_set_current_weather(
        self, state: object, callback: Callable[[object], None]
    ) -> None: ...

    def run_energyplus(self, state: object, command_line_args: list[str]) -> int: ...

    def stop_simulation(self, state: object) -> None: ...


class StateManagerProtocol(Protocol):
    """Subset of the EnergyPlus state manager."""

    def new_state(self) -> object: ...

    def delete_state(self, state: object) -> None: ...


class APIProtocol(Protocol):
    """EnergyPlus API object shape used by the session."""

    runtime: RuntimeProtocol
    exchange: ExchangeProtocol
    state_manager: StateManagerProtocol


class RuntimeExchangeProtocol(ExchangeProtocol, Protocol):
    """Data Exchange methods used after handle discovery."""

    def warmup_flag(self, state: object) -> bool: ...

    def kind_of_sim(self, state: object) -> int: ...

    def current_environment_num(self, state: object) -> int: ...

    def year(self, state: object) -> int: ...

    def month(self, state: object) -> int: ...

    def day_of_month(self, state: object) -> int: ...

    def hour(self, state: object) -> int: ...

    def minutes(self, state: object) -> int: ...

    def num_time_steps_in_hour(self, state: object) -> int: ...

    def zone_time_step_number(self, state: object) -> int: ...

    def get_variable_value(self, state: object, handle: int) -> float: ...

    def get_meter_value(self, state: object, handle: int) -> float: ...

    def set_actuator_value(self, state: object, handle: int, value: float) -> None: ...

    def reset_actuator(self, state: object, handle: int) -> None: ...


def energyplus_clock_to_datetime(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
) -> datetime:
    """Convert EnergyPlus's 1-24/end-minute clock to a simulation datetime."""

    safe_year = year if year >= 1 else 2001
    if not 0 <= hour <= 23:
        raise ValueError(f"EnergyPlus API hour must be between 0 and 23, got {hour}")
    if not 0 <= minute <= 60:
        raise ValueError(f"EnergyPlus minute must be between 0 and 60, got {minute}")
    base = datetime(safe_year, month, day)  # noqa: DTZ001 - simulation-local calendar
    return base + timedelta(hours=hour, minutes=minute)


def make_timestep_identity(
    run_id: str,
    exchange: RuntimeExchangeProtocol,
    state: object,
) -> tuple[TimestepIdentity, datetime]:
    """Read one deterministic EnergyPlus zone-timestep identity."""

    year = int(exchange.year(state))
    month = int(exchange.month(state))
    day = int(exchange.day_of_month(state))
    hour = int(exchange.hour(state))
    timestep = int(exchange.zone_time_step_number(state))
    steps_per_hour = int(exchange.num_time_steps_in_hour(state))
    if steps_per_hour < 1 or not 1 <= timestep <= steps_per_hour:
        raise ValueError(f"Invalid EnergyPlus zone timestep {timestep}/{steps_per_hour}.")
    # Derive the minute from the deterministic zone-timestep index. This
    # avoids relying on HVAC-substep clock values at a zone reporting callback.
    minute = round(60 * timestep / steps_per_hour)
    identity = TimestepIdentity(
        run_id=run_id,
        environment=int(exchange.current_environment_num(state)),
        year=year if year >= 1 else 2001,
        month=month,
        day=day,
        hour=hour,
        minute=minute,
        timestep=timestep,
    )
    return identity, energyplus_clock_to_datetime(year, month, day, hour, minute)


def should_process_environment(
    exchange: RuntimeExchangeProtocol,
    state: object,
    *,
    include_design_days: bool,
) -> bool:
    """Guard callbacks against warmup, sizing, and design-day data."""

    if exchange.warmup_flag(state):
        return False
    if include_design_days:
        return True
    return int(exchange.kind_of_sim(state)) == _WEATHER_RUN_PERIOD_KIND


def action_from_mapping(value: Mapping[str, Any]) -> RuntimeAction:
    """Parse the narrow runtime action contract from a durable-store row."""

    expiry_raw = value.get("expires_at", value.get("expiry"))
    if isinstance(expiry_raw, datetime):
        wall_expiry = expiry_raw
    elif isinstance(expiry_raw, str):
        wall_expiry = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
    else:
        raise ValueError("Runtime action is missing an ISO expires_at/expiry")
    if wall_expiry.tzinfo is None or wall_expiry.utcoffset() is None:
        raise ValueError("Runtime action wall-clock expiry must be timezone-aware")
    wall_expiry = wall_expiry.astimezone(UTC)

    heating = value.get("applied_heating_setpoint_c", value.get("heating_setpoint_c"))
    cooling = value.get("applied_cooling_setpoint_c", value.get("cooling_setpoint_c"))
    hold_minutes = int(value.get("hold_minutes", 0))
    if hold_minutes < 1:
        raise ValueError("Runtime action is missing a positive hold_minutes")
    simulation_expiry_raw = value.get("simulation_expires_at")
    if isinstance(simulation_expiry_raw, datetime):
        simulation_expiry = simulation_expiry_raw
    elif isinstance(simulation_expiry_raw, str):
        simulation_expiry = datetime.fromisoformat(simulation_expiry_raw.replace("Z", "+00:00"))
    else:
        simulation_expiry = None
    if simulation_expiry is not None and simulation_expiry.tzinfo is not None:
        simulation_expiry = simulation_expiry.replace(tzinfo=None)
    action = RuntimeAction(
        run_id=str(value.get("run_id", "")),
        observation_id=int(value.get("observation_id", 0)),
        action_generation=int(value.get("action_generation", 0)),
        heating_setpoint_c=float(heating),
        cooling_setpoint_c=float(cooling),
        # This is replaced with a simulation-time expiry when first applied.
        expires_at=simulation_expiry,
        validation_result=str(value.get("validation_result", "")),
        hold_minutes=hold_minutes,
        wall_expires_at=wall_expiry,
        fallback_status=bool(value.get("fallback_status", False)),
        source=str(value.get("source", "store")),
    )
    for number in (action.heating_setpoint_c, action.cooling_setpoint_c):
        if not math.isfinite(number):
            raise ValueError("Runtime action setpoints must be finite")
    if action.heating_setpoint_c >= action.cooling_setpoint_c:
        raise ValueError("Runtime action heating setpoint must be below cooling setpoint")
    if action.observation_id < 1 or action.action_generation < 1:
        raise ValueError("Runtime action identifiers must be positive")
    return action


def bind_action_to_simulation_clock(
    action: RuntimeAction,
    simulation_time: datetime,
    maximum_hold_minutes: int,
) -> RuntimeAction:
    """Bind a wall-valid action to its bounded simulated hold interval."""

    if not 1 <= action.hold_minutes <= maximum_hold_minutes:
        raise ValueError(
            f"Action hold {action.hold_minutes} minutes exceeds runtime maximum "
            f"{maximum_hold_minutes}."
        )
    expiry = (
        simulation_time + timedelta(minutes=action.hold_minutes)
        if action.expires_at is None
        else action.expires_at
    )
    if expiry <= simulation_time:
        raise ValueError("Action simulation expiry is not after the application timestep")
    if expiry - simulation_time > timedelta(minutes=maximum_hold_minutes):
        raise ValueError("Action simulation expiry exceeds the maximum hold duration")
    return replace(action, expires_at=expiry)


def validate_runtime_action(
    action: RuntimeAction,
    *,
    run_id: str,
    expected_observation_id: int,
    last_generation: int,
    simulation_time: datetime,
    wall_time: datetime | None = None,
) -> tuple[bool, str]:
    """Enforce freshness, run isolation, monotonicity, and idempotency at actuation."""

    if action.run_id != run_id:
        return False, "wrong_run"
    if action.observation_id != expected_observation_id:
        return False, "stale_observation"
    if action.action_generation <= last_generation:
        return False, "duplicate_or_non_monotonic_generation"
    del simulation_time
    checked_wall_time = wall_time or datetime.now(UTC)
    if checked_wall_time.tzinfo is None or checked_wall_time.utcoffset() is None:
        raise ValueError("wall_time must be timezone-aware")
    if action.wall_expires_at is not None and action.wall_expires_at <= checked_wall_time:
        return False, "expired"
    if action.validation_result.casefold() not in {"valid", "clamped", "fallback", "accepted"}:
        return False, "not_safety_validated"
    return True, "accepted"


def actuator_specs_from_map(path: Path) -> tuple[HandleSpec, ...]:
    """Load the restricted actuator capability map generated during model preparation."""

    if not path.is_file():
        raise EnergyPlusIntegrationError(f"Actuator map does not exist: {path}")
    specs: list[HandleSpec] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required_fields = {
            "logical_action",
            "component_type",
            "control_type",
            "actuator_key",
            "required",
        }
        if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
            raise EnergyPlusIntegrationError(
                f"Actuator map {path} is missing columns: "
                f"{', '.join(sorted(required_fields - set(reader.fieldnames or ())))}."
            )
        for row in reader:
            specs.append(
                HandleSpec(
                    logical_metric=str(row["logical_action"]),
                    kind=HandleKind.ACTUATOR,
                    name=str(row["component_type"]),
                    control_type=str(row["control_type"]),
                    key=str(row["actuator_key"]),
                    required=str(row["required"]).casefold() in {"1", "true", "yes"},
                    units="C",
                )
            )
    if not specs:
        raise EnergyPlusIntegrationError(f"Actuator map {path} contains no capabilities.")
    return tuple(specs)


class ReplaySchedule:
    """Read and select validated actions from an exported real-run schedule."""

    def __init__(self, path: Path, run_id: str) -> None:
        self._actions: list[tuple[datetime, RuntimeAction]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                timestamp = datetime.fromisoformat(str(row["simulation_timestamp"])).replace(
                    tzinfo=None
                )
                hold = int(row["hold_minutes"])
                action = RuntimeAction(
                    run_id=run_id,
                    observation_id=int(row["observation_id"]),
                    action_generation=int(row["action_generation"]),
                    heating_setpoint_c=float(row["heating_setpoint_c"]),
                    cooling_setpoint_c=float(row["cooling_setpoint_c"]),
                    expires_at=timestamp + timedelta(minutes=hold),
                    validation_result="accepted",
                    hold_minutes=hold,
                    source="replay",
                )
                self._actions.append((timestamp, action))
        if not self._actions:
            raise EnergyPlusIntegrationError(f"Replay schedule has no actions: {path}")
        self._actions.sort(key=lambda item: (item[0], item[1].action_generation))

    def action_at(self, simulation_time: datetime) -> RuntimeAction | None:
        """Return the latest non-expired action at a simulated timestamp."""

        candidates = [
            action
            for timestamp, action in self._actions
            if action.expires_at is not None and timestamp <= simulation_time < action.expires_at
        ]
        return candidates[-1] if candidates else None


def _aggregate(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "mean": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
    }


def load_fanger_people_zone_map(model_path: Path) -> dict[str, str]:
    """Load the structured People-to-zone map recorded during preparation."""

    manifest = model_path.resolve().parent / "preparation-manifest.json"
    if not manifest.is_file():
        return {}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnergyPlusIntegrationError(
            f"Prepared-model manifest cannot be read: {manifest}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise EnergyPlusIntegrationError(
            f"Prepared-model manifest must contain a JSON object: {manifest}"
        )
    section_name = "baseline" if model_path.stem.casefold() == "baseline" else "agent_ready"
    section = payload.get(section_name)
    if not isinstance(section, Mapping):
        return {}
    raw_mapping = section.get("fanger_people_zone_map")
    if raw_mapping is None:
        return {}
    if not isinstance(raw_mapping, Mapping):
        raise EnergyPlusIntegrationError(f"fanger_people_zone_map must be an object in {manifest}")
    mapping: dict[str, str] = {}
    for people_name, zone_name in raw_mapping.items():
        if (
            not isinstance(people_name, str)
            or not people_name
            or not isinstance(zone_name, str)
            or not zone_name
        ):
            raise EnergyPlusIntegrationError(
                f"fanger_people_zone_map contains an invalid entry in {manifest}"
            )
        mapping[people_name.casefold()] = zone_name
    return mapping


class EnergyPlusSession:
    """Own callbacks and mutable state for exactly one EnergyPlus state."""

    def __init__(
        self,
        request: SimulationRequest,
        api: APIProtocol,
        store: RuntimeStore,
        registry: HandleRegistry,
    ) -> None:
        self.request = request
        self.api = api
        self.exchange = cast(RuntimeExchangeProtocol, api.exchange)
        self.store = store
        self.registry = registry
        self.progress = 0
        self.observation_count = 0
        self.applied_action_count = 0
        self.last_observation_id = 0
        self.last_generation = 0
        self.active_action: RuntimeAction | None = None
        self._seen_timesteps: set[str] = set()
        self._seen_messages: set[str] = set()
        self._cumulative_electricity_kwh = 0.0
        self._cumulative_hvac_kwh = 0.0
        self._callback_error: str | None = None
        self._pending_action: RuntimeAction | None = None
        self._last_decision_simulation_time: datetime | None = None
        self._previous_observation_occupied: bool | None = None
        self._previous_observation_outdoor_c: float | None = None
        self._previous_comfort_risk: bool | None = None
        self._previous_pmv_risk: bool | None = None
        self._previous_co2_risk: bool | None = None
        self._previous_demand_risk: bool | None = None
        self._action_expired_since_last_observation = False
        self._fanger_people_zone_map = load_fanger_people_zone_map(request.model_path)
        self._replay = (
            ReplaySchedule(cast(Path, request.replay_schedule_path), request.run_id)
            if request.mode is SimulationMode.REPLAY
            else None
        )

    def register_callbacks(self, state: object) -> None:
        """Register message/progress/observation/actuation callbacks."""

        runtime = self.api.runtime
        runtime.callback_message(state, self._on_message)
        runtime.callback_progress(state, self._on_progress)
        runtime.callback_end_zone_timestep_after_zone_reporting(state, self._on_observation)
        runtime.callback_begin_zone_timestep_before_set_current_weather(state, self._on_actuation)

    def _now_text(self) -> str:
        return datetime.now().astimezone().isoformat()

    def _on_message(self, raw: bytes) -> None:
        message = raw.decode("utf-8", errors="replace").strip()
        if not message:
            return
        severity = classify_message(message)
        digest = message_digest(severity, message)
        if digest in self._seen_messages:
            return
        self._seen_messages.add(digest)
        self.store.record_simulation_message(
            self.request.run_id,
            self._now_text(),
            severity.value,
            digest,
            message,
        )

    def _on_progress(self, progress: int) -> None:
        self.progress = max(0, min(100, int(progress)))
        self.store.update_run(
            self.request.run_id,
            {"progress_percent": self.progress, "updated_at": self._now_text()},
        )

    def _stop_after_callback_error(self, state: object, exc: Exception) -> None:
        if self._callback_error is not None:
            return
        self._callback_error = str(exc)
        message = f"EnergyPlus callback failed: {exc}"
        digest = message_digest(MessageSeverity.FATAL, message)
        self.store.record_error(
            self.request.run_id,
            self._now_text(),
            MessageSeverity.FATAL.value,
            digest,
            message,
        )
        self.api.runtime.stop_simulation(state)

    def _ensure_handles(self, state: object) -> bool:
        if self.registry.is_resolved:
            return True
        if not self.exchange.api_data_fully_ready(state):
            return False
        self.registry.resolve(self.exchange, state, self.request.output_directory)
        self.store.record_artifact(
            self.request.run_id,
            self._now_text(),
            "energyplus_api_points",
            str(self.request.output_directory / "api_points.csv"),
        )
        return True

    def _read_records(self, state: object) -> dict[str, list[tuple[str, float]]]:
        readings: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for record in self.registry.records:
            if not record.available or record.spec.kind is HandleKind.ACTUATOR:
                continue
            if record.spec.kind is HandleKind.VARIABLE:
                value = float(self.exchange.get_variable_value(state, record.handle))
            else:
                value = float(self.exchange.get_meter_value(state, record.handle))
            if not math.isfinite(value):
                raise EnergyPlusIntegrationError(
                    f"Non-finite EnergyPlus value for {record.spec.requested_label()}."
                )
            readings[record.spec.logical_metric].append((record.spec.key, value))
        return readings

    def _on_observation(self, state: object) -> None:
        try:
            if not should_process_environment(
                self.exchange,
                state,
                include_design_days=self.request.include_design_days,
            ):
                return
            if not self._ensure_handles(state):
                return
            identity, simulation_time = make_timestep_identity(
                self.request.run_id, self.exchange, state
            )
            if identity.key in self._seen_timesteps:
                return
            self._seen_timesteps.add(identity.key)
            readings = self._read_records(state)
            timestamp = simulation_time.isoformat()

            zone_rows: dict[str, dict[str, float]] = defaultdict(dict)
            building_values: dict[str, Any] = {}
            for metric, keyed_values in readings.items():
                if metric.startswith("zone_") or metric.endswith("_setpoint_c"):
                    for key, value in keyed_values:
                        zone_key = (
                            self._fanger_people_zone_map.get(key.casefold(), key)
                            if metric in {"zone_pmv", "zone_ppd"}
                            else key
                        )
                        zone_rows[zone_key][metric] = value
                elif keyed_values:
                    building_values[metric] = keyed_values[0][1]
            zone_rows = defaultdict(
                dict,
                {
                    zone_name: values
                    for zone_name, values in zone_rows.items()
                    if values.get("heating_setpoint_c", 0.0) < values.get("cooling_setpoint_c", 0.0)
                },
            )
            if not zone_rows:
                raise EnergyPlusIntegrationError(
                    "No conditioned zone had an ordered heating/cooling thermostat pair."
                )

            facility_energy = building_values.get("facility_electricity_j")
            facility_net_energy = building_values.get("facility_net_electricity_j")
            if facility_energy is not None:
                timestep_electricity_j = float(facility_energy)
                electricity_signal = "Electricity:Facility"
            elif facility_net_energy is not None:
                timestep_electricity_j = float(facility_net_energy)
                electricity_signal = "ElectricityNet:Facility"
            else:
                raise EnergyPlusIntegrationError(
                    "Neither Electricity:Facility nor ElectricityNet:Facility has "
                    "an available Runtime API handle."
                )
            timestep_hvac_raw = building_values.get("hvac_electricity_j")
            timestep_hvac_j = float(timestep_hvac_raw) if timestep_hvac_raw is not None else None
            self._cumulative_electricity_kwh += timestep_electricity_j / 3_600_000.0
            if timestep_hvac_j is not None:
                self._cumulative_hvac_kwh += timestep_hvac_j / 3_600_000.0
            building_values.update(
                {
                    "timestep_energy_kwh": timestep_electricity_j / 3_600_000.0,
                    "cumulative_energy_kwh": self._cumulative_electricity_kwh,
                    "runtime_electricity_signal": electricity_signal,
                    "environment": identity.environment,
                    "simulation_timestamp": timestamp,
                }
            )
            if timestep_hvac_j is not None:
                building_values["timestep_hvac_energy_kwh"] = timestep_hvac_j / 3_600_000.0
                building_values["cumulative_hvac_energy_kwh"] = self._cumulative_hvac_kwh

            observation: dict[str, Any] = dict(building_values)
            for source_metric, target_name in (
                ("zone_air_temperature_c", "zone_temperature"),
                ("zone_operative_temperature_c", "operative_temperature"),
                ("zone_relative_humidity_pct", "relative_humidity"),
                ("zone_pmv", "pmv"),
                ("zone_ppd", "ppd"),
                ("zone_co2_ppm", "co2"),
                ("heating_setpoint_c", "heating_setpoint"),
                ("cooling_setpoint_c", "cooling_setpoint"),
            ):
                aggregate = _aggregate(
                    [
                        values[source_metric]
                        for values in zone_rows.values()
                        if source_metric in values
                    ]
                )
                if aggregate is not None:
                    observation[target_name] = aggregate
                    if target_name in {"heating_setpoint", "cooling_setpoint"}:
                        building_values[target_name] = aggregate
            occupancy = [
                values["zone_occupant_count"]
                for values in zone_rows.values()
                if "zone_occupant_count" in values
            ]
            if occupancy:
                observation["occupancy_count"] = sum(occupancy)
                observation["occupied"] = sum(occupancy) > 0
            observation["zone_count"] = len(zone_rows)
            self.store.insert_telemetry(
                self.request.run_id,
                timestamp,
                identity.key,
                building_values,
            )
            for zone_name, values in sorted(zone_rows.items()):
                self.store.insert_zone_telemetry(
                    self.request.run_id,
                    timestamp,
                    identity.key,
                    zone_name,
                    values,
                )
            self.last_observation_id = self.store.insert_observation(
                self.request.run_id,
                timestamp,
                identity.key,
                observation,
            )
            self.observation_count += 1
            if self._decision_due(
                simulation_time,
                observation,
            ) and self._wait_for_action(simulation_time):
                self._last_decision_simulation_time = simulation_time
            if self.request.display_delay_seconds:
                time.sleep(self.request.display_delay_seconds)
        except (
            EnergyPlusIntegrationError,
            HandleDiscoveryError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            self._stop_after_callback_error(state, exc)

    def _actuator_records(self, logical_metric: str) -> tuple[HandleRecord, ...]:
        return tuple(
            record
            for record in self.registry.by_logical_metric(logical_metric)
            if record.available and record.spec.kind is HandleKind.ACTUATOR
        )

    def _reset_actuators(self, state: object) -> None:
        for metric in ("heating_setpoint_c", "cooling_setpoint_c"):
            for record in self._actuator_records(metric):
                self.exchange.reset_actuator(state, record.handle)
        self.active_action = None

    def _decision_due(
        self,
        simulation_time: datetime,
        observation: Mapping[str, Any],
    ) -> bool:
        if (
            self.request.mode not in {SimulationMode.AGENT, SimulationMode.RULE}
            or self.request.decision_wait_seconds <= 0
        ):
            return False
        occupied = bool(observation.get("occupied", False))
        outdoor_raw = observation.get("outdoor_drybulb_c")
        outdoor = float(outdoor_raw) if outdoor_raw is not None else None
        interval_due = (
            self._last_decision_simulation_time is None
            or simulation_time - self._last_decision_simulation_time
            >= timedelta(minutes=self.request.decision_interval_minutes)
        )
        occupancy_event = (
            self._previous_observation_occupied is not None
            and occupied != self._previous_observation_occupied
        )
        outdoor_event = (
            outdoor is not None
            and self._previous_observation_outdoor_c is not None
            and abs(outdoor - self._previous_observation_outdoor_c) >= 3.0
        )
        operative = observation.get(
            "operative_temperature",
            observation.get("zone_temperature"),
        )
        comfort_risk = False
        if occupied and isinstance(operative, Mapping):
            minimum = operative.get("min")
            maximum = operative.get("max")
            comfort_risk = bool(
                (minimum is not None and float(minimum) <= 22.5)
                or (maximum is not None and float(maximum) >= 25.5)
            )
        comfort_event = comfort_risk and self._previous_comfort_risk is False
        pmv = observation.get("pmv")
        pmv_risk = False
        if occupied and isinstance(pmv, Mapping):
            minimum = pmv.get("min")
            maximum = pmv.get("max")
            pmv_risk = bool(
                (minimum is not None and abs(float(minimum)) >= 0.6)
                or (maximum is not None and abs(float(maximum)) >= 0.6)
            )
        pmv_event = pmv_risk and self._previous_pmv_risk is False
        demand_raw = observation.get("facility_demand_w")
        demand_risk = bool(
            demand_raw is not None
            and float(demand_raw) / 1_000.0 >= self.request.demand_event_threshold_kw
        )
        demand_event = demand_risk and self._previous_demand_risk is False
        co2 = observation.get("co2")
        co2_risk = False
        if occupied and isinstance(co2, Mapping):
            maximum = co2.get("max")
            co2_risk = maximum is not None and float(maximum) >= 900.0
        co2_event = co2_risk and self._previous_co2_risk is False
        expiry_event = self._action_expired_since_last_observation
        self._previous_observation_occupied = occupied
        self._previous_observation_outdoor_c = outdoor
        self._previous_comfort_risk = comfort_risk
        self._previous_pmv_risk = pmv_risk
        self._previous_co2_risk = co2_risk
        self._previous_demand_risk = demand_risk
        self._action_expired_since_last_observation = False
        due = (
            interval_due
            or occupancy_event
            or outdoor_event
            or comfort_event
            or pmv_event
            or co2_event
            or demand_event
            or expiry_event
        )
        return due

    def _wait_for_action(self, simulation_time: datetime) -> bool:
        deadline = time.monotonic() + self.request.decision_wait_seconds
        started = time.monotonic()
        while time.monotonic() < deadline:
            stored = self.store.latest_applicable_action(
                self.request.run_id,
                self.last_observation_id,
            )
            if stored is not None:
                action = action_from_mapping(stored)
                accepted, _ = validate_runtime_action(
                    action,
                    run_id=self.request.run_id,
                    expected_observation_id=self.last_observation_id,
                    last_generation=self.last_generation,
                    simulation_time=simulation_time,
                    wall_time=datetime.now(UTC),
                )
                if accepted:
                    self._pending_action = action
                    self.store.record_simulation_message(
                        self.request.run_id,
                        self._now_text(),
                        MessageSeverity.INFORMATION.value,
                        message_digest(
                            MessageSeverity.INFORMATION,
                            f"decision action ready {self.last_observation_id}",
                        ),
                        f"Bounded decision wait received action for observation "
                        f"{self.last_observation_id} after "
                        f"{time.monotonic() - started:.3f} seconds.",
                    )
                    return True
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.request.decision_poll_seconds, remaining))
        self.store.record_simulation_message(
            self.request.run_id,
            self._now_text(),
            MessageSeverity.WARNING.value,
            message_digest(
                MessageSeverity.WARNING,
                f"decision wait timeout {self.last_observation_id}",
            ),
            f"Bounded decision wait timed out for observation "
            f"{self.last_observation_id} after {self.request.decision_wait_seconds:.3f} "
            "seconds; simulation continues under the active or baseline-safe schedule.",
        )
        return False

    def _fixed_action(self, simulation_time: datetime) -> RuntimeAction | None:
        if self.request.mode is not SimulationMode.FIXED_OVERRIDE:
            return None
        heating = self.request.fixed_heating_setpoint_c
        cooling = self.request.fixed_cooling_setpoint_c
        if heating is None or cooling is None:
            return None
        return RuntimeAction(
            run_id=self.request.run_id,
            observation_id=max(1, self.last_observation_id),
            action_generation=self.last_generation + 1,
            heating_setpoint_c=heating,
            cooling_setpoint_c=cooling,
            expires_at=simulation_time
            + timedelta(minutes=self.request.maximum_action_hold_minutes),
            validation_result="accepted",
            hold_minutes=self.request.maximum_action_hold_minutes,
            source="fixed_override",
        )

    def _candidate_action(self, simulation_time: datetime) -> RuntimeAction | None:
        if self._pending_action is not None:
            action = self._pending_action
            self._pending_action = None
            return action
        if self._replay is not None:
            return self._replay.action_at(simulation_time)
        fixed = self._fixed_action(simulation_time)
        if fixed is not None:
            return fixed
        if self.request.mode is SimulationMode.BASELINE or self.last_observation_id < 1:
            return None
        stored = self.store.latest_applicable_action(
            self.request.run_id,
            self.last_observation_id,
        )
        return action_from_mapping(stored) if stored is not None else None

    def _apply(self, state: object, simulation_time: datetime, action: RuntimeAction) -> None:
        capabilities = {
            "heating_setpoint_c": self._actuator_records("heating_setpoint_c"),
            "cooling_setpoint_c": self._actuator_records("cooling_setpoint_c"),
        }
        missing = [name for name, records in capabilities.items() if not records]
        if missing:
            raise EnergyPlusIntegrationError(
                f"Cannot apply unsupported actuators: {', '.join(missing)}"
            )
        for record in capabilities["heating_setpoint_c"]:
            self.exchange.set_actuator_value(
                state,
                record.handle,
                action.heating_setpoint_c,
            )
        for record in capabilities["cooling_setpoint_c"]:
            self.exchange.set_actuator_value(
                state,
                record.handle,
                action.cooling_setpoint_c,
            )
        applied_action = bind_action_to_simulation_clock(
            action,
            simulation_time,
            self.request.maximum_action_hold_minutes,
        )
        self.last_generation = applied_action.action_generation
        self.active_action = applied_action
        self.applied_action_count += 1
        self.store.record_applied_action(
            self.request.run_id,
            simulation_time.isoformat(),
            {
                "run_id": applied_action.run_id,
                "observation_id": applied_action.observation_id,
                "action_generation": applied_action.action_generation,
                "applied_heating_setpoint_c": applied_action.heating_setpoint_c,
                "applied_cooling_setpoint_c": applied_action.cooling_setpoint_c,
                "simulation_expires_at": (
                    applied_action.expires_at.isoformat()
                    if applied_action.expires_at is not None
                    else None
                ),
                "wall_expires_at": (
                    applied_action.wall_expires_at.isoformat()
                    if applied_action.wall_expires_at is not None
                    else None
                ),
                "hold_minutes": applied_action.hold_minutes,
                "validation_result": applied_action.validation_result,
                "fallback_status": applied_action.fallback_status,
                "source": applied_action.source,
            },
        )

    def _on_actuation(self, state: object) -> None:
        try:
            # The "before set current weather" calling point is intentionally
            # early for actuation, but some meter handles are not initialized
            # there. Discovery occurs at the end-of-zone reporting callback;
            # actuation begins on the following timestep.
            if not self.registry.is_resolved:
                return
            if not should_process_environment(
                self.exchange,
                state,
                include_design_days=self.request.include_design_days,
            ):
                return
            _, simulation_time = make_timestep_identity(self.request.run_id, self.exchange, state)
            if (
                self.active_action is not None
                and self.active_action.expires_at is not None
                and self.active_action.expires_at <= simulation_time
            ):
                self._reset_actuators(state)
                self._action_expired_since_last_observation = True
            action = self._candidate_action(simulation_time)
            if action is None:
                return
            if action.source in {"replay", "fixed_override"}:
                if action.action_generation <= self.last_generation:
                    # Keep the currently active override; do not duplicate the audit row.
                    return
                accepted, reason = True, "accepted"
            else:
                accepted, reason = validate_runtime_action(
                    action,
                    run_id=self.request.run_id,
                    expected_observation_id=self.last_observation_id,
                    last_generation=self.last_generation,
                    simulation_time=simulation_time,
                )
            if not accepted:
                self.store.record_error(
                    self.request.run_id,
                    simulation_time.isoformat(),
                    MessageSeverity.WARNING.value,
                    message_digest(MessageSeverity.WARNING, reason),
                    f"Ignored runtime action: {reason}",
                )
                return
            self._apply(state, simulation_time, action)
            self._action_expired_since_last_observation = False
        except (
            EnergyPlusIntegrationError,
            HandleDiscoveryError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            self._stop_after_callback_error(state, exc)

    @property
    def callback_error(self) -> str | None:
        """Return the first callback failure, if any."""

        return self._callback_error


def _load_store(reference: str, database_path: Path, run_id: str) -> RuntimeStore:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise EnergyPlusIntegrationError(
            "store_adapter must use the form 'package.module:factory'."
        )
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise EnergyPlusIntegrationError(
            f"Runtime store adapter {reference!r} is unavailable: {exc}"
        ) from exc
    if not callable(factory):
        raise EnergyPlusIntegrationError(f"Runtime store adapter {reference!r} is not callable.")
    return cast(RuntimeStore, factory(database_path=database_path, run_id=run_id))


def _load_api(installation: EnergyPlusInstallation) -> APIProtocol:
    add_pyenergyplus_to_path(installation)
    try:
        module: ModuleType = importlib.import_module("pyenergyplus.api")
        api_class = module.__dict__["EnergyPlusAPI"]
    except (ImportError, AttributeError) as exc:
        raise EnergyPlusIntegrationError(
            f"Could not import EnergyPlusAPI from {installation.home}: {exc}"
        ) from exc
    return cast(APIProtocol, api_class())


def _installation_for_request(request: SimulationRequest) -> EnergyPlusInstallation:
    if request.energyplus_home is not None:
        installation = inspect_energyplus_root(
            request.energyplus_home,
            "simulation request",
        )
        if not installation.is_runtime_complete:
            raise EnergyPlusIntegrationError(
                f"Requested EnergyPlus installation is incomplete or not version 26.1.0: "
                f"{installation.home}"
            )
        return installation

    class _RequestSettings:
        energyplus_home: Path | None = None

    return require_energyplus(_RequestSettings())


def _validate_request_files(request: SimulationRequest) -> None:
    for label, path in (
        ("model", request.model_path),
        ("weather", request.weather_path),
    ):
        if not path.is_file():
            raise EnergyPlusIntegrationError(f"EnergyPlus {label} file does not exist: {path}")
    if request.mode is not SimulationMode.BASELINE and (
        request.actuator_map_path is None or not request.actuator_map_path.is_file()
    ):
        raise EnergyPlusIntegrationError(
            f"{request.mode.value} mode requires a generated actuator map."
        )


def _execute_simulation(request: SimulationRequest) -> SimulationResult:
    """Execute one simulation inside the already-isolated child process."""

    started = time.monotonic()
    _validate_request_files(request)
    request.output_directory.mkdir(parents=True, exist_ok=True)
    installation = _installation_for_request(request)
    api = _load_api(installation)
    store = _load_store(request.store_adapter, request.database_path, request.run_id)
    specs = list(default_observation_specs())
    if request.mode is not SimulationMode.BASELINE:
        specs.extend(actuator_specs_from_map(cast(Path, request.actuator_map_path)))
    registry = HandleRegistry(tuple(specs))
    state = api.state_manager.new_state()
    session = EnergyPlusSession(request, api, store, registry)
    exit_code: int | None = None
    try:
        store.create_run(
            request.run_id,
            {
                "run_id": request.run_id,
                "status": "running",
                "mode": request.mode.value,
                "model_path": str(request.model_path),
                "weather_path": str(request.weather_path),
                "output_directory": str(request.output_directory),
                "energyplus_version": installation.version,
                "started_at": datetime.now().astimezone().isoformat(),
            },
        )
        registry.request_variables(api.exchange, state)
        session.register_callbacks(state)
        arguments = [
            "-w",
            str(request.weather_path),
            "-d",
            str(request.output_directory),
            "-r",
            str(request.model_path),
        ]
        exit_code = int(api.runtime.run_energyplus(state, arguments))
        err_path = request.output_directory / "eplusout.err"
        messages = parse_error_file(err_path) if err_path.is_file() else ()
        counts = severity_counts(messages)
        for message in messages:
            if message.severity in {MessageSeverity.SEVERE, MessageSeverity.FATAL}:
                store.record_error(
                    request.run_id,
                    datetime.now().astimezone().isoformat(),
                    message.severity.value,
                    message.digest,
                    message.message,
                )
        fatal_count = counts[MessageSeverity.FATAL.value]
        error = session.callback_error
        status = "completed" if exit_code == 0 and fatal_count == 0 and error is None else "failed"
        if status == "failed" and error is None:
            error = f"EnergyPlus exited {exit_code} with {fatal_count} fatal diagnostics"
        store.update_run(
            request.run_id,
            {
                "status": status,
                "exit_code": exit_code,
                "progress_percent": session.progress,
                "completed_at": datetime.now().astimezone().isoformat(),
                "error": error,
            },
        )
        for artifact_type, name in (
            ("energyplus_error_file", "eplusout.err"),
            ("energyplus_sqlite", "eplusout.sql"),
            ("energyplus_csv", "eplusout.csv"),
        ):
            path = request.output_directory / name
            if path.is_file():
                store.record_artifact(
                    request.run_id,
                    datetime.now().astimezone().isoformat(),
                    artifact_type,
                    str(path),
                )
        return SimulationResult(
            run_id=request.run_id,
            status=status,
            exit_code=exit_code,
            output_directory=request.output_directory,
            elapsed_seconds=time.monotonic() - started,
            progress_percent=session.progress,
            warning_count=counts[MessageSeverity.WARNING.value],
            severe_count=counts[MessageSeverity.SEVERE.value],
            fatal_count=fatal_count,
            observation_count=session.observation_count,
            applied_action_count=session.applied_action_count,
            error=error,
        )
    finally:
        api.state_manager.delete_state(state)
        store.close()


def _child_entry(
    request: SimulationRequest,
    connection: Connection,
) -> None:
    """Process boundary that converts unexpected failures into a serializable result."""

    started = time.monotonic()
    try:
        result = _execute_simulation(request)
    except Exception as exc:  # deliberate child-process boundary
        failure = f"{type(exc).__name__}: {exc}"
        _mark_run_failed(request, failure)
        result = SimulationResult(
            run_id=request.run_id,
            status="failed",
            exit_code=None,
            output_directory=request.output_directory,
            elapsed_seconds=time.monotonic() - started,
            progress_percent=0,
            warning_count=0,
            severe_count=0,
            fatal_count=0,
            observation_count=0,
            applied_action_count=0,
            error=failure,
        )
    connection.send(result)
    connection.close()


def _mark_run_failed(request: SimulationRequest, error: str) -> None:
    """Best-effort terminal update for failures outside the active session."""

    try:
        store = _load_store(request.store_adapter, request.database_path, request.run_id)
        store.update_run(
            request.run_id,
            {
                "status": "failed",
                "error": error,
                "completed_at": datetime.now().astimezone().isoformat(),
            },
        )
        store.close()
    except (
        EnergyPlusIntegrationError,
        ImportError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        # There may be no database or run row yet. The serializable process
        # result remains the authoritative failure signal in that case.
        return


def run_simulation(request: SimulationRequest) -> SimulationResult:
    """Run EnergyPlus in a fresh spawned process and return its audited result."""

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_entry,
        args=(request, child_connection),
        name=f"ecoloop-energyplus-{request.run_id}",
    )
    process.start()
    child_connection.close()
    if not parent_connection.poll(request.process_timeout_seconds):
        process.terminate()
        process.join(timeout=10)
        parent_connection.close()
        timeout_error = (
            f"EnergyPlus process exceeded {request.process_timeout_seconds:.1f} seconds "
            "and was terminated."
        )
        _mark_run_failed(request, timeout_error)
        return SimulationResult(
            run_id=request.run_id,
            status="failed",
            exit_code=process.exitcode,
            output_directory=request.output_directory,
            elapsed_seconds=request.process_timeout_seconds,
            progress_percent=0,
            warning_count=0,
            severe_count=0,
            fatal_count=0,
            observation_count=0,
            applied_action_count=0,
            error=timeout_error,
        )
    result = cast(SimulationResult, parent_connection.recv())
    parent_connection.close()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=10)
    return result
