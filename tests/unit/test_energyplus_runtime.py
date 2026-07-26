from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoloop.energyplus.handles import HandleRegistry
from ecoloop.energyplus.runtime import (
    EnergyPlusSession,
    ReplaySchedule,
    RuntimeAction,
    SimulationMode,
    SimulationRequest,
    bind_action_to_simulation_clock,
    energyplus_clock_to_datetime,
    load_fanger_people_zone_map,
    make_timestep_identity,
    validate_runtime_action,
)


class _FakeStore:
    def __init__(self, action_after: int | None) -> None:
        self.action_after = action_after
        self.polls = 0
        self.messages: list[tuple[str, str]] = []

    def latest_applicable_action(
        self,
        run_id: str,
        observation_id: int,
    ) -> dict[str, object] | None:
        self.polls += 1
        if self.action_after is None or self.polls < self.action_after:
            return None
        return {
            "run_id": run_id,
            "observation_id": observation_id,
            "action_generation": 1,
            "applied_heating_setpoint_c": 20.0,
            "applied_cooling_setpoint_c": 24.0,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "hold_minutes": 60,
            "validation_result": "valid",
        }

    def record_simulation_message(
        self,
        run_id: str,
        timestamp: str,
        severity: str,
        digest: str,
        message: str,
    ) -> None:
        self.messages.append((severity, message))


class _FakeAPI:
    exchange = object()


class _FakeClockExchange:
    def current_environment_num(self, state: object) -> int:
        del state
        return 3

    def year(self, state: object) -> int:
        del state
        return 2014

    def month(self, state: object) -> int:
        del state
        return 7

    def day_of_month(self, state: object) -> int:
        del state
        return 15

    def hour(self, state: object) -> int:
        del state
        return 8

    def zone_time_step_number(self, state: object) -> int:
        del state
        return 2

    def num_time_steps_in_hour(self, state: object) -> int:
        del state
        return 4


def test_energyplus_clock_uses_end_of_timestep_semantics() -> None:
    assert energyplus_clock_to_datetime(2014, 7, 15, 0, 15) == datetime(  # noqa: DTZ001
        2014, 7, 15, 0, 15
    )
    assert energyplus_clock_to_datetime(2014, 7, 15, 23, 60) == datetime(  # noqa: DTZ001
        2014, 7, 16, 0, 0
    )


def test_runtime_rejects_wrong_run_stale_expired_and_duplicate_actions() -> None:
    now = datetime(2014, 7, 15, 12, 0)  # noqa: DTZ001
    valid = RuntimeAction(
        run_id="run-1",
        observation_id=10,
        action_generation=3,
        heating_setpoint_c=20.0,
        cooling_setpoint_c=24.0,
        expires_at=datetime(2014, 7, 15, 13, 0),  # noqa: DTZ001
        wall_expires_at=datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
        validation_result="valid",
    )
    assert validate_runtime_action(
        valid,
        run_id="run-1",
        expected_observation_id=10,
        last_generation=2,
        simulation_time=now,
        wall_time=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    ) == (True, "accepted")
    assert (
        validate_runtime_action(
            valid,
            run_id="other",
            expected_observation_id=10,
            last_generation=2,
            simulation_time=now,
            wall_time=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        )[1]
        == "wrong_run"
    )
    assert (
        validate_runtime_action(
            valid,
            run_id="run-1",
            expected_observation_id=11,
            last_generation=2,
            simulation_time=now,
            wall_time=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        )[1]
        == "stale_observation"
    )
    assert (
        validate_runtime_action(
            valid,
            run_id="run-1",
            expected_observation_id=10,
            last_generation=3,
            simulation_time=now,
            wall_time=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        )[1]
        == "duplicate_or_non_monotonic_generation"
    )
    assert (
        validate_runtime_action(
            valid,
            run_id="run-1",
            expected_observation_id=10,
            last_generation=2,
            simulation_time=datetime(2014, 7, 15, 14, 0),  # noqa: DTZ001
            wall_time=datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        )[1]
        == "expired"
    )


def test_action_hold_expires_on_simulation_clock() -> None:
    action = RuntimeAction(
        run_id="run-1",
        observation_id=1,
        action_generation=1,
        heating_setpoint_c=20.0,
        cooling_setpoint_c=24.0,
        expires_at=None,
        wall_expires_at=datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
        validation_result="valid",
        hold_minutes=60,
    )
    start = datetime(2014, 7, 15, 8, 0)  # noqa: DTZ001
    active = bind_action_to_simulation_clock(action, start, 120)
    assert active.expires_at == datetime(2014, 7, 15, 9, 0)  # noqa: DTZ001


def test_replay_schedule_normalizes_aware_timestamp_to_energyplus_clock(
    tmp_path: Path,
) -> None:
    schedule = tmp_path / "action_schedule.csv"
    schedule.write_text(
        "simulation_timestamp,observation_id,action_generation,"
        "heating_setpoint_c,cooling_setpoint_c,hold_minutes\n"
        "2014-07-15T08:00:00+00:00,1,1,20.0,24.0,60\n",
        encoding="utf-8",
    )

    replay = ReplaySchedule(schedule, "replay-run")
    action = replay.action_at(datetime(2014, 7, 15, 8, 30))  # noqa: DTZ001

    assert action is not None
    assert action.heating_setpoint_c == 20.0
    assert action.cooling_setpoint_c == 24.0
    assert action.expires_at == datetime(2014, 7, 15, 9, 0)  # noqa: DTZ001


def _agent_session(store: _FakeStore, wait_seconds: float) -> EnergyPlusSession:
    request = SimulationRequest(
        run_id="run-1",
        model_path=Path("model.idf"),
        weather_path=Path("weather.epw"),
        output_directory=Path("output"),
        database_path=Path("database.db"),
        mode=SimulationMode.AGENT,
        decision_wait_seconds=wait_seconds,
        decision_poll_seconds=0.01,
    )
    session = EnergyPlusSession(
        request,
        _FakeAPI(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        HandleRegistry(()),
    )
    session.last_observation_id = 5
    return session


def test_bounded_decision_wait_accepts_only_exact_observation_action() -> None:
    store = _FakeStore(action_after=2)
    session = _agent_session(store, 0.1)
    session._wait_for_action(datetime(2014, 7, 15, 8, 0))  # noqa: DTZ001
    assert session._pending_action is not None
    assert session._pending_action.observation_id == 5
    assert any("received action" in message for _, message in store.messages)


def test_bounded_decision_wait_times_out_without_blocking_forever() -> None:
    store = _FakeStore(action_after=None)
    session = _agent_session(store, 0.03)
    session._wait_for_action(datetime(2014, 7, 15, 8, 0))  # noqa: DTZ001
    assert session._pending_action is None
    assert store.polls >= 1
    assert any("timed out" in message for _, message in store.messages)


def test_display_delay_preserves_timestep_identity_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(
        "ecoloop.energyplus.runtime.time.sleep",
        lambda seconds: slept.append(seconds),
    )
    request = SimulationRequest(
        run_id="run-1",
        model_path=Path("model.idf"),
        weather_path=Path("weather.epw"),
        output_directory=Path("output"),
        database_path=Path("database.db"),
        display_delay_seconds=0.25,
    )
    session = EnergyPlusSession(
        request,
        _FakeAPI(),  # type: ignore[arg-type]
        _FakeStore(action_after=None),  # type: ignore[arg-type]
        HandleRegistry(()),
    )
    exchange = _FakeClockExchange()
    identity_before, timestamp_before = make_timestep_identity(
        request.run_id,
        exchange,  # type: ignore[arg-type]
        object(),
    )
    payload = {"temperature_c": 24.0, "energy_kwh": 1.25}
    expected_payload = dict(payload)

    if session.request.display_delay_seconds:
        import time

        time.sleep(session.request.display_delay_seconds)

    identity_after, timestamp_after = make_timestep_identity(
        request.run_id,
        exchange,  # type: ignore[arg-type]
        object(),
    )
    assert slept == [0.25]
    assert identity_after == identity_before
    assert timestamp_after == timestamp_before
    assert payload == expected_payload


def test_action_expiry_does_not_trigger_a_decision_before_expiry() -> None:
    session = _agent_session(_FakeStore(action_after=None), 0.1)
    session.request = SimulationRequest(
        run_id="run-1",
        model_path=Path("model.idf"),
        weather_path=Path("weather.epw"),
        output_directory=Path("output"),
        database_path=Path("database.db"),
        mode=SimulationMode.AGENT,
        decision_wait_seconds=0.1,
        decision_interval_minutes=120,
    )
    session._last_decision_simulation_time = datetime(2014, 7, 15, 8, 0)  # noqa: DTZ001
    session._previous_observation_occupied = False
    session.active_action = RuntimeAction(
        run_id="run-1",
        observation_id=1,
        action_generation=1,
        heating_setpoint_c=20.0,
        cooling_setpoint_c=24.0,
        expires_at=datetime(2014, 7, 15, 9, 0),  # noqa: DTZ001
        validation_result="valid",
    )
    observation = {"occupied": False, "outdoor_drybulb_c": 30.0}

    assert not session._decision_due(
        datetime(2014, 7, 15, 8, 15),  # noqa: DTZ001
        observation,
    )
    session._action_expired_since_last_observation = True
    assert session._decision_due(
        datetime(2014, 7, 15, 9, 0),  # noqa: DTZ001
        observation,
    )


def test_runtime_level_trigger_is_edge_based_between_hourly_decisions() -> None:
    session = _agent_session(_FakeStore(action_after=None), 0.1)
    session.request = SimulationRequest(
        run_id="run-1",
        model_path=Path("model.idf"),
        weather_path=Path("weather.epw"),
        output_directory=Path("output"),
        database_path=Path("database.db"),
        mode=SimulationMode.AGENT,
        decision_wait_seconds=0.1,
        decision_interval_minutes=60,
    )
    session._last_decision_simulation_time = datetime(2014, 7, 15, 8, 0)  # noqa: DTZ001
    session._previous_observation_occupied = True
    session._previous_observation_outdoor_c = 30.0
    session._previous_comfort_risk = True
    session._previous_pmv_risk = True
    session._previous_co2_risk = True
    session._previous_demand_risk = True
    persistent = {
        "occupied": True,
        "outdoor_drybulb_c": 30.0,
        "operative_temperature": {"min": 22.4, "max": 24.0},
        "pmv": {"min": 0.65, "max": 0.65},
        "co2": {"max": 950.0},
        "facility_demand_w": 80_000.0,
    }
    assert not session._decision_due(
        datetime(2014, 7, 15, 8, 15),  # noqa: DTZ001
        persistent,
    )
    safe = {
        "occupied": True,
        "outdoor_drybulb_c": 30.0,
        "operative_temperature": {"min": 23.0, "max": 24.0},
        "pmv": {"min": 0.2, "max": 0.2},
        "co2": {"max": 700.0},
        "facility_demand_w": 50_000.0,
    }
    assert not session._decision_due(
        datetime(2014, 7, 15, 8, 30),  # noqa: DTZ001
        safe,
    )
    assert session._decision_due(
        datetime(2014, 7, 15, 8, 45),  # noqa: DTZ001
        persistent,
    )


def test_fanger_people_keys_are_loaded_from_preparation_manifest(
    tmp_path: Path,
) -> None:
    manifest = {
        "baseline": {
            "fanger_people_zone_map": {
                "Office People": "Office Zone",
            }
        }
    }
    (tmp_path / "preparation-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    mapping = load_fanger_people_zone_map(tmp_path / "baseline.idf")
    assert mapping == {"office people": "Office Zone"}
