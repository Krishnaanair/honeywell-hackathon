"""Conversion from version-controlled configuration to runtime constraints."""

from __future__ import annotations

from datetime import datetime

from ecoloop.config import ControlConfig, LimitConfig
from ecoloop.schemas import (
    ActuatorCapabilities,
    ControlConstraints,
    SetpointLimits,
)


def setpoint_limits_from_config(config: LimitConfig) -> SetpointLimits:
    """Copy validated file configuration into the runtime schema."""

    return SetpointLimits.model_validate(config.model_dump())


def build_control_constraints(
    *,
    run_id: str,
    timestamp: datetime,
    occupied: bool,
    capabilities: ActuatorCapabilities,
    next_action_generation: int,
    config: ControlConfig,
    observation_max_age_minutes: int = 30,
    freeze_protection_temperature_c: float = 8.0,
    overheat_protection_temperature_c: float = 35.0,
) -> ControlConstraints:
    """Build the complete deterministic constraint snapshot for one decision."""

    return ControlConstraints(
        run_id=run_id,
        timestamp=timestamp,
        occupied=occupied,
        occupied_limits=setpoint_limits_from_config(config.occupied),
        unoccupied_limits=setpoint_limits_from_config(config.unoccupied),
        capabilities=capabilities,
        maximum_hold_minutes=config.maximum_action_hold_minutes,
        observation_max_age_minutes=observation_max_age_minutes,
        demand_threshold_kw=config.demand_threshold_kw,
        freeze_protection_temperature_c=freeze_protection_temperature_c,
        overheat_protection_temperature_c=overheat_protection_temperature_c,
        next_action_generation=next_action_generation,
    )
