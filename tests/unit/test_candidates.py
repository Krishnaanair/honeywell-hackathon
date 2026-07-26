"""Candidate grid, scoring, cache, and cadence tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ecoloop.control.cadence import DecisionCadence, DecisionTrigger
from ecoloop.control.candidates import (
    evaluate_candidates,
    generate_candidate_actions,
    quantized_state_key,
    score_candidate,
)
from ecoloop.schemas import ActuatorCapabilities
from tests.unit._factories import NOW, candidate, constraints, observation


def test_candidate_grid_is_deterministic_unique_and_bounded() -> None:
    current = observation()
    limits = constraints()
    first = generate_candidate_actions(
        current,
        limits,
        baseline_heating_setpoint_c=21.0,
        baseline_cooling_setpoint_c=25.0,
    )
    second = generate_candidate_actions(
        current,
        limits,
        baseline_heating_setpoint_c=21.0,
        baseline_cooling_setpoint_c=25.0,
    )
    assert first == second
    assert len({item.candidate_id for item in first}) == len(first)
    assert first[0].heating_setpoint_c == current.heating_setpoint_c
    assert first[0].cooling_setpoint_c == current.cooling_setpoint_c
    assert all(19.0 <= item.heating_setpoint_c <= 21.0 for item in first)
    assert all(23.0 <= item.cooling_setpoint_c <= 25.0 for item in first)
    assert all(item.cooling_setpoint_c - item.heating_setpoint_c >= 2.0 for item in first)


def test_candidate_grid_rejects_run_or_occupancy_mismatch() -> None:
    with pytest.raises(ValueError, match="same run"):
        generate_candidate_actions(
            observation(),
            constraints(run_id="another-run"),
        )
    with pytest.raises(ValueError, match="occupancy"):
        generate_candidate_actions(
            observation(),
            constraints(occupied=False),
        )
    with pytest.raises(ValueError, match="capabilities"):
        generate_candidate_actions(
            observation(),
            constraints(capabilities=ActuatorCapabilities()),
        )


def test_score_components_sum_exactly_and_sort_best_first() -> None:
    current = observation()
    limits = constraints()
    candidates = [
        candidate(
            candidate_id="tight",
            heating_setpoint_c=20.0,
            cooling_setpoint_c=23.0,
        ),
        candidate(
            candidate_id="hold",
            heating_setpoint_c=20.0,
            cooling_setpoint_c=24.0,
        ),
        candidate(
            candidate_id="relaxed",
            heating_setpoint_c=19.0,
            cooling_setpoint_c=25.0,
        ),
    ]
    scores = evaluate_candidates(current, limits, candidates)
    assert scores == sorted(
        scores,
        key=lambda score: (score.total_score, score.candidate.candidate_id),
    )
    assert all(score.total_score == score.components.total for score in scores)
    assert any("not MPC" in assumption for assumption in scores[0].assumptions)


def test_hot_occupied_state_prefers_more_cooling_despite_change_penalty() -> None:
    hot = observation(
        zone_temperature_min_c=27.0,
        zone_temperature_mean_c=28.0,
        zone_temperature_max_c=29.0,
        operative_temperature_min_c=27.0,
        operative_temperature_mean_c=28.0,
        operative_temperature_max_c=29.0,
        pmv_mean=1.1,
        pmv_max_abs=1.2,
    )
    tighter = candidate(
        candidate_id="tighter",
        heating_setpoint_c=20.0,
        cooling_setpoint_c=23.0,
    )
    relaxed = candidate(
        candidate_id="relaxed",
        heating_setpoint_c=20.0,
        cooling_setpoint_c=25.0,
    )
    tight_score = score_candidate(hot, constraints(), tighter)
    relaxed_score = score_candidate(hot, constraints(), relaxed)
    assert tight_score.predicted_operative_temperature_c < (
        relaxed_score.predicted_operative_temperature_c
    )
    assert tight_score.total_score < relaxed_score.total_score


def test_scoring_refuses_candidate_outside_constraints() -> None:
    with pytest.raises(ValueError, match="outside"):
        score_candidate(
            observation(),
            constraints(),
            candidate(heating_setpoint_c=10.0),
        )
    with pytest.raises(ValueError, match="unsupported optional"):
        score_candidate(
            observation(),
            constraints(),
            candidate(ventilation_multiplier=1.1),
        )


def test_quantized_state_cache_key_is_stable_only_for_nearby_state() -> None:
    base = observation()
    near = observation(
        zone_temperature_min_c=23.1,
        zone_temperature_mean_c=24.1,
        zone_temperature_max_c=25.1,
        operative_temperature_min_c=23.1,
        operative_temperature_mean_c=24.1,
        operative_temperature_max_c=25.1,
        facility_demand_kw=51.0,
    )
    far = observation(
        zone_temperature_min_c=24.0,
        zone_temperature_mean_c=25.0,
        zone_temperature_max_c=26.0,
        operative_temperature_min_c=24.0,
        operative_temperature_mean_c=25.0,
        operative_temperature_max_c=26.0,
    )
    assert quantized_state_key(base) == quantized_state_key(near)
    assert quantized_state_key(base) != quantized_state_key(far)


def test_cadence_emits_normal_and_event_triggers() -> None:
    cadence = DecisionCadence(normal_interval_minutes=60)
    previous = observation(
        observation_id=1,
        occupied=False,
        occupancy_count=0.0,
        outdoor_temperature_c=25.0,
    )
    current = observation(
        observation_id=2,
        simulation_timestamp=NOW + timedelta(minutes=60),
        operative_temperature_min_c=22.4,
        pmv_max_abs=0.65,
        co2_max_ppm=950.0,
        facility_demand_kw=80.0,
        outdoor_temperature_c=30.0,
    )
    decision = cadence.evaluate(
        current,
        constraints(),
        previous_observation=previous,
        last_decision_simulation_timestamp=NOW,
        action_expires_at=NOW + timedelta(minutes=30),
    )
    assert decision.should_decide
    assert {
        DecisionTrigger.NORMAL_INTERVAL,
        DecisionTrigger.OPERATIVE_LIMIT,
        DecisionTrigger.PMV_LIMIT,
        DecisionTrigger.CO2_LIMIT,
        DecisionTrigger.DEMAND_LIMIT,
        DecisionTrigger.OCCUPANCY_CHANGE,
        DecisionTrigger.OUTDOOR_CHANGE,
        DecisionTrigger.ACTION_EXPIRY,
    } <= set(decision.triggers)


def test_first_observation_triggers_decision() -> None:
    decision = DecisionCadence().evaluate(
        observation(),
        constraints(),
        previous_observation=None,
        last_decision_simulation_timestamp=None,
        action_expires_at=None,
    )
    assert decision.triggers[0] is DecisionTrigger.FIRST_OBSERVATION


def test_cadence_does_not_repeat_level_trigger_until_risk_is_reentered() -> None:
    cadence = DecisionCadence(normal_interval_minutes=60)
    previous = observation(
        observation_id=1,
        operative_temperature_min_c=22.4,
        pmv_max_abs=0.65,
        co2_max_ppm=950.0,
        facility_demand_kw=80.0,
    )
    current = observation(
        observation_id=2,
        simulation_timestamp=NOW + timedelta(minutes=15),
        operative_temperature_min_c=22.3,
        pmv_max_abs=0.66,
        co2_max_ppm=960.0,
        facility_demand_kw=82.0,
    )
    persistent = cadence.evaluate(
        current,
        constraints(),
        previous_observation=previous,
        last_decision_simulation_timestamp=NOW,
        action_expires_at=NOW + timedelta(minutes=60),
    )
    assert not persistent.should_decide

    safe = observation(
        observation_id=3,
        simulation_timestamp=NOW + timedelta(minutes=30),
        operative_temperature_min_c=23.0,
        pmv_max_abs=0.2,
        co2_max_ppm=700.0,
        facility_demand_kw=50.0,
    )
    reentered = cadence.evaluate(
        current.model_copy(
            update={
                "observation_id": 4,
                "simulation_timestamp": NOW + timedelta(minutes=45),
            }
        ),
        constraints(),
        previous_observation=safe,
        last_decision_simulation_timestamp=NOW,
        action_expires_at=NOW + timedelta(minutes=60),
    )
    assert {
        DecisionTrigger.OPERATIVE_LIMIT,
        DecisionTrigger.PMV_LIMIT,
        DecisionTrigger.CO2_LIMIT,
        DecisionTrigger.DEMAND_LIMIT,
    } <= set(reentered.triggers)
