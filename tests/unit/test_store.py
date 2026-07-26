"""SQLite WAL audit-bus tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from ecoloop.control.safety import SafetyContext, SafetyValidator
from ecoloop.db.store import DataConflictError, SQLiteStore, _json_dumps
from ecoloop.exceptions import RunStateError
from ecoloop.schemas import (
    BuildingTelemetry,
    ClampDetail,
    MessageSeverity,
    RunStatus,
    RunType,
    ToolCallTrace,
    ValidationCode,
    ValidationResult,
    ZoneTelemetry,
)
from tests.unit._factories import NOW, action, candidate, constraints, observation_input


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "bus" / "ecoloop.db", clock=lambda: NOW)


def _create_running_run(store: SQLiteStore, run_id: str = "run-agent-1") -> None:
    store.create_run(
        run_id,
        RunType.AGENT,
        energyplus_version="26.1.0",
        period_name="smoke",
    )
    store.set_run_status(run_id, RunStatus.RUNNING)


def test_json_dumps_recursively_serializes_models_and_rejects_nan() -> None:
    clamp = ClampDetail(
        code=ValidationCode.SETPOINT_CLAMPED,
        field="cooling_setpoint_c",
        proposed_value=30.0,
        applied_value=26.0,
        message="cooling setpoint was clamped",
    )

    payload = _json_dumps({"nested": ([clamp],), "count": 1})

    assert json.loads(payload) == {
        "count": 1,
        "nested": [
            [
                {
                    "applied_value": 26.0,
                    "code": "setpoint_clamped",
                    "field": "cooling_setpoint_c",
                    "message": "cooling setpoint was clamped",
                    "proposed_value": 30.0,
                }
            ]
        ],
    }
    with pytest.raises(ValueError, match="Out of range float values"):
        _json_dumps({"nested": [{"invalid": float("nan")}]})


def test_migrations_create_required_tables_and_enable_wal(store: SQLiteStore) -> None:
    connection = sqlite3.connect(store.path)
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    connection.close()
    assert str(journal_mode).casefold() == "wal"
    assert user_version == 1
    assert {
        "runs",
        "telemetry",
        "zone_telemetry",
        "observations",
        "proposed_actions",
        "applied_actions",
        "agent_decisions",
        "tool_calls",
        "simulation_messages",
        "errors",
        "metrics",
        "run_artifacts",
    } <= tables


def test_run_lifecycle_is_explicit_and_terminal(store: SQLiteStore) -> None:
    _create_running_run(store)
    completed = store.set_run_status("run-agent-1", RunStatus.COMPLETED)
    assert completed.status is RunStatus.COMPLETED
    assert completed.progress_percent == 100.0
    with pytest.raises(RunStateError, match="invalid run transition"):
        store.set_run_status("run-agent-1", RunStatus.RUNNING)


def test_fatal_error_prevents_run_completion(store: SQLiteStore) -> None:
    _create_running_run(store)
    store.record_error(
        "run-agent-1",
        MessageSeverity.FATAL,
        "eplusout.err",
        "Program terminated due to fatal error",
    )
    with pytest.raises(RunStateError, match="fatal"):
        store.set_run_status("run-agent-1", RunStatus.COMPLETED)


def test_observation_ids_are_monotonic_and_duplicates_are_idempotent(
    store: SQLiteStore,
) -> None:
    _create_running_run(store)
    first_input = observation_input()
    first = store.record_observation(first_input)
    duplicate = store.record_observation(first_input)
    second = store.record_observation(
        observation_input(
            timestep_key="ts-2",
            simulation_timestamp=NOW + timedelta(minutes=15),
        )
    )
    assert duplicate.observation_id == first.observation_id
    assert second.observation_id > first.observation_id
    assert store.get_current_observation("run-agent-1") == second
    assert [item.observation_id for item in store.get_recent_observations("run-agent-1")] == [
        first.observation_id,
        second.observation_id,
    ]


def test_reusing_timestep_key_with_different_payload_is_a_conflict(
    store: SQLiteStore,
) -> None:
    _create_running_run(store)
    store.record_observation(observation_input())
    with pytest.raises(DataConflictError, match="different observation"):
        store.record_observation(observation_input(occupancy_count=13.0))


def test_duplicate_telemetry_and_zone_callback_rows_are_suppressed(
    store: SQLiteStore,
) -> None:
    _create_running_run(store)
    facility = BuildingTelemetry(
        run_id="run-agent-1",
        timestamp=NOW,
        simulation_timestamp=NOW,
        timestep_key="ts-1",
        environment="RUN PERIOD 1",
        outdoor_temperature_c=32.0,
        facility_demand_kw=50.0,
        timestep_electricity_kwh=12.5,
        cumulative_electricity_kwh=250.0,
    )
    zone = ZoneTelemetry(
        run_id="run-agent-1",
        timestamp=NOW,
        simulation_timestamp=NOW,
        timestep_key="ts-1",
        environment="RUN PERIOD 1",
        zone_name="Core Zone",
        mean_air_temperature_c=24.0,
    )
    assert store.record_telemetry(facility)
    assert not store.record_telemetry(facility)
    assert store.record_zone_telemetry(zone)
    assert not store.record_zone_telemetry(zone)
    with pytest.raises(DataConflictError, match="different telemetry"):
        store.record_telemetry(facility.model_copy(update={"facility_demand_kw": 51.0}))
    with pytest.raises(DataConflictError, match="different telemetry"):
        store.record_zone_telemetry(zone.model_copy(update={"mean_air_temperature_c": 24.5}))


def test_validated_action_application_is_atomic_and_idempotent(
    store: SQLiteStore,
) -> None:
    _create_running_run(store)
    persisted = store.record_observation(observation_input())
    proposal = action(
        observation_id=persisted.observation_id,
        expires_at=NOW + timedelta(minutes=240),
        action=candidate(
            heating_setpoint_c=30.0,
            cooling_setpoint_c=18.0,
            hold_minutes=240,
        ),
    )
    result = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id="run-agent-1",
            latest_observation=persisted,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    first = store.apply_validated_action(
        proposal,
        result,
        expected_run_id="run-agent-1",
        timestamp=NOW,
    )
    duplicate = store.apply_validated_action(
        proposal,
        result,
        expected_run_id="run-agent-1",
        timestamp=NOW,
    )
    assert first.applied
    assert not duplicate.applied
    assert duplicate.idempotent_duplicate
    latest = store.get_last_applied_action("run-agent-1")
    assert latest is not None
    assert latest[0].action_id == proposal.action_id
    assert latest[1].applied_action == result.applied_action
    assert latest[1].clamps == result.clamps
    assert len(result.clamps) >= 3
    assert len(store.get_applied_actions("run-agent-1")) == 1


def test_action_id_cannot_be_reused_with_changed_audit_metadata(
    store: SQLiteStore,
) -> None:
    _create_running_run(store)
    persisted = store.record_observation(observation_input())
    proposal = action(observation_id=persisted.observation_id)
    assert store.record_proposed_action(proposal)
    assert not store.record_proposed_action(proposal)
    with pytest.raises(DataConflictError, match="different values"):
        store.record_proposed_action(proposal.model_copy(update={"model": "different-model"}))


def test_store_rejects_wrong_run_stale_and_non_monotonic_actions(
    store: SQLiteStore,
) -> None:
    _create_running_run(store)
    first_observation = store.record_observation(observation_input())
    first_action = action(observation_id=first_observation.observation_id)
    first_validation = SafetyValidator().validate(
        first_action,
        SafetyContext(
            expected_run_id="run-agent-1",
            latest_observation=first_observation,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    assert store.apply_validated_action(
        first_action,
        first_validation,
        expected_run_id="run-agent-1",
    ).applied

    second_observation = store.record_observation(
        observation_input(
            timestep_key="ts-2",
            simulation_timestamp=NOW + timedelta(minutes=15),
        )
    )
    stale_action = action(
        action_id="stale-action",
        observation_id=first_observation.observation_id,
        action_generation=2,
    )
    stale_validation = ValidationResult(
        run_id=stale_action.run_id,
        observation_id=stale_action.observation_id,
        action_generation=stale_action.action_generation,
        timestamp=NOW,
        accepted=True,
        proposed_action=stale_action.action,
        applied_action=stale_action.action,
        applied_expires_at=stale_action.expires_at,
    )
    stale = store.apply_validated_action(
        stale_action,
        stale_validation,
        expected_run_id="run-agent-1",
    )
    assert stale.rejection_code is ValidationCode.STALE_OBSERVATION

    non_monotonic_action = action(
        action_id="non-monotonic",
        observation_id=second_observation.observation_id,
        action_generation=1,
    )
    non_monotonic_validation = stale_validation.model_copy(
        update={
            "observation_id": second_observation.observation_id,
            "action_generation": 1,
            "proposed_action": non_monotonic_action.action,
            "applied_action": non_monotonic_action.action,
        }
    )
    non_monotonic = store.apply_validated_action(
        non_monotonic_action,
        non_monotonic_validation,
        expected_run_id="run-agent-1",
    )
    assert non_monotonic.rejection_code is ValidationCode.NON_MONOTONIC_GENERATION

    wrong_run = store.apply_validated_action(
        non_monotonic_action,
        non_monotonic_validation,
        expected_run_id="another-run",
    )
    assert wrong_run.rejection_code is ValidationCode.WRONG_RUN


def test_store_refuses_to_apply_action_when_run_is_not_active(
    store: SQLiteStore,
) -> None:
    store.create_run("run-agent-1", RunType.AGENT)
    persisted = store.record_observation(observation_input())
    proposal = action(observation_id=persisted.observation_id)
    validation = SafetyValidator().validate(
        proposal,
        SafetyContext(
            expected_run_id="run-agent-1",
            latest_observation=persisted,
            constraints=constraints(),
            last_applied_generation=0,
            now=NOW,
        ),
    )
    result = store.apply_validated_action(
        proposal,
        validation,
        expected_run_id="run-agent-1",
    )
    assert not result.applied
    assert result.rejection_code is ValidationCode.RUN_NOT_ACTIVE


def test_tool_calls_errors_metrics_and_artifacts_round_trip(
    store: SQLiteStore,
    tmp_path: Path,
) -> None:
    _create_running_run(store)
    observation = store.record_observation(observation_input())
    trace = ToolCallTrace(
        call_id="call-1",
        run_id="run-agent-1",
        timestamp=NOW,
        observation_id=observation.observation_id,
        sequence=1,
        tool_name="get_constraints",
        arguments={"run_id": "run-agent-1"},
        result={"maximum_hold_minutes": 120},
        success=True,
        duration_ms=2.5,
    )
    assert store.record_tool_call(trace)
    assert not store.record_tool_call(trace)
    assert store.get_recent_tool_calls("run-agent-1") == [trace]
    with pytest.raises(DataConflictError, match="different audit data"):
        store.record_tool_call(trace.model_copy(update={"result": {"changed": True}}))

    first_hash = store.record_error(
        "run-agent-1",
        MessageSeverity.SEVERE,
        "eplusout.err",
        " ** Severe ** Missing actuator ",
        details={"line": 8},
    )
    second_hash = store.record_error(
        "run-agent-1",
        MessageSeverity.SEVERE,
        "eplusout.err",
        "** severe ** missing ACTUATOR",
        details={"line": 9},
    )
    assert first_hash == second_hash
    assert store.get_recent_errors("run-agent-1")[0]["occurrence_count"] == 2

    store.upsert_metric(
        "run-agent-1",
        "facility_electricity_kwh",
        value=100.0,
        units="kWh",
        source="eplusout.csv",
        verified=True,
    )
    assert (
        store.get_metrics("run-agent-1", verified_only=True)["facility_electricity_kwh"]["value"]
        == 100.0
    )
    with pytest.raises(ValueError, match="finite"):
        store.upsert_metric(
            "run-agent-1",
            "invalid",
            value=float("nan"),
            source="test",
            verified=False,
        )
    assert store.record_artifact(
        "run-agent-1",
        "energyplus_csv",
        tmp_path / "eplusout.csv",
        sha256="a" * 64,
        size_bytes=42,
    )
