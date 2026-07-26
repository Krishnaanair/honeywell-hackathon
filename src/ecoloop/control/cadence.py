"""Normal and event-triggered supervisory decision cadence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ecoloop.schemas import BuildingObservation, ControlConstraints, SetpointLimits
from ecoloop.time_utils import as_utc, elapsed_minutes


class DecisionTrigger(StrEnum):
    """Reasons that can independently request a new decision."""

    FIRST_OBSERVATION = "first_observation"
    NORMAL_INTERVAL = "normal_interval"
    OPERATIVE_LIMIT = "operative_limit"
    PMV_LIMIT = "pmv_limit"
    CO2_LIMIT = "co2_limit"
    DEMAND_LIMIT = "demand_limit"
    OCCUPANCY_CHANGE = "occupancy_change"
    OUTDOOR_CHANGE = "outdoor_change"
    ACTION_EXPIRY = "action_expiry"


@dataclass(frozen=True, slots=True)
class CadenceDecision:
    """Decision cadence result with every active trigger."""

    should_decide: bool
    triggers: tuple[DecisionTrigger, ...]


@dataclass(frozen=True, slots=True)
class DecisionCadence:
    """Deterministic normal and event-trigger configuration."""

    normal_interval_minutes: int = 60
    operative_approach_margin_c: float = 0.5
    pmv_approach_margin: float = 0.1
    co2_approach_margin_ppm: float = 100.0
    outdoor_sharp_change_c: float = 3.0

    def __post_init__(self) -> None:
        if self.normal_interval_minutes <= 0:
            raise ValueError("normal_interval_minutes must be positive")
        if (
            min(
                self.operative_approach_margin_c,
                self.pmv_approach_margin,
                self.co2_approach_margin_ppm,
                self.outdoor_sharp_change_c,
            )
            < 0
        ):
            raise ValueError("event trigger margins must be non-negative")

    def evaluate(
        self,
        observation: BuildingObservation,
        constraints: ControlConstraints,
        *,
        previous_observation: BuildingObservation | None,
        last_decision_simulation_timestamp: datetime | None,
        action_expires_at: datetime | None,
    ) -> CadenceDecision:
        """Evaluate all cadence triggers against simulated, not wall-clock, time."""

        if observation.run_id != constraints.run_id:
            raise ValueError("observation and constraints must belong to the same run")
        if previous_observation is not None and previous_observation.run_id != observation.run_id:
            raise ValueError("previous observation belongs to a different run")
        triggers: list[DecisionTrigger] = []
        if last_decision_simulation_timestamp is None:
            triggers.append(DecisionTrigger.FIRST_OBSERVATION)
        elif (
            elapsed_minutes(
                last_decision_simulation_timestamp,
                observation.simulation_timestamp,
            )
            >= self.normal_interval_minutes
        ):
            triggers.append(DecisionTrigger.NORMAL_INTERVAL)

        limits = constraints.active_limits
        if self._operative_risk(observation, limits) and not (
            previous_observation is not None and self._operative_risk(previous_observation, limits)
        ):
            triggers.append(DecisionTrigger.OPERATIVE_LIMIT)
        if self._pmv_risk(observation, limits) and not (
            previous_observation is not None and self._pmv_risk(previous_observation, limits)
        ):
            triggers.append(DecisionTrigger.PMV_LIMIT)
        if self._co2_risk(observation, limits) and not (
            previous_observation is not None and self._co2_risk(previous_observation, limits)
        ):
            triggers.append(DecisionTrigger.CO2_LIMIT)
        demand_risk = (
            observation.facility_demand_kw is not None
            and observation.facility_demand_kw >= constraints.demand_threshold_kw
        )
        previous_demand_risk = (
            previous_observation is not None
            and previous_observation.facility_demand_kw is not None
            and previous_observation.facility_demand_kw >= constraints.demand_threshold_kw
        )
        if demand_risk and not previous_demand_risk:
            triggers.append(DecisionTrigger.DEMAND_LIMIT)
        if previous_observation is not None:
            if previous_observation.occupied != observation.occupied:
                triggers.append(DecisionTrigger.OCCUPANCY_CHANGE)
            if (
                previous_observation.outdoor_temperature_c is not None
                and observation.outdoor_temperature_c is not None
                and abs(
                    observation.outdoor_temperature_c - previous_observation.outdoor_temperature_c
                )
                >= self.outdoor_sharp_change_c
            ):
                triggers.append(DecisionTrigger.OUTDOOR_CHANGE)
        if action_expires_at is not None and as_utc(action_expires_at) <= as_utc(
            observation.simulation_timestamp
        ):
            triggers.append(DecisionTrigger.ACTION_EXPIRY)
        unique = tuple(dict.fromkeys(triggers))
        return CadenceDecision(should_decide=bool(unique), triggers=unique)

    def _operative_risk(
        self,
        observation: BuildingObservation,
        limits: SetpointLimits,
    ) -> bool:
        operative_min = (
            observation.operative_temperature_min_c
            if observation.operative_temperature_min_c is not None
            else observation.zone_temperature_min_c
        )
        operative_max = (
            observation.operative_temperature_max_c
            if observation.operative_temperature_max_c is not None
            else observation.zone_temperature_max_c
        )
        return bool(
            (
                limits.operative_min_c is not None
                and operative_min <= limits.operative_min_c + self.operative_approach_margin_c
            )
            or (
                limits.operative_max_c is not None
                and operative_max >= limits.operative_max_c - self.operative_approach_margin_c
            )
        )

    def _pmv_risk(
        self,
        observation: BuildingObservation,
        limits: SetpointLimits,
    ) -> bool:
        return bool(
            limits.absolute_pmv_max is not None
            and observation.pmv_max_abs is not None
            and observation.pmv_max_abs
            >= max(0.0, limits.absolute_pmv_max - self.pmv_approach_margin)
        )

    def _co2_risk(
        self,
        observation: BuildingObservation,
        limits: SetpointLimits,
    ) -> bool:
        return bool(
            limits.co2_max_ppm is not None
            and observation.co2_max_ppm is not None
            and observation.co2_max_ppm
            >= max(0.0, limits.co2_max_ppm - self.co2_approach_margin_ppm)
        )
