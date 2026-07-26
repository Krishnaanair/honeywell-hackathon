"""Explicit adapter between EnergyPlus callbacks and the typed SQLite store."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecoloop.config import Settings
from ecoloop.db.store import DataConflictError, SQLiteStore
from ecoloop.schemas import (
    ActuatorCapabilities,
    BuildingTelemetry,
    MessageSeverity,
    ObservationInput,
    RunStatus,
    RunType,
    ZoneTelemetry,
)
from ecoloop.time_utils import utc_now


def _simulation_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _optional_number(values: Mapping[str, Any], name: str, *, divisor: float = 1.0) -> float | None:
    raw = values.get(name)
    if raw is None:
        return None
    value = float(raw) / divisor
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _aggregate_value(
    values: Mapping[str, Any],
    aggregate_name: str,
    component: str,
    *,
    required: bool = False,
) -> float | None:
    aggregate = values.get(aggregate_name)
    if not isinstance(aggregate, Mapping):
        if required:
            raise ValueError(f"observation is missing {aggregate_name}")
        return None
    value = aggregate.get(component)
    if value is None:
        if required:
            raise ValueError(f"observation is missing {aggregate_name}.{component}")
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{aggregate_name}.{component} must be finite")
    return parsed


def _required_aggregate(
    values: Mapping[str, Any],
    aggregate_name: str,
    component: str,
) -> float:
    result = _aggregate_value(
        values,
        aggregate_name,
        component,
        required=True,
    )
    if result is None:  # pragma: no cover - guarded by required=True
        raise ValueError(f"observation is missing {aggregate_name}.{component}")
    return result


def _run_type(value: object) -> RunType:
    return RunType(str(value))


class SQLiteRuntimeStoreAdapter:
    """Map the narrow runtime protocol onto the domain-typed SQLite store."""

    def __init__(self, database_path: Path, run_id: str) -> None:
        self.store = SQLiteStore(database_path)
        self.run_id = run_id
        self._settings = Settings()
        self._controlled = False
        self._electricity_signal: str | None = None

    def create_run(self, run_id: str, values: Mapping[str, Any]) -> None:
        """Create or start a real run without overwriting existing metadata."""

        if run_id != self.run_id:
            raise ValueError("adapter run_id mismatch")
        run_type = _run_type(values["mode"])
        self._controlled = run_type is not RunType.BASELINE
        existing = self.store.get_run(run_id)
        if existing is None:
            try:
                self.store.create_run(
                    run_id,
                    run_type,
                    is_fake=False,
                    energyplus_version=str(values.get("energyplus_version") or ""),
                    model_path=Path(str(values["model_path"])),
                    weather_path=Path(str(values["weather_path"])),
                    metadata={
                        "output_directory": str(values["output_directory"]),
                        "runtime_integration": "External PyEnergyPlus Runtime API",
                    },
                )
            except DataConflictError:
                existing = self.store.get_run(run_id)
                if existing is None:
                    raise
        current = self.store.get_run(run_id)
        if current is not None and current.status is RunStatus.PENDING:
            self.store.set_run_status(run_id, RunStatus.RUNNING)

    def update_run(self, run_id: str, values: Mapping[str, Any]) -> None:
        """Map progress and terminal status updates."""

        progress = values.get("progress_percent")
        if progress is not None:
            current = self.store.get_run(run_id)
            if current is not None and current.status is RunStatus.RUNNING:
                self.store.update_progress(run_id, float(progress))
        status_value = values.get("status")
        if status_value not in {"completed", "failed"}:
            return
        current = self.store.get_run(run_id)
        if current is None or current.status is not RunStatus.RUNNING:
            return
        self.store.set_run_status(
            run_id,
            RunStatus(str(status_value)),
            error_summary=(str(values["error"]) if values.get("error") is not None else None),
        )

    def insert_telemetry(
        self,
        run_id: str,
        timestamp: str,
        timestep_identity: str,
        values: Mapping[str, Any],
    ) -> None:
        """Persist facility telemetry with explicit unit conversion."""

        simulation_time = _simulation_datetime(timestamp)
        heating = _aggregate_value(values, "heating_setpoint", "mean")
        cooling = _aggregate_value(values, "cooling_setpoint", "mean")
        electricity_signal = str(values.get("runtime_electricity_signal", ""))
        if electricity_signal and electricity_signal != self._electricity_signal:
            self._electricity_signal = electricity_signal
            self.store.record_simulation_message(
                run_id,
                MessageSeverity.INFORMATION,
                f"Runtime callback electricity signal: {electricity_signal}; official "
                "final total remains Electricity:Facility from EnergyPlus output.",
            )
        self.store.record_telemetry(
            BuildingTelemetry(
                run_id=run_id,
                timestamp=utc_now(),
                simulation_timestamp=simulation_time,
                timestep_key=timestep_identity,
                environment=str(values["environment"]),
                outdoor_temperature_c=_optional_number(values, "outdoor_drybulb_c"),
                facility_demand_kw=_optional_number(
                    values,
                    "facility_demand_w",
                    divisor=1_000.0,
                ),
                timestep_electricity_kwh=_optional_number(
                    values,
                    "timestep_energy_kwh",
                ),
                cumulative_electricity_kwh=_optional_number(
                    values,
                    "cumulative_energy_kwh",
                ),
                hvac_electricity_kwh=_optional_number(
                    values,
                    "timestep_hvac_energy_kwh",
                ),
                heating_setpoint_c=heating,
                cooling_setpoint_c=cooling,
            )
        )

    def insert_zone_telemetry(
        self,
        run_id: str,
        timestamp: str,
        timestep_identity: str,
        zone_name: str,
        values: Mapping[str, Any],
    ) -> None:
        """Persist one zone row; absent optional points remain ``None``."""

        temperature = _optional_number(values, "zone_air_temperature_c")
        if temperature is None:
            raise ValueError(f"required zone temperature is missing for {zone_name}")
        self.store.record_zone_telemetry(
            ZoneTelemetry(
                run_id=run_id,
                timestamp=utc_now(),
                simulation_timestamp=_simulation_datetime(timestamp),
                timestep_key=timestep_identity,
                environment=str(values.get("environment", "weather_run_period")),
                zone_name=zone_name,
                mean_air_temperature_c=temperature,
                operative_temperature_c=_optional_number(
                    values,
                    "zone_operative_temperature_c",
                ),
                relative_humidity_percent=_optional_number(
                    values,
                    "zone_relative_humidity_pct",
                ),
                occupant_count=_optional_number(values, "zone_occupant_count"),
                pmv=_optional_number(values, "zone_pmv"),
                ppd_percent=_optional_number(values, "zone_ppd"),
                co2_ppm=_optional_number(values, "zone_co2_ppm"),
                heating_setpoint_c=_optional_number(values, "heating_setpoint_c"),
                cooling_setpoint_c=_optional_number(values, "cooling_setpoint_c"),
            )
        )

    def insert_observation(
        self,
        run_id: str,
        timestamp: str,
        timestep_identity: str,
        values: Mapping[str, Any],
    ) -> int:
        """Build the typed aggregate observation and return its monotonic ID."""

        occupancy_raw = values.get("occupancy_count")
        if occupancy_raw is None:
            raise ValueError("observation is missing the model occupancy signal")
        heating = _required_aggregate(values, "heating_setpoint", "mean")
        cooling = _required_aggregate(values, "cooling_setpoint", "mean")
        pmv_min = _aggregate_value(values, "pmv", "min")
        pmv_max = _aggregate_value(values, "pmv", "max")
        pmv_max_abs = (
            max(abs(pmv_min), abs(pmv_max)) if pmv_min is not None and pmv_max is not None else None
        )
        observation = ObservationInput(
            run_id=run_id,
            timestamp=utc_now(),
            simulation_timestamp=_simulation_datetime(timestamp),
            timestep_key=timestep_identity,
            environment=str(values["environment"]),
            occupied=bool(values.get("occupied", False)),
            occupancy_count=float(occupancy_raw),
            zone_temperature_mean_c=_required_aggregate(
                values,
                "zone_temperature",
                "mean",
            ),
            zone_temperature_min_c=_required_aggregate(
                values,
                "zone_temperature",
                "min",
            ),
            zone_temperature_max_c=_required_aggregate(
                values,
                "zone_temperature",
                "max",
            ),
            operative_temperature_mean_c=_aggregate_value(
                values,
                "operative_temperature",
                "mean",
            ),
            operative_temperature_min_c=_aggregate_value(
                values,
                "operative_temperature",
                "min",
            ),
            operative_temperature_max_c=_aggregate_value(
                values,
                "operative_temperature",
                "max",
            ),
            relative_humidity_mean_percent=_aggregate_value(
                values,
                "relative_humidity",
                "mean",
            ),
            relative_humidity_max_percent=_aggregate_value(
                values,
                "relative_humidity",
                "max",
            ),
            pmv_mean=_aggregate_value(values, "pmv", "mean"),
            pmv_max_abs=pmv_max_abs,
            ppd_mean_percent=_aggregate_value(values, "ppd", "mean"),
            ppd_max_percent=_aggregate_value(values, "ppd", "max"),
            co2_mean_ppm=_aggregate_value(values, "co2", "mean"),
            co2_max_ppm=_aggregate_value(values, "co2", "max"),
            outdoor_temperature_c=_optional_number(values, "outdoor_drybulb_c"),
            heating_setpoint_c=heating,
            cooling_setpoint_c=cooling,
            facility_demand_kw=_optional_number(
                values,
                "facility_demand_w",
                divisor=1_000.0,
            ),
            timestep_electricity_kwh=_optional_number(values, "timestep_energy_kwh"),
            cumulative_electricity_kwh=_optional_number(
                values,
                "cumulative_energy_kwh",
            ),
            hvac_electricity_kwh=_optional_number(
                values,
                "timestep_hvac_energy_kwh",
            ),
            tariff_per_kwh=self._settings.tariff_per_kwh,
            carbon_kg_per_kwh=self._settings.carbon_kg_per_kwh,
            actuator_capabilities=ActuatorCapabilities(
                heating_setpoint=self._controlled,
                cooling_setpoint=self._controlled,
            ),
            zones=self.store.get_zone_telemetry(run_id, timestep_identity),
        )
        return self.store.record_observation(observation).observation_id

    def latest_applicable_action(
        self,
        run_id: str,
        observation_id: int,
    ) -> Mapping[str, Any] | None:
        """Return the latest accepted action; runtime performs final freshness checks."""

        pair = self.store.get_last_applied_action(run_id)
        if pair is None:
            return None
        action, validation = pair
        if not validation.accepted or validation.applied_action is None:
            return None
        if action.observation_id != observation_id:
            return None
        applied = validation.applied_action
        return {
            "run_id": action.run_id,
            "observation_id": action.observation_id,
            "action_generation": action.action_generation,
            "applied_heating_setpoint_c": applied.heating_setpoint_c,
            "applied_cooling_setpoint_c": applied.cooling_setpoint_c,
            "expires_at": action.expires_at.isoformat(),
            "simulation_expires_at": None,
            "hold_minutes": applied.hold_minutes,
            "validation_result": "clamped" if validation.clamps else "valid",
            "fallback_status": bool(validation.fallback_status),
            "source": "sqlite_validated_action",
            "requested_observation_id": observation_id,
        }

    def record_applied_action(
        self,
        run_id: str,
        timestamp: str,
        values: Mapping[str, Any],
    ) -> None:
        """Audit physical actuator application without duplicating action rows."""

        message = (
            f"Applied thermostat actuators generation={values['action_generation']} "
            f"observation={values['observation_id']} "
            f"heating={float(values['applied_heating_setpoint_c']):.3f}C "
            f"cooling={float(values['applied_cooling_setpoint_c']):.3f}C "
            f"source={values['source']} simulation_timestamp={timestamp}"
        )
        self.store.record_simulation_message(
            run_id,
            MessageSeverity.INFORMATION,
            message,
            timestamp=utc_now(),
        )

    def record_simulation_message(
        self,
        run_id: str,
        timestamp: str,
        severity: str,
        digest: str,
        message: str,
    ) -> None:
        """Delegate de-duplication to the canonical SQLite store."""

        del digest
        self.store.record_simulation_message(
            run_id,
            MessageSeverity(severity),
            message,
            timestamp=_simulation_datetime(timestamp),
        )

    def record_error(
        self,
        run_id: str,
        timestamp: str,
        severity: str,
        digest: str,
        message: str,
    ) -> None:
        """Persist an EnergyPlus/runtime error with the canonical source tag."""

        self.store.record_error(
            run_id,
            MessageSeverity(severity),
            "energyplus_runtime",
            message,
            details={"runtime_digest": digest, "runtime_timestamp": timestamp},
            timestamp=utc_now(),
        )

    def record_artifact(
        self,
        run_id: str,
        timestamp: str,
        artifact_type: str,
        path: str,
    ) -> None:
        """Persist a run artifact path restricted by the runtime request."""

        self.store.record_artifact(
            run_id,
            artifact_type,
            Path(path),
            timestamp=_simulation_datetime(timestamp),
        )

    def record_metric(
        self,
        run_id: str,
        timestamp: str,
        name: str,
        value: float,
        units: str,
    ) -> None:
        """Persist callback metrics as unverified until official-output cross-check."""

        self.store.upsert_metric(
            run_id,
            name,
            value=value,
            units=units,
            source="energyplus_runtime_api",
            verified=False,
            timestamp=_simulation_datetime(timestamp),
        )

    def close(self) -> None:
        """No-op: ``SQLiteStore`` opens a scoped connection for each operation."""


def open_runtime_store(database_path: Path, run_id: str) -> SQLiteRuntimeStoreAdapter:
    """Factory loaded by the spawned EnergyPlus worker."""

    return SQLiteRuntimeStoreAdapter(database_path, run_id)
