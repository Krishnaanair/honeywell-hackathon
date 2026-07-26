"""Independent deterministic guardrails for every control-affecting action."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from ecoloop.schemas import (
    BuildingObservation,
    ClampDetail,
    ControlAction,
    ControlConstraints,
    ValidationCode,
    ValidationIssue,
    ValidationResult,
)
from ecoloop.time_utils import as_utc, elapsed_minutes


@dataclass(frozen=True, slots=True)
class SafetyContext:
    """Trusted simulation/controller state used by the validator."""

    expected_run_id: str
    latest_observation: BuildingObservation
    constraints: ControlConstraints
    last_applied_generation: int
    now: datetime
    applied_action_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        as_utc(self.now)
        if self.last_applied_generation < 0:
            raise ValueError("last_applied_generation must be non-negative")
        if self.latest_observation.run_id != self.expected_run_id:
            raise ValueError("latest observation does not belong to expected_run_id")
        if self.constraints.run_id != self.expected_run_id:
            raise ValueError("constraints do not belong to expected_run_id")


class SafetyValidator:
    """Validate, clamp, and explain an action without consulting the model."""

    def validate(
        self,
        action: ControlAction,
        context: SafetyContext,
    ) -> ValidationResult:
        """Return a rejection or an actuator-safe action with clamp details."""

        now = as_utc(context.now)
        issues = self._metadata_issues(action, context)
        if issues:
            return self._rejected(action, now, issues)

        capabilities = context.constraints.capabilities
        unsupported: list[str] = []
        if not capabilities.heating_setpoint:
            unsupported.append("heating_setpoint_c")
        if not capabilities.cooling_setpoint:
            unsupported.append("cooling_setpoint_c")
        optional_capabilities = (
            (
                "ventilation_multiplier",
                action.action.ventilation_multiplier,
                capabilities.ventilation_multiplier,
            ),
            ("lighting_fraction", action.action.lighting_fraction, capabilities.lighting_fraction),
            (
                "supply_air_temperature_c",
                action.action.supply_air_temperature_c,
                capabilities.supply_air_temperature,
            ),
            ("shading_state", action.action.shading_state, capabilities.shading_state),
        )
        unsupported.extend(
            name
            for name, value, supported in optional_capabilities
            if value is not None and not supported
        )
        if unsupported:
            return self._rejected(
                action,
                now,
                tuple(
                    ValidationIssue(
                        code=ValidationCode.UNSUPPORTED_ACTUATOR,
                        field=field,
                        message=f"actuator capability is not available: {field}",
                    )
                    for field in unsupported
                ),
            )

        limits = context.constraints.active_limits
        observation = context.latest_observation
        current_heating = float(observation.heating_setpoint_c)
        current_cooling = float(observation.cooling_setpoint_c)
        heating_low, heating_high, heating_transition = ramp_envelope(
            current_heating,
            lower=float(limits.heating_min_c),
            upper=float(limits.heating_max_c),
            maximum_change=float(limits.maximum_change_c),
        )
        cooling_low, cooling_high, cooling_transition = ramp_envelope(
            current_cooling,
            lower=float(limits.cooling_min_c),
            upper=float(limits.cooling_max_c),
            maximum_change=float(limits.maximum_change_c),
        )

        clamps: list[ClampDetail] = []
        accepted_issues: list[ValidationIssue] = []
        if heating_transition:
            accepted_issues.append(
                ValidationIssue(
                    code=ValidationCode.SETPOINT_CLAMPED,
                    field="heating_setpoint_c",
                    message=(
                        "prior heating schedule was outside the active occupancy "
                        "bounds; the nearest active boundary overrides the normal "
                        "decision ramp for this transition only"
                    ),
                )
            )
        if cooling_transition:
            accepted_issues.append(
                ValidationIssue(
                    code=ValidationCode.SETPOINT_CLAMPED,
                    field="cooling_setpoint_c",
                    message=(
                        "prior cooling schedule was outside the active occupancy "
                        "bounds; the nearest active boundary overrides the normal "
                        "decision ramp for this transition only"
                    ),
                )
            )
        proposed_heating = float(action.action.heating_setpoint_c)
        proposed_cooling = float(action.action.cooling_setpoint_c)
        heating = _clamp(proposed_heating, heating_low, heating_high)
        cooling = _clamp(proposed_cooling, cooling_low, cooling_high)
        self._record_numeric_clamp(
            clamps,
            field="heating_setpoint_c",
            proposed=proposed_heating,
            applied=heating,
            code=(
                ValidationCode.SETPOINT_CLAMPED
                if heating_transition
                or proposed_heating < limits.heating_min_c
                or proposed_heating > limits.heating_max_c
                else ValidationCode.RATE_CLAMPED
            ),
            message=(
                "heating setpoint was moved to the nearest active occupancy boundary"
                if heating_transition
                else "heating setpoint was clamped to active bounds and ramp rate"
            ),
        )
        self._record_numeric_clamp(
            clamps,
            field="cooling_setpoint_c",
            proposed=proposed_cooling,
            applied=cooling,
            code=(
                ValidationCode.SETPOINT_CLAMPED
                if cooling_transition
                or proposed_cooling < limits.cooling_min_c
                or proposed_cooling > limits.cooling_max_c
                else ValidationCode.RATE_CLAMPED
            ),
            message=(
                "cooling setpoint was moved to the nearest active occupancy boundary"
                if cooling_transition
                else "cooling setpoint was clamped to active bounds and ramp rate"
            ),
        )

        hold_minutes = min(
            action.action.hold_minutes,
            context.constraints.maximum_hold_minutes,
        )
        self._record_numeric_clamp(
            clamps,
            field="hold_minutes",
            proposed=action.action.hold_minutes,
            applied=hold_minutes,
            code=ValidationCode.HOLD_CLAMPED,
            message="hold duration was clamped to the configured maximum",
        )

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
        comfort_cold = bool(
            limits.operative_min_c is not None and operative_min < limits.operative_min_c
        ) or bool(
            limits.absolute_pmv_max is not None
            and observation.pmv_mean is not None
            and observation.pmv_mean < -limits.absolute_pmv_max
        )
        comfort_hot = bool(
            limits.operative_max_c is not None and operative_max > limits.operative_max_c
        ) or bool(
            limits.absolute_pmv_max is not None
            and observation.pmv_mean is not None
            and observation.pmv_mean > limits.absolute_pmv_max
        )

        if comfort_cold:
            protected_heating = heating_high
            self._record_numeric_clamp(
                clamps,
                field="heating_setpoint_c",
                proposed=heating,
                applied=protected_heating,
                code=ValidationCode.SETPOINT_CLAMPED,
                message="occupied cold/PMV condition requires maximum safe heating response",
            )
            heating = protected_heating
        if comfort_hot:
            protected_cooling = cooling_low
            self._record_numeric_clamp(
                clamps,
                field="cooling_setpoint_c",
                proposed=cooling,
                applied=protected_cooling,
                code=ValidationCode.SETPOINT_CLAMPED,
                message="occupied hot/PMV condition requires maximum safe cooling response",
            )
            cooling = protected_cooling

        freeze_active = observation.zone_temperature_min_c <= (
            context.constraints.freeze_protection_temperature_c
        )
        overheat_active = observation.zone_temperature_max_c >= (
            context.constraints.overheat_protection_temperature_c
        )
        if freeze_active:
            freeze_heating = float(limits.heating_max_c)
            self._record_numeric_clamp(
                clamps,
                field="heating_setpoint_c",
                proposed=heating,
                applied=freeze_heating,
                code=ValidationCode.FREEZE_OVERRIDE,
                message="freeze protection overrides the normal ramp rate",
            )
            heating = freeze_heating
        if overheat_active:
            overheat_cooling = float(limits.cooling_min_c)
            self._record_numeric_clamp(
                clamps,
                field="cooling_setpoint_c",
                proposed=cooling,
                applied=overheat_cooling,
                code=ValidationCode.OVERHEAT_OVERRIDE,
                message="overheat protection overrides the normal ramp rate",
            )
            cooling = overheat_cooling

        demand = observation.facility_demand_kw
        demand_emergency = (
            demand is not None
            and demand > context.constraints.demand_threshold_kw
            and not comfort_cold
            and not comfort_hot
            and not freeze_active
            and not overheat_active
        )
        if demand_emergency:
            demand_heating = max(heating_low, heating - 1.0)
            demand_cooling = min(cooling_high, cooling + 1.0)
            self._record_numeric_clamp(
                clamps,
                field="heating_setpoint_c",
                proposed=heating,
                applied=demand_heating,
                code=ValidationCode.DEMAND_OVERRIDE,
                message="demand threshold exceeded; heating was relaxed within safe limits",
            )
            self._record_numeric_clamp(
                clamps,
                field="cooling_setpoint_c",
                proposed=cooling,
                applied=demand_cooling,
                code=ValidationCode.DEMAND_OVERRIDE,
                message="demand threshold exceeded; cooling was relaxed within safe limits",
            )
            heating, cooling = demand_heating, demand_cooling

        ventilation = action.action.ventilation_multiplier
        if (
            limits.co2_max_ppm is not None
            and observation.co2_max_ppm is not None
            and observation.co2_max_ppm > limits.co2_max_ppm
            and capabilities.ventilation_multiplier
        ):
            proposed_ventilation = ventilation if ventilation is not None else 1.0
            protected_ventilation = max(1.0, proposed_ventilation)
            self._record_numeric_clamp(
                clamps,
                field="ventilation_multiplier",
                proposed=proposed_ventilation,
                applied=protected_ventilation,
                code=ValidationCode.SETPOINT_CLAMPED,
                message="CO2 threshold exceeded; ventilation cannot be reduced below nominal",
            )
            ventilation = protected_ventilation

        feasible = _restore_deadband(
            heating,
            cooling,
            minimum_deadband=float(limits.minimum_deadband_c),
            heating_low=float(limits.heating_min_c) if freeze_active else heating_low,
            heating_high=float(limits.heating_max_c),
            cooling_low=float(limits.cooling_min_c),
            cooling_high=float(limits.cooling_max_c) if overheat_active else cooling_high,
        )
        if feasible is None:
            return self._rejected(
                action,
                now,
                (
                    ValidationIssue(
                        code=ValidationCode.INVALID_DEADBAND,
                        field="heating_setpoint_c,cooling_setpoint_c",
                        message="no feasible setpoint pair satisfies the minimum deadband",
                    ),
                ),
            )
        deadband_heating, deadband_cooling = feasible
        self._record_numeric_clamp(
            clamps,
            field="heating_setpoint_c",
            proposed=heating,
            applied=deadband_heating,
            code=ValidationCode.INVALID_DEADBAND,
            message="heating setpoint was lowered to restore minimum deadband",
        )
        self._record_numeric_clamp(
            clamps,
            field="cooling_setpoint_c",
            proposed=cooling,
            applied=deadband_cooling,
            code=ValidationCode.INVALID_DEADBAND,
            message="cooling setpoint was raised to restore minimum deadband",
        )
        heating, cooling = deadband_heating, deadband_cooling
        if heating >= cooling or cooling - heating < limits.minimum_deadband_c - 1e-9:
            return self._rejected(
                action,
                now,
                (
                    ValidationIssue(
                        code=ValidationCode.INVALID_DEADBAND,
                        message="validator could not restore a valid thermostat deadband",
                    ),
                ),
            )

        applied = action.action.model_copy(
            update={
                "heating_setpoint_c": heating,
                "cooling_setpoint_c": cooling,
                "hold_minutes": hold_minutes,
                "ventilation_multiplier": ventilation,
            }
        )
        maximum_expiry = min(
            as_utc(action.timestamp) + timedelta(minutes=hold_minutes),
            as_utc(action.timestamp) + timedelta(minutes=context.constraints.maximum_hold_minutes),
        )
        applied_expiry = min(as_utc(action.expires_at), maximum_expiry)
        if applied_expiry < as_utc(action.expires_at):
            accepted_issues.append(
                ValidationIssue(
                    code=ValidationCode.HOLD_CLAMPED,
                    field="expires_at",
                    message="action expiry was shortened to the validated hold duration",
                )
            )
        return ValidationResult(
            run_id=action.run_id,
            observation_id=action.observation_id,
            action_generation=action.action_generation,
            timestamp=now,
            accepted=True,
            proposed_action=action.action,
            applied_action=applied,
            applied_expires_at=applied_expiry,
            issues=tuple(accepted_issues),
            clamps=tuple(clamps),
            fallback_status="active" if action.fallback else None,
        )

    @staticmethod
    def _metadata_issues(
        action: ControlAction,
        context: SafetyContext,
    ) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        observation = context.latest_observation
        constraints = context.constraints
        now = as_utc(context.now)
        if action.action_id in context.applied_action_ids:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.DUPLICATE_ACTION,
                    field="action_id",
                    message="action_id has already been applied",
                )
            )
        if action.run_id != context.expected_run_id:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.WRONG_RUN,
                    field="run_id",
                    message="action belongs to a different run",
                )
            )
        if action.observation_id != observation.observation_id:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.STALE_OBSERVATION,
                    field="observation_id",
                    message="action does not target the latest observation",
                )
            )
        age_minutes = elapsed_minutes(observation.timestamp, now)
        if age_minutes < -1.0:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.FUTURE_OBSERVATION,
                    field="observation_id",
                    message="observation timestamp is unexpectedly in the future",
                )
            )
        elif age_minutes > constraints.observation_max_age_minutes:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.STALE_OBSERVATION,
                    field="observation_id",
                    message=(
                        f"observation age {age_minutes:.1f} minutes exceeds "
                        f"{constraints.observation_max_age_minutes} minutes"
                    ),
                )
            )
        if as_utc(action.expires_at) <= now:
            issues.append(
                ValidationIssue(
                    code=ValidationCode.EXPIRED_ACTION,
                    field="expires_at",
                    message="action has expired",
                )
            )
        if as_utc(action.expires_at) <= as_utc(action.timestamp):
            issues.append(
                ValidationIssue(
                    code=ValidationCode.INVALID_EXPIRY,
                    field="expires_at",
                    message="action expiry must follow its creation timestamp",
                )
            )
        if (
            action.action_generation <= context.last_applied_generation
            or action.action_generation != constraints.next_action_generation
        ):
            issues.append(
                ValidationIssue(
                    code=ValidationCode.NON_MONOTONIC_GENERATION,
                    field="action_generation",
                    message=(
                        "action generation must equal the next configured generation "
                        "and exceed the last applied generation"
                    ),
                )
            )
        numeric_values = (
            action.action.heating_setpoint_c,
            action.action.cooling_setpoint_c,
            action.action.ventilation_multiplier,
            action.action.lighting_fraction,
            action.action.supply_air_temperature_c,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric_values):
            issues.append(
                ValidationIssue(
                    code=ValidationCode.NON_FINITE,
                    message="all numeric action values must be finite",
                )
            )
        return tuple(issues)

    @staticmethod
    def _record_numeric_clamp(
        clamps: list[ClampDetail],
        *,
        field: str,
        proposed: float | int,
        applied: float | int,
        code: ValidationCode,
        message: str,
    ) -> None:
        if not math.isclose(float(proposed), float(applied), abs_tol=1e-9):
            clamps.append(
                ClampDetail(
                    code=code,
                    field=field,
                    proposed_value=proposed,
                    applied_value=applied,
                    message=message,
                )
            )

    @staticmethod
    def _rejected(
        action: ControlAction,
        timestamp: datetime,
        issues: tuple[ValidationIssue, ...],
    ) -> ValidationResult:
        return ValidationResult(
            run_id=action.run_id,
            observation_id=action.observation_id,
            action_generation=action.action_generation,
            timestamp=timestamp,
            accepted=False,
            proposed_action=action.action,
            applied_action=None,
            issues=issues,
            fallback_status="rejected",
        )


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def ramp_envelope(
    current: float,
    *,
    lower: float,
    upper: float,
    maximum_change: float,
) -> tuple[float, float, bool]:
    """Return a feasible ramp envelope and flag an occupancy-boundary transition.

    Normal decisions remain rate-limited. If a trusted current schedule is so
    far outside the newly active occupancy envelope that the two constraints do
    not intersect, the one transition action is pinned to the nearest active
    boundary. This is narrower than disabling the rate limit and cannot move an
    actuator beyond the configured active bounds.
    """

    ramp_low = current - maximum_change
    ramp_high = current + maximum_change
    feasible_low = max(lower, ramp_low)
    feasible_high = min(upper, ramp_high)
    if feasible_low <= feasible_high:
        return feasible_low, feasible_high, False
    boundary = _clamp(current, lower, upper)
    return boundary, boundary, True


def _restore_deadband(
    heating: float,
    cooling: float,
    *,
    minimum_deadband: float,
    heating_low: float,
    heating_high: float,
    cooling_low: float,
    cooling_high: float,
) -> tuple[float, float] | None:
    """Return the nearest feasible pair using symmetric deterministic adjustment."""

    heating = _clamp(heating, heating_low, heating_high)
    cooling = _clamp(cooling, cooling_low, cooling_high)
    deficit = heating + minimum_deadband - cooling
    if deficit <= 1e-9:
        return heating, cooling
    lower_capacity = heating - heating_low
    raise_capacity = cooling_high - cooling
    if lower_capacity + raise_capacity + 1e-9 < deficit:
        return None
    lower_share = min(lower_capacity, deficit / 2.0)
    heating -= lower_share
    remaining = deficit - lower_share
    raise_share = min(raise_capacity, remaining)
    cooling += raise_share
    remaining -= raise_share
    if remaining > 1e-9:
        extra_lower = min(heating - heating_low, remaining)
        heating -= extra_lower
        remaining -= extra_lower
    if remaining > 1e-9:
        return None
    return _clamp(heating, heating_low, heating_high), _clamp(cooling, cooling_low, cooling_high)
