"""Deterministic fallback controller and repeated-failure circuit breaker."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from ecoloop.schemas import (
    BuildingObservation,
    CandidateAction,
    ControlAction,
    ControlConstraints,
    ReasonCode,
    ValidationResult,
)
from ecoloop.time_utils import as_utc


@dataclass(slots=True)
class CircuitBreaker:
    """Open after a configured number of consecutive control failures."""

    maximum_consecutive_failures: int
    consecutive_failures: int = 0
    is_open: bool = False

    def __post_init__(self) -> None:
        if self.maximum_consecutive_failures <= 0:
            raise ValueError("maximum_consecutive_failures must be positive")

    def record_success(self) -> None:
        """Close and reset after a successful validated decision."""

        self.consecutive_failures = 0
        self.is_open = False

    def record_failure(self) -> bool:
        """Record a failure and return whether the circuit is now open."""

        self.consecutive_failures += 1
        if self.consecutive_failures >= self.maximum_consecutive_failures:
            self.is_open = True
        return self.is_open

    def reset(self) -> None:
        """Explicitly reset the circuit for a new run or operator recovery."""

        self.record_success()


class DeterministicFallbackController:
    """Generate a conservative rule action from the latest trusted observation."""

    def create_action(
        self,
        observation: BuildingObservation,
        constraints: ControlConstraints,
        *,
        timestamp: datetime,
        reason_code: ReasonCode = ReasonCode.SAFE_FALLBACK,
        explanation: str = "Deterministic fallback selected safe schedule-like setpoints.",
    ) -> ControlAction:
        """Create a new fallback proposal; it still must pass safety validation."""

        if observation.run_id != constraints.run_id:
            raise ValueError("observation and constraints must belong to the same run")
        limits = constraints.active_limits
        if observation.occupied:
            heating = _clamp(
                20.0,
                float(limits.heating_min_c),
                float(limits.heating_max_c),
            )
            cooling = _clamp(
                25.0,
                float(limits.cooling_min_c),
                float(limits.cooling_max_c),
            )
            operative = (
                float(observation.operative_temperature_mean_c)
                if observation.operative_temperature_mean_c is not None
                else float(observation.zone_temperature_mean_c)
            )
            if limits.operative_min_c is not None and operative < limits.operative_min_c:
                heating = min(
                    float(limits.heating_max_c),
                    float(observation.heating_setpoint_c) + float(limits.maximum_change_c),
                )
            if limits.operative_max_c is not None and operative > limits.operative_max_c:
                cooling = max(
                    float(limits.cooling_min_c),
                    float(observation.cooling_setpoint_c) - float(limits.maximum_change_c),
                )
        else:
            heating = float(limits.heating_min_c)
            cooling = float(limits.cooling_max_c)
        if cooling - heating < limits.minimum_deadband_c:
            heating = min(heating, cooling - float(limits.minimum_deadband_c))

        hold_minutes = min(60, constraints.maximum_hold_minutes)
        now = as_utc(timestamp)
        candidate = CandidateAction(
            candidate_id=f"fallback-{observation.observation_id}",
            heating_setpoint_c=heating,
            cooling_setpoint_c=cooling,
            hold_minutes=hold_minutes,
        )
        identity = (
            f"{observation.run_id}|{observation.observation_id}|"
            f"{constraints.next_action_generation}|{now.isoformat()}"
        )
        action_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return ControlAction(
            action_id=f"fallback-{action_hash}",
            run_id=observation.run_id,
            observation_id=observation.observation_id,
            action_generation=constraints.next_action_generation,
            timestamp=now,
            expires_at=now + timedelta(minutes=hold_minutes),
            action=candidate,
            model="deterministic-fallback",
            latency_ms=0.0,
            reason_code=reason_code,
            explanation=explanation,
            fallback=True,
        )


def reusable_last_safe_action(
    last_safe: tuple[ControlAction, ValidationResult] | None,
    *,
    observation: BuildingObservation,
    constraints: ControlConstraints,
    timestamp: datetime,
    consecutive_failures: int,
) -> ControlAction | None:
    """Reuse applied values for one interval, with fresh IDs/generation/expiry."""

    if consecutive_failures != 1 or last_safe is None:
        return None
    prior_action, prior_validation = last_safe
    if not prior_validation.accepted or prior_validation.applied_action is None:
        return None
    if prior_action.run_id != observation.run_id:
        return None
    hold_minutes = min(
        prior_validation.applied_action.hold_minutes,
        60,
        constraints.maximum_hold_minutes,
    )
    now = as_utc(timestamp)
    values = prior_validation.applied_action.model_copy(
        update={
            "candidate_id": f"last-safe-{observation.observation_id}",
            "hold_minutes": hold_minutes,
        }
    )
    identity = (
        f"{observation.run_id}|{observation.observation_id}|"
        f"{constraints.next_action_generation}|last-safe"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return ControlAction(
        action_id=f"last-safe-{digest}",
        run_id=observation.run_id,
        observation_id=observation.observation_id,
        action_generation=constraints.next_action_generation,
        timestamp=now,
        expires_at=now + timedelta(minutes=hold_minutes),
        action=values,
        model="last-known-safe",
        latency_ms=0.0,
        reason_code=ReasonCode.SAFE_FALLBACK,
        explanation="Reused the last validated values for one bounded interval.",
        fallback=True,
    )


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
