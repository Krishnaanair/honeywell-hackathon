"""Deterministic domain factories used only by unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ecoloop.schemas import (
    ActuatorCapabilities,
    BuildingObservation,
    CandidateAction,
    ControlAction,
    ControlConstraints,
    ObservationInput,
    ReasonCode,
    SetpointLimits,
)

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def observation_input(**updates: Any) -> ObservationInput:
    values: dict[str, Any] = {
        "run_id": "run-agent-1",
        "timestamp": NOW,
        "simulation_timestamp": NOW,
        "timestep_key": "ts-1",
        "environment": "RUN PERIOD 1",
        "occupied": True,
        "occupancy_count": 12.0,
        "zone_temperature_mean_c": 24.0,
        "zone_temperature_min_c": 23.0,
        "zone_temperature_max_c": 25.0,
        "operative_temperature_mean_c": 24.0,
        "operative_temperature_min_c": 23.0,
        "operative_temperature_max_c": 25.0,
        "relative_humidity_mean_percent": 50.0,
        "relative_humidity_max_percent": 55.0,
        "pmv_mean": 0.1,
        "pmv_max_abs": 0.2,
        "ppd_mean_percent": 6.0,
        "ppd_max_percent": 8.0,
        "co2_mean_ppm": 650.0,
        "co2_max_ppm": 700.0,
        "outdoor_temperature_c": 32.0,
        "forecast_temperature_mean_c": 33.0,
        "forecast_temperature_max_c": 35.0,
        "forecast_solar_mean_w_m2": 500.0,
        "heating_setpoint_c": 20.0,
        "cooling_setpoint_c": 24.0,
        "facility_demand_kw": 50.0,
        "timestep_electricity_kwh": 12.5,
        "cumulative_electricity_kwh": 250.0,
        "hvac_electricity_kwh": 7.5,
        "tariff_per_kwh": 0.12,
        "carbon_kg_per_kwh": 0.7,
        "actuator_capabilities": ActuatorCapabilities(
            heating_setpoint=True,
            cooling_setpoint=True,
        ),
    }
    values.update(updates)
    return ObservationInput.model_validate(values)


def observation(**updates: Any) -> BuildingObservation:
    observation_id = int(updates.pop("observation_id", 1))
    return BuildingObservation(
        observation_id=observation_id,
        **observation_input(**updates).model_dump(),
    )


def constraints(**updates: Any) -> ControlConstraints:
    occupied_limits = SetpointLimits(
        heating_min_c=19.0,
        heating_max_c=22.0,
        cooling_min_c=23.0,
        cooling_max_c=26.0,
        minimum_deadband_c=2.0,
        maximum_change_c=1.0,
        operative_min_c=22.0,
        operative_max_c=26.0,
        absolute_pmv_max=0.7,
        co2_max_ppm=1000.0,
    )
    unoccupied_limits = SetpointLimits(
        heating_min_c=17.0,
        heating_max_c=21.0,
        cooling_min_c=24.0,
        cooling_max_c=29.0,
        minimum_deadband_c=2.0,
        maximum_change_c=2.0,
    )
    values: dict[str, Any] = {
        "run_id": "run-agent-1",
        "timestamp": NOW,
        "occupied": True,
        "occupied_limits": occupied_limits,
        "unoccupied_limits": unoccupied_limits,
        "capabilities": ActuatorCapabilities(
            heating_setpoint=True,
            cooling_setpoint=True,
        ),
        "maximum_hold_minutes": 120,
        "observation_max_age_minutes": 30,
        "demand_threshold_kw": 75.0,
        "freeze_protection_temperature_c": 8.0,
        "overheat_protection_temperature_c": 35.0,
        "next_action_generation": 1,
    }
    values.update(updates)
    return ControlConstraints.model_validate(values)


def candidate(**updates: Any) -> CandidateAction:
    values: dict[str, Any] = {
        "candidate_id": "candidate-test",
        "heating_setpoint_c": 20.0,
        "cooling_setpoint_c": 24.0,
        "hold_minutes": 60,
    }
    values.update(updates)
    return CandidateAction.model_validate(values)


def action(**updates: Any) -> ControlAction:
    values: dict[str, Any] = {
        "action_id": "action-test-1",
        "run_id": "run-agent-1",
        "observation_id": 1,
        "action_generation": 1,
        "timestamp": NOW,
        "expires_at": NOW + timedelta(minutes=60),
        "action": candidate(),
        "model": "qwen3:8b",
        "latency_ms": 125.0,
        "reason_code": ReasonCode.ENERGY_OPTIMIZATION,
        "explanation": "Selected the lowest safe evaluated candidate.",
    }
    values.update(updates)
    return ControlAction.model_validate(values)
