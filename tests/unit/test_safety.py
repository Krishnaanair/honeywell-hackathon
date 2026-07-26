"""Deterministic validator and fallback tests."""

from __future__ import annotations

from datetime import timedelta

from ecoloop.control.fallback import (
    CircuitBreaker,
    DeterministicFallbackController,
    reusable_last_safe_action,
)
from ecoloop.control.safety import SafetyContext, SafetyValidator
from ecoloop.schemas import ActuatorCapabilities, ReasonCode, ValidationCode
from tests.unit._factories import NOW, action, candidate, constraints, observation


def _validate(**action_updates: object):
    current = observation()
    limits = constraints()
    proposal = action(**action_updates)
    result = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=limits,
            last_applied_generation=0,
            now=NOW,
        ),
    )
    return proposal, result


def test_accepts_fresh_supported_safe_action_without_modification() -> None:
    proposal, result = _validate()
    assert result.accepted
    assert result.applied_action == proposal.action
    assert result.clamps == ()
    assert result.issues == ()


def test_clamps_setpoints_ramp_rate_and_maximum_hold() -> None:
    proposed_values = candidate(
        heating_setpoint_c=30.0,
        cooling_setpoint_c=18.0,
        hold_minutes=240,
    )
    _, result = _validate(
        action=proposed_values,
        expires_at=NOW + timedelta(minutes=240),
    )
    assert result.accepted
    assert result.applied_action is not None
    assert result.applied_action.heating_setpoint_c == 21.0
    assert result.applied_action.cooling_setpoint_c == 23.0
    assert result.applied_action.hold_minutes == 120
    assert {clamp.field for clamp in result.clamps} >= {
        "heating_setpoint_c",
        "cooling_setpoint_c",
        "hold_minutes",
    }
    assert result.applied_expires_at == NOW + timedelta(minutes=120)
    assert any(issue.code is ValidationCode.HOLD_CLAMPED for issue in result.issues)


def test_occupancy_transition_uses_nearest_active_boundary_then_resumes_ramp() -> None:
    current = observation(
        occupied=True,
        heating_setpoint_c=17.0,
        cooling_setpoint_c=29.0,
    )
    limits = constraints(occupied=True)
    proposal = action(
        action=candidate(
            heating_setpoint_c=20.0,
            cooling_setpoint_c=25.0,
        )
    )

    transition = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=limits,
            last_applied_generation=0,
            now=NOW,
        ),
    )

    assert transition.accepted
    assert transition.applied_action is not None
    assert transition.applied_action.heating_setpoint_c == 19.0
    assert transition.applied_action.cooling_setpoint_c == 26.0
    assert {
        issue.field for issue in transition.issues if issue.code is ValidationCode.SETPOINT_CLAMPED
    } == {"heating_setpoint_c", "cooling_setpoint_c"}
    assert all(
        clamp.code is ValidationCode.SETPOINT_CLAMPED
        for clamp in transition.clamps
        if clamp.field in {"heating_setpoint_c", "cooling_setpoint_c"}
    )

    next_observation = current.model_copy(
        update={
            "observation_id": 2,
            "heating_setpoint_c": 19.0,
            "cooling_setpoint_c": 26.0,
        }
    )
    next_action = action(
        action_id="action-test-2",
        observation_id=2,
        action_generation=2,
        action=candidate(
            candidate_id="candidate-next",
            heating_setpoint_c=22.0,
            cooling_setpoint_c=23.0,
        ),
    )
    normal = SafetyValidator().validate(
        next_action,
        SafetyContext(
            expected_run_id=next_observation.run_id,
            latest_observation=next_observation,
            constraints=limits.model_copy(update={"next_action_generation": 2}),
            last_applied_generation=1,
            now=NOW,
        ),
    )
    assert normal.accepted
    assert normal.applied_action is not None
    assert normal.applied_action.heating_setpoint_c == 20.0
    assert normal.applied_action.cooling_setpoint_c == 25.0
    assert not any("transition only" in issue.message for issue in normal.issues)


def test_restores_deadband_with_nearest_symmetric_adjustment() -> None:
    current = observation(heating_setpoint_c=21.0, cooling_setpoint_c=23.0)
    limits = constraints()
    proposal = action(action=candidate(heating_setpoint_c=22.0, cooling_setpoint_c=23.0))
    result = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=limits,
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert result.accepted
    assert result.applied_action is not None
    assert (
        result.applied_action.cooling_setpoint_c - result.applied_action.heating_setpoint_c >= 2.0
    )
    assert any(clamp.code is ValidationCode.INVALID_DEADBAND for clamp in result.clamps)


def test_rejects_wrong_run_stale_expired_duplicate_and_generation() -> None:
    current = observation()
    limits = constraints()
    proposal = action(
        run_id="wrong-run",
        observation_id=2,
        action_generation=1,
        expires_at=NOW - timedelta(minutes=1),
    )
    result = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=limits,
            last_applied_generation=1,
            now=NOW,
            applied_action_ids=frozenset({proposal.action_id}),
        ),
    )
    assert not result.accepted
    codes = {issue.code for issue in result.issues}
    assert {
        ValidationCode.WRONG_RUN,
        ValidationCode.STALE_OBSERVATION,
        ValidationCode.EXPIRED_ACTION,
        ValidationCode.NON_MONOTONIC_GENERATION,
        ValidationCode.DUPLICATE_ACTION,
    } <= codes


def test_rejects_stale_observation_by_wall_clock_age() -> None:
    current = observation(timestamp=NOW - timedelta(minutes=31))
    proposal = action()
    result = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert not result.accepted
    assert ValidationCode.STALE_OBSERVATION in {issue.code for issue in result.issues}


def test_rejects_values_for_unsupported_optional_actuator() -> None:
    proposal = action(action=candidate(ventilation_multiplier=1.2))
    current = observation()
    result = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert not result.accepted
    assert result.issues[0].code is ValidationCode.UNSUPPORTED_ACTUATOR
    assert result.issues[0].field == "ventilation_multiplier"


def test_safety_layer_rejects_non_finite_value_even_after_schema_bypass() -> None:
    unsafe_values = candidate().model_copy(update={"heating_setpoint_c": float("nan")})
    proposal = action(action=unsafe_values)
    current = observation()
    result = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert not result.accepted
    assert ValidationCode.NON_FINITE in {issue.code for issue in result.issues}


def test_rejects_when_mandatory_setpoint_actuator_is_missing() -> None:
    current = observation()
    unavailable = constraints(
        capabilities=ActuatorCapabilities(
            heating_setpoint=True,
            cooling_setpoint=False,
        )
    )
    result = SafetyValidator().validate(
        action(),
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=unavailable,
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert not result.accepted
    assert result.issues[0].field == "cooling_setpoint_c"


def test_demand_override_relaxes_setpoints_inside_safe_envelope() -> None:
    current = observation(facility_demand_kw=90.0)
    result = SafetyValidator().validate(
        action(),
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert result.accepted
    assert result.applied_action is not None
    assert result.applied_action.heating_setpoint_c == 19.0
    assert result.applied_action.cooling_setpoint_c == 25.0
    assert sum(clamp.code is ValidationCode.DEMAND_OVERRIDE for clamp in result.clamps) == 2


def test_freeze_and_overheat_protection_override_normal_ramp() -> None:
    freeze_observation = observation(
        zone_temperature_min_c=6.0,
        zone_temperature_mean_c=15.0,
        zone_temperature_max_c=24.0,
        operative_temperature_min_c=6.0,
        operative_temperature_mean_c=15.0,
        operative_temperature_max_c=24.0,
    )
    freeze = SafetyValidator().validate(
        action(),
        SafetyContext(
            expected_run_id=freeze_observation.run_id,
            latest_observation=freeze_observation,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert freeze.accepted and freeze.applied_action is not None
    assert freeze.applied_action.heating_setpoint_c == 22.0
    assert any(clamp.code is ValidationCode.FREEZE_OVERRIDE for clamp in freeze.clamps)

    hot_observation = observation(
        zone_temperature_min_c=24.0,
        zone_temperature_mean_c=30.0,
        zone_temperature_max_c=36.0,
        operative_temperature_min_c=24.0,
        operative_temperature_mean_c=30.0,
        operative_temperature_max_c=36.0,
        cooling_setpoint_c=26.0,
    )
    overheat = SafetyValidator().validate(
        action(),
        SafetyContext(
            expected_run_id=hot_observation.run_id,
            latest_observation=hot_observation,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert overheat.accepted and overheat.applied_action is not None
    assert overheat.applied_action.cooling_setpoint_c == 23.0
    assert any(clamp.code is ValidationCode.OVERHEAT_OVERRIDE for clamp in overheat.clamps)


def test_circuit_breaker_opens_and_resets_deterministically() -> None:
    breaker = CircuitBreaker(maximum_consecutive_failures=3)
    assert not breaker.record_failure()
    assert not breaker.record_failure()
    assert breaker.record_failure()
    assert breaker.is_open
    breaker.record_success()
    assert not breaker.is_open
    assert breaker.consecutive_failures == 0


def test_fallback_and_one_interval_last_safe_reuse_still_require_validation() -> None:
    current = observation()
    limits = constraints()
    controller = DeterministicFallbackController()
    fallback = controller.create_action(
        current,
        limits,
        timestamp=NOW,
        reason_code=ReasonCode.TIMEOUT_FALLBACK,
    )
    validation = SafetyValidator().validate(
        fallback,
        SafetyContext(
            expected_run_id=current.run_id,
            latest_observation=current,
            constraints=limits,
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert fallback.fallback
    assert validation.accepted
    reused = reusable_last_safe_action(
        (fallback, validation),
        observation=current.model_copy(update={"observation_id": 2}),
        constraints=limits.model_copy(update={"next_action_generation": 2}),
        timestamp=NOW + timedelta(minutes=15),
        consecutive_failures=1,
    )
    assert reused is not None
    assert reused.action_generation == 2
    assert (
        reusable_last_safe_action(
            (fallback, validation),
            observation=current.model_copy(update={"observation_id": 3}),
            constraints=limits.model_copy(update={"next_action_generation": 3}),
            timestamp=NOW + timedelta(minutes=30),
            consecutive_failures=2,
        )
        is None
    )
