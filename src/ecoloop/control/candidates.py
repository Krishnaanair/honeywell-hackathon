"""Bounded candidate generation and transparent one-step heuristic scoring."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable

from ecoloop.control.safety import ramp_envelope
from ecoloop.schemas import (
    BuildingObservation,
    CandidateAction,
    CandidateScore,
    ControlConstraints,
    ScoreComponents,
)


def generate_candidate_actions(
    observation: BuildingObservation,
    constraints: ControlConstraints,
    *,
    baseline_heating_setpoint_c: float | None = None,
    baseline_cooling_setpoint_c: float | None = None,
    hold_minutes: int = 60,
    grid_step_c: float = 1.0,
    max_candidates: int = 25,
) -> list[CandidateAction]:
    """Generate a deterministic, feasible grid around current/reference setpoints."""

    if observation.run_id != constraints.run_id:
        raise ValueError("observation and constraints must belong to the same run")
    if constraints.occupied != observation.occupied:
        raise ValueError("constraint occupancy mode does not match the observation")
    if not (
        constraints.capabilities.heating_setpoint and constraints.capabilities.cooling_setpoint
    ):
        raise ValueError("mandatory thermostat actuator capabilities are unavailable")
    if grid_step_c <= 0 or not math.isfinite(grid_step_c):
        raise ValueError("grid_step_c must be finite and positive")
    if not 1 <= max_candidates <= 256:
        raise ValueError("max_candidates must be in 1..256")

    limits = constraints.active_limits
    current_heating = float(observation.heating_setpoint_c)
    current_cooling = float(observation.cooling_setpoint_c)
    heating_low, heating_high, _ = ramp_envelope(
        current_heating,
        lower=float(limits.heating_min_c),
        upper=float(limits.heating_max_c),
        maximum_change=float(limits.maximum_change_c),
    )
    cooling_low, cooling_high, _ = ramp_envelope(
        current_cooling,
        lower=float(limits.cooling_min_c),
        upper=float(limits.cooling_max_c),
        maximum_change=float(limits.maximum_change_c),
    )

    recommended_heating = (
        _clamp(20.0, heating_low, heating_high) if observation.occupied else heating_low
    )
    recommended_cooling = (
        _clamp(25.0, cooling_low, cooling_high) if observation.occupied else cooling_high
    )
    heating_values = _bounded_values(
        {
            current_heating,
            current_heating - grid_step_c,
            current_heating + grid_step_c,
            recommended_heating,
            baseline_heating_setpoint_c,
            heating_low,
            heating_high,
        },
        heating_low,
        heating_high,
    )
    cooling_values = _bounded_values(
        {
            current_cooling,
            current_cooling - grid_step_c,
            current_cooling + grid_step_c,
            recommended_cooling,
            baseline_cooling_setpoint_c,
            cooling_low,
            cooling_high,
        },
        cooling_low,
        cooling_high,
    )

    duration = min(hold_minutes, constraints.maximum_hold_minutes)
    if duration <= 0:
        raise ValueError("hold_minutes must be positive")
    pairs = [
        (heating, cooling)
        for heating in heating_values
        for cooling in cooling_values
        if cooling - heating >= limits.minimum_deadband_c - 1e-9
    ]
    pairs.sort(
        key=lambda pair: (
            abs(pair[0] - current_heating) + abs(pair[1] - current_cooling),
            pair[0],
            pair[1],
        )
    )
    candidates = [
        CandidateAction(
            candidate_id=_candidate_id(heating, cooling, duration),
            heating_setpoint_c=heating,
            cooling_setpoint_c=cooling,
            hold_minutes=duration,
        )
        for heating, cooling in pairs[:max_candidates]
    ]
    if not candidates:
        raise ValueError("no candidate satisfies setpoint limits and minimum deadband")
    return candidates


def score_candidate(
    observation: BuildingObservation,
    constraints: ControlConstraints,
    candidate: CandidateAction,
    *,
    action_change_penalty_per_c: float = 0.15,
) -> CandidateScore:
    """Score one candidate with an explicit one-step engineering heuristic.

    This is not a predictive-horizon optimizer. It uses recent slope, a small
    outdoor coupling term, current demand, and configurable comfort/grid signals
    to rank an already bounded candidate set.
    """

    if observation.run_id != constraints.run_id:
        raise ValueError("observation and constraints must belong to the same run")
    if not math.isfinite(action_change_penalty_per_c) or action_change_penalty_per_c < 0:
        raise ValueError("action_change_penalty_per_c must be finite and non-negative")
    capabilities = constraints.capabilities
    unsupported_optional = (
        (candidate.ventilation_multiplier is not None and not capabilities.ventilation_multiplier)
        or (candidate.lighting_fraction is not None and not capabilities.lighting_fraction)
        or (
            candidate.supply_air_temperature_c is not None
            and not capabilities.supply_air_temperature
        )
        or (candidate.shading_state is not None and not capabilities.shading_state)
    )
    if not (capabilities.heating_setpoint and capabilities.cooling_setpoint):
        raise ValueError("mandatory thermostat actuator capabilities are unavailable")
    if unsupported_optional:
        raise ValueError("candidate requests an unsupported optional actuator")
    limits = constraints.active_limits
    if (
        candidate.heating_setpoint_c < limits.heating_min_c
        or candidate.heating_setpoint_c > limits.heating_max_c
        or candidate.cooling_setpoint_c < limits.cooling_min_c
        or candidate.cooling_setpoint_c > limits.cooling_max_c
        or candidate.cooling_setpoint_c - candidate.heating_setpoint_c < limits.minimum_deadband_c
    ):
        raise ValueError("candidate is outside active deterministic constraints")

    current_operative = float(
        observation.operative_temperature_mean_c
        if observation.operative_temperature_mean_c is not None
        else observation.zone_temperature_mean_c
    )
    temperature_trend = observation.recent_trends.get(
        "operative_temperature_mean_c"
    ) or observation.recent_trends.get("zone_temperature_mean_c")
    slope_per_hour = float(temperature_trend.slope_per_hour) if temperature_trend else 0.0
    hold_hours = candidate.hold_minutes / 60.0
    outdoor = (
        float(observation.outdoor_temperature_c)
        if observation.outdoor_temperature_c is not None
        else current_operative
    )
    free_response = (
        current_operative
        + slope_per_hour * hold_hours
        + 0.08 * (outdoor - current_operative) * hold_hours
    )
    predicted_operative = free_response
    if free_response < candidate.heating_setpoint_c:
        predicted_operative += 0.55 * (candidate.heating_setpoint_c - free_response)
    elif free_response > candidate.cooling_setpoint_c:
        predicted_operative -= 0.55 * (free_response - candidate.cooling_setpoint_c)

    comfort_low = (
        float(limits.operative_min_c)
        if limits.operative_min_c is not None
        else float(candidate.heating_setpoint_c)
    )
    comfort_high = (
        float(limits.operative_max_c)
        if limits.operative_max_c is not None
        else float(candidate.cooling_setpoint_c)
    )
    temperature_violation = max(
        0.0,
        comfort_low - predicted_operative,
        predicted_operative - comfort_high,
    )
    comfort_weight = 35.0 if observation.occupied else 5.0
    comfort_penalty = temperature_violation**2 * comfort_weight

    pmv_penalty = 0.0
    if observation.pmv_max_abs is not None and limits.absolute_pmv_max is not None:
        pmv_excess = max(
            0.0,
            float(observation.pmv_max_abs) - float(limits.absolute_pmv_max),
        )
        pmv_penalty = pmv_excess**2 * 40.0

    current_demand = (
        float(observation.facility_demand_kw)
        if observation.facility_demand_kw is not None
        else None
    )
    predicted_demand: float | None = None
    demand_penalty = 0.0
    energy_penalty = 0.0
    tariff_penalty = 0.0
    carbon_penalty = 0.0
    if current_demand is not None:
        heating_relaxation = max(
            0.0,
            float(observation.heating_setpoint_c) - float(candidate.heating_setpoint_c),
        )
        cooling_relaxation = max(
            0.0,
            float(candidate.cooling_setpoint_c) - float(observation.cooling_setpoint_c),
        )
        tightening = max(
            0.0,
            float(candidate.heating_setpoint_c) - float(observation.heating_setpoint_c),
        ) + max(
            0.0,
            float(observation.cooling_setpoint_c) - float(candidate.cooling_setpoint_c),
        )
        predicted_demand = max(
            0.0,
            current_demand
            - 1.0 * heating_relaxation
            - 1.5 * cooling_relaxation
            + 1.25 * tightening,
        )
        demand_excess = max(
            0.0,
            predicted_demand - float(constraints.demand_threshold_kw),
        )
        demand_penalty = demand_excess**2 * 0.15
        estimated_interval_kwh = predicted_demand * hold_hours
        # Energy-priority weighting: within the deterministic comfort band the
        # ranking must prefer the lowest-energy compliant candidate, so the
        # per-kWh terms outweigh action-change and forecast margins while any
        # predicted band violation still dominates them all.
        energy_penalty = estimated_interval_kwh * 0.40
        tariff_penalty = estimated_interval_kwh * float(observation.tariff_per_kwh) * 1.0
        carbon_penalty = estimated_interval_kwh * float(observation.carbon_kg_per_kwh) * 0.25

    action_change_penalty = action_change_penalty_per_c * (
        abs(float(candidate.heating_setpoint_c) - float(observation.heating_setpoint_c))
        + abs(float(candidate.cooling_setpoint_c) - float(observation.cooling_setpoint_c))
    )
    forecast_penalty = 0.0
    if observation.forecast_temperature_max_c is not None:
        hot_margin = max(
            0.0,
            float(observation.forecast_temperature_max_c) - float(candidate.cooling_setpoint_c),
        )
        forecast_penalty += hot_margin * 0.2
    if observation.forecast_temperature_mean_c is not None:
        cold_margin = max(
            0.0,
            float(candidate.heating_setpoint_c) - float(observation.forecast_temperature_mean_c),
        )
        forecast_penalty += cold_margin * 0.15

    components = ScoreComponents(
        comfort_penalty=comfort_penalty,
        pmv_penalty=pmv_penalty,
        demand_penalty=demand_penalty,
        energy_penalty=energy_penalty,
        tariff_penalty=tariff_penalty,
        carbon_penalty=carbon_penalty,
        action_change_penalty=action_change_penalty,
        forecast_penalty=forecast_penalty,
    )
    return CandidateScore(
        candidate=candidate,
        components=components,
        total_score=components.total,
        predicted_operative_temperature_c=predicted_operative,
        predicted_demand_kw=predicted_demand,
        assumptions=(
            "one-step linear temperature trend",
            "8% per-hour outdoor coupling",
            "fixed setpoint-to-demand sensitivity",
            f"{action_change_penalty_per_c:g} score units per C action change",
            "lower score is preferred; this is not MPC",
        ),
    )


def evaluate_candidates(
    observation: BuildingObservation,
    constraints: ControlConstraints,
    candidates: Iterable[CandidateAction],
) -> list[CandidateScore]:
    """Score candidates and return a deterministic best-first ordering."""

    scores = [score_candidate(observation, constraints, candidate) for candidate in candidates]
    scores.sort(key=lambda score: (score.total_score, score.candidate.candidate_id))
    return scores


def quantized_state_key(observation: BuildingObservation) -> str:
    """Hash a compact, quantized state for safe decision-cache lookup."""

    payload = {
        "occupied": observation.occupied,
        "occupancy": round(float(observation.occupancy_count)),
        "operative": round(
            float(
                observation.operative_temperature_mean_c
                if observation.operative_temperature_mean_c is not None
                else observation.zone_temperature_mean_c
            )
            * 2
        )
        / 2,
        "outdoor": (
            round(float(observation.outdoor_temperature_c))
            if observation.outdoor_temperature_c is not None
            else None
        ),
        "demand": (
            round(float(observation.facility_demand_kw) / 5.0) * 5.0
            if observation.facility_demand_kw is not None
            else None
        ),
        "pmv": (
            round(float(observation.pmv_mean) * 5.0) / 5.0
            if observation.pmv_mean is not None
            else None
        ),
        "co2": (
            round(float(observation.co2_max_ppm) / 50.0) * 50.0
            if observation.co2_max_ppm is not None
            else None
        ),
        "heating": round(float(observation.heating_setpoint_c) * 2.0) / 2.0,
        "cooling": round(float(observation.cooling_setpoint_c) * 2.0) / 2.0,
        "capabilities": observation.actuator_capabilities.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bounded_values(
    values: set[float | None],
    lower: float,
    upper: float,
) -> list[float]:
    bounded = {
        round(_clamp(float(value), lower, upper), 6)
        for value in values
        if value is not None and math.isfinite(value)
    }
    return sorted(bounded)


def _candidate_id(heating: float, cooling: float, hold_minutes: int) -> str:
    canonical = f"{heating:.6f}|{cooling:.6f}|{hold_minutes}"
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()[:12]
    return f"candidate-{digest}"


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
