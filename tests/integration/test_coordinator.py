"""Production coordinator integration over SQLite and genuine MCP stdio."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import ecoloop.coordinator as coordinator_module
from ecoloop.agent.models import ModelResponse, ToolRequest
from ecoloop.config import Settings
from ecoloop.coordinator import CoordinatorConfig, coordinate_simulation, run_case
from ecoloop.db.store import SQLiteStore
from ecoloop.energyplus.model import ModelArtifacts
from ecoloop.energyplus.runtime import (
    SimulationMode,
    SimulationRequest,
    SimulationResult,
)
from ecoloop.mcp.sqlite_service import SQLiteMCPService
from ecoloop.schemas import RunStatus, RunType
from tests.support.agent_fakes import ScriptedModel, valid_tool_sequence
from tests.unit._factories import observation_input


def _request(tmp_path: Path, mode: SimulationMode) -> SimulationRequest:
    return SimulationRequest(
        run_id="fake-run",
        model_path=tmp_path / "building.idf",
        weather_path=tmp_path / "weather.epw",
        output_directory=tmp_path / "output",
        database_path=tmp_path / "ecoloop.db",
        mode=mode,
        process_timeout_seconds=5.0,
        decision_wait_seconds=0.0,
        decision_poll_seconds=0.01,
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        ECOLOOP_DATABASE_PATH=tmp_path / "ecoloop.db",
        ECOLOOP_RUNS_DIR=tmp_path / "runs",
        LLM_TIMEOUT_SECONDS=0.25,
        ECOLOOP_MAX_TOOL_ROUNDS=4,
        ECOLOOP_MAX_CONSECUTIVE_TIMEOUTS=2,
    )


def _controlled_fake_runner(
    *,
    linger_seconds: float = 0.15,
    second_occupancy_change: bool = False,
    thread_ids: list[int] | None = None,
) -> Callable[[SimulationRequest], SimulationResult]:
    """Return an explicit fake runner that honors the SQLite action handshake."""

    def run(request: SimulationRequest) -> SimulationResult:
        if thread_ids is not None:
            thread_ids.append(threading.get_ident())
        store = SQLiteStore(request.database_path)
        if store.get_run(request.run_id) is None:
            store.create_run(
                request.run_id,
                RunType(request.mode.value),
                is_fake=True,
                energyplus_version="explicit-test-fake",
            )
        current_run = store.get_run(request.run_id)
        if current_run is not None and current_run.status is RunStatus.PENDING:
            store.set_run_status(request.run_id, RunStatus.RUNNING)
        wall_now = datetime.now(UTC)
        first = store.record_observation(
            observation_input(
                run_id=request.run_id,
                timestamp=wall_now,
                simulation_timestamp=wall_now,
            )
        )
        deadline = time.monotonic() + 4.0
        applied_count = 0
        while time.monotonic() < deadline:
            latest = store.get_last_applied_action(request.run_id)
            if latest is not None and latest[0].observation_id == first.observation_id:
                applied_count = 1
                break
            time.sleep(0.01)
        if second_occupancy_change:
            second_wall = datetime.now(UTC)
            store.record_observation(
                observation_input(
                    run_id=request.run_id,
                    timestamp=second_wall,
                    simulation_timestamp=wall_now + timedelta(minutes=15),
                    timestep_key="ts-2",
                    occupied=False,
                    occupancy_count=0.0,
                )
            )
            time.sleep(linger_seconds)
        else:
            time.sleep(linger_seconds)
        run = store.get_run(request.run_id)
        if run is not None and run.status is RunStatus.RUNNING:
            store.set_run_status(request.run_id, RunStatus.COMPLETED)
        return SimulationResult(
            run_id=request.run_id,
            status="completed",
            exit_code=0,
            output_directory=request.output_directory,
            elapsed_seconds=0.2,
            progress_percent=100,
            warning_count=0,
            severe_count=0,
            fatal_count=0,
            observation_count=2 if second_occupancy_change else 1,
            applied_action_count=applied_count,
        )

    return run


class ObservationAdvanceModel(ScriptedModel):
    """Wait for a newer observation, then target that current MCP state."""

    def __init__(
        self,
        decision_started: threading.Event,
        newer_observation_ready: threading.Event,
    ) -> None:
        super().__init__([valid_tool_sequence()])
        self._decision_started = decision_started
        self._newer_observation_ready = newer_observation_ready

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del messages, tools
        call_number = self.call_count + 1
        self.call_count = call_number
        if call_number == 1:
            self._decision_started.set()
            ready = await asyncio.to_thread(self._newer_observation_ready.wait, 2.0)
            if not ready:
                raise RuntimeError("newer observation was not published during the decision")
        response = valid_tool_sequence()
        calls = list(response.tool_calls)
        calls[-1] = ToolRequest(
            name="apply_control_action",
            arguments={
                "run_id": "fake-run",
                "observation_id": 2,
                "action": {
                    "heating_setpoint_c": 20.0,
                    "cooling_setpoint_c": 25.0,
                    "hold_minutes": 60,
                    "action_generation": call_number,
                    "reason_code": "COMFORT_PROTECTION",
                    "explanation": "Use the current observation's bounded candidate.",
                },
            },
        )
        return response.model_copy(update={"tool_calls": calls})


class TerminalBoundaryModel(ScriptedModel):
    """Block inference until the coordinator cancels it at simulation terminal."""

    def __init__(
        self,
        decision_started: threading.Event,
        decision_cancelled: threading.Event,
    ) -> None:
        super().__init__([valid_tool_sequence()])
        self._decision_started = decision_started
        self._decision_cancelled = decision_cancelled

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del messages, tools
        self.call_count += 1
        self._decision_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self._decision_cancelled.set()
            raise


class TerminalCompletionModel(ScriptedModel):
    """Return a valid decision only after durable run status is terminal."""

    def __init__(
        self,
        decision_started: threading.Event,
        run_marked_terminal: threading.Event,
    ) -> None:
        super().__init__([valid_tool_sequence()])
        self._decision_started = decision_started
        self._run_marked_terminal = run_marked_terminal

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        del messages, tools
        self.call_count += 1
        self._decision_started.set()
        terminal = await asyncio.to_thread(self._run_marked_terminal.wait, 2.0)
        if not terminal:
            raise RuntimeError("run did not reach terminal status during the decision")
        return valid_tool_sequence()


def _runner_advancing_observation_during_decision(
    decision_started: threading.Event,
    newer_observation_ready: threading.Event,
) -> Callable[[SimulationRequest], SimulationResult]:
    """Publish observation 2 only after observation 1 has triggered a decision."""

    def run(request: SimulationRequest) -> SimulationResult:
        store = SQLiteStore(request.database_path)
        store.create_run(
            request.run_id,
            RunType.AGENT,
            is_fake=True,
            energyplus_version="explicit-test-fake",
        )
        store.set_run_status(request.run_id, RunStatus.RUNNING)
        wall_now = datetime.now(UTC)
        store.record_observation(
            observation_input(
                run_id=request.run_id,
                timestamp=wall_now,
                simulation_timestamp=wall_now,
            )
        )
        if not decision_started.wait(2.0):
            raise RuntimeError("coordinator did not start the first observation decision")
        second = store.record_observation(
            observation_input(
                run_id=request.run_id,
                timestamp=datetime.now(UTC),
                simulation_timestamp=wall_now + timedelta(minutes=15),
                timestep_key="race-step-2",
                occupied=False,
                occupancy_count=0.0,
            )
        )
        newer_observation_ready.set()

        deadline = time.monotonic() + 3.0
        applied_count = 0
        while time.monotonic() < deadline:
            applied = store.get_applied_actions(request.run_id)
            if applied and applied[-1][0].observation_id == second.observation_id:
                applied_count = len(applied)
                break
            time.sleep(0.01)
        time.sleep(0.3)
        run_record = store.get_run(request.run_id)
        if run_record is not None and run_record.status is RunStatus.RUNNING:
            store.set_run_status(request.run_id, RunStatus.COMPLETED)
        return SimulationResult(
            run_id=request.run_id,
            status="completed",
            exit_code=0,
            output_directory=request.output_directory,
            elapsed_seconds=0.4,
            progress_percent=100,
            warning_count=0,
            severe_count=0,
            fatal_count=0,
            observation_count=2,
            applied_action_count=applied_count,
        )

    return run


def _runner_completing_with_queued_observation(
    decision_started: threading.Event,
) -> Callable[[SimulationRequest], SimulationResult]:
    """Finish while observation one is deciding and observation two is queued."""

    def run(request: SimulationRequest) -> SimulationResult:
        store = SQLiteStore(request.database_path)
        store.create_run(
            request.run_id,
            RunType.AGENT,
            is_fake=True,
            energyplus_version="explicit-test-fake",
        )
        store.set_run_status(request.run_id, RunStatus.RUNNING)
        wall_now = datetime.now(UTC)
        store.record_observation(
            observation_input(
                run_id=request.run_id,
                timestamp=wall_now,
                simulation_timestamp=wall_now,
            )
        )
        if not decision_started.wait(2.0):
            raise RuntimeError("coordinator did not start the terminal-boundary decision")
        store.record_observation(
            observation_input(
                run_id=request.run_id,
                timestamp=datetime.now(UTC),
                simulation_timestamp=wall_now + timedelta(minutes=15),
                timestep_key="terminal-queued-step",
                occupied=False,
                occupancy_count=0.0,
            )
        )
        store.set_run_status(request.run_id, RunStatus.COMPLETED)
        return SimulationResult(
            run_id=request.run_id,
            status="completed",
            exit_code=0,
            output_directory=request.output_directory,
            elapsed_seconds=0.1,
            progress_percent=100,
            warning_count=0,
            severe_count=0,
            fatal_count=0,
            observation_count=2,
            applied_action_count=0,
        )

    return run


def _runner_marks_terminal_before_return(
    decision_started: threading.Event,
    run_marked_terminal: threading.Event,
) -> Callable[[SimulationRequest], SimulationResult]:
    """Mark the run complete while allowing a late model response to finish."""

    def run(request: SimulationRequest) -> SimulationResult:
        store = SQLiteStore(request.database_path)
        store.create_run(
            request.run_id,
            RunType.AGENT,
            is_fake=True,
            energyplus_version="explicit-test-fake",
        )
        store.set_run_status(request.run_id, RunStatus.RUNNING)
        wall_now = datetime.now(UTC)
        store.record_observation(
            observation_input(
                run_id=request.run_id,
                timestamp=wall_now,
                simulation_timestamp=wall_now,
            )
        )
        if not decision_started.wait(2.0):
            raise RuntimeError("coordinator did not start the late terminal decision")
        store.set_run_status(request.run_id, RunStatus.COMPLETED)
        run_marked_terminal.set()
        time.sleep(0.2)
        return SimulationResult(
            run_id=request.run_id,
            status="completed",
            exit_code=0,
            output_directory=request.output_directory,
            elapsed_seconds=0.2,
            progress_percent=100,
            warning_count=0,
            severe_count=0,
            fatal_count=0,
            observation_count=1,
            applied_action_count=0,
        )

    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent_coordinator_runs_threaded_simulation_and_genuine_mcp(
    tmp_path: Path,
) -> None:
    main_thread = threading.get_ident()
    runner_threads: list[int] = []
    request = _request(tmp_path, SimulationMode.AGENT)
    result = await coordinate_simulation(
        request,
        settings=_settings(tmp_path),
        config=CoordinatorConfig(
            observation_poll_seconds=0.01,
            overall_timeout_seconds=6.0,
            decision_timeout_seconds=3.0,
            terminal_settle_seconds=0.0,
        ),
        runner=_controlled_fake_runner(thread_ids=runner_threads),
        model_backend=ScriptedModel([valid_tool_sequence()]),
    )

    assert result.completed
    assert runner_threads and runner_threads[0] != main_thread
    assert len(result.decisions) == 1
    assert result.decisions[0].status == "applied"
    assert result.decisions[0].triggers[0].value == "first_observation"
    store = SQLiteStore(request.database_path)
    applied = store.get_applied_actions(request.run_id)
    assert len(applied) == 1
    assert applied[0][0].observation_id == 1
    tool_names = [trace.tool_name for trace in store.get_recent_tool_calls(request.run_id)]
    assert tool_names == [
        "get_current_building_state",
        "get_constraints",
        "generate_candidate_actions",
        "apply_control_action",
    ]
    connection = sqlite3.connect(store.path)
    try:
        decision_count = int(
            connection.execute("SELECT COUNT(*) FROM agent_decisions").fetchone()[0]
        )
    finally:
        connection.close()
    assert decision_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coordinator_marks_newer_terminal_observation_as_decided(
    tmp_path: Path,
) -> None:
    decision_started = threading.Event()
    newer_observation_ready = threading.Event()
    model = ObservationAdvanceModel(decision_started, newer_observation_ready)
    request = _request(tmp_path, SimulationMode.AGENT)

    result = await coordinate_simulation(
        request,
        settings=_settings(tmp_path),
        config=CoordinatorConfig(
            observation_poll_seconds=0.01,
            overall_timeout_seconds=6.0,
            decision_timeout_seconds=3.0,
            terminal_settle_seconds=0.0,
        ),
        runner=_runner_advancing_observation_during_decision(
            decision_started,
            newer_observation_ready,
        ),
        model_backend=model,
    )

    assert result.completed
    assert result.observations_seen == 2
    assert result.last_observation_id == 2
    assert len(result.decisions) == 1
    assert result.decisions[0].observation_id == 1
    assert model.call_count == 1
    store = SQLiteStore(request.database_path)
    applied = store.get_applied_actions(request.run_id)
    assert len(applied) == 1
    assert applied[0][0].observation_id == 2
    connection = sqlite3.connect(store.path)
    try:
        durable_observations = [
            int(row[0])
            for row in connection.execute(
                "SELECT observation_id FROM agent_decisions ORDER BY timestamp"
            )
        ]
    finally:
        connection.close()
    assert durable_observations == [2]
    assert store.get_recent_errors(request.run_id) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terminal_simulation_cancels_inflight_and_queued_decisions(
    tmp_path: Path,
) -> None:
    decision_started = threading.Event()
    decision_cancelled = threading.Event()
    model = TerminalBoundaryModel(decision_started, decision_cancelled)
    request = _request(tmp_path, SimulationMode.AGENT)

    result = await coordinate_simulation(
        request,
        settings=_settings(tmp_path),
        config=CoordinatorConfig(
            observation_poll_seconds=0.01,
            overall_timeout_seconds=6.0,
            decision_timeout_seconds=3.0,
            terminal_settle_seconds=0.0,
        ),
        runner=_runner_completing_with_queued_observation(decision_started),
        model_backend=model,
    )

    assert result.completed
    assert result.decisions == ()
    assert result.last_observation_id == 2
    assert model.call_count == 1
    assert decision_cancelled.is_set()
    store = SQLiteStore(request.database_path)
    assert store.get_applied_actions(request.run_id) == []
    assert store.get_recent_errors(request.run_id) == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposed_actions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_late_terminal_decision_cannot_apply_or_invoke_fallback(
    tmp_path: Path,
) -> None:
    decision_started = threading.Event()
    run_marked_terminal = threading.Event()
    model = TerminalCompletionModel(decision_started, run_marked_terminal)
    request = _request(tmp_path, SimulationMode.AGENT)

    result = await coordinate_simulation(
        request,
        settings=_settings(tmp_path),
        config=CoordinatorConfig(
            observation_poll_seconds=0.01,
            overall_timeout_seconds=6.0,
            decision_timeout_seconds=3.0,
            terminal_settle_seconds=0.0,
        ),
        runner=_runner_marks_terminal_before_return(
            decision_started,
            run_marked_terminal,
        ),
        model_backend=model,
    )

    assert result.completed
    assert result.decisions == ()
    assert model.call_count == 1
    store = SQLiteStore(request.database_path)
    assert store.get_applied_actions(request.run_id) == []
    assert store.get_recent_errors(request.run_id) == []
    with sqlite3.connect(store.path) as connection:
        tool_names = [
            str(row[0])
            for row in connection.execute("SELECT tool_name FROM tool_calls ORDER BY timestamp")
        ]
        assert "apply_control_action" not in tool_names
        assert "request_safe_fallback" not in tool_names
        assert connection.execute("SELECT COUNT(*) FROM proposed_actions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_decisions").fetchone()[0] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rule_coordinator_uses_deterministic_service_without_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_model(**kwargs: object) -> object:
        del kwargs
        raise AssertionError("rule mode must not construct an Ollama backend")

    monkeypatch.setattr(coordinator_module, "OllamaModelBackend", forbidden_model)
    request = _request(tmp_path, SimulationMode.RULE)
    result = await coordinate_simulation(
        request,
        settings=_settings(tmp_path),
        config=CoordinatorConfig(
            observation_poll_seconds=0.01,
            overall_timeout_seconds=6.0,
            terminal_settle_seconds=0.0,
        ),
        runner=_controlled_fake_runner(),
    )

    assert result.completed
    assert len(result.decisions) == 1
    assert result.decisions[0].status == "fallback"
    assert result.decisions[0].fallback is True
    store = SQLiteStore(request.database_path)
    applied = store.get_applied_actions(request.run_id)
    assert len(applied) == 1
    assert applied[0][0].model == "deterministic-fallback"
    assert store.get_recent_tool_calls(request.run_id) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coordinator_deduplicates_observations_and_enforces_decision_limit(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, SimulationMode.RULE)
    result = await coordinate_simulation(
        request,
        settings=_settings(tmp_path),
        config=CoordinatorConfig(
            observation_poll_seconds=0.01,
            overall_timeout_seconds=6.0,
            maximum_decisions=1,
            terminal_settle_seconds=0.0,
        ),
        runner=_controlled_fake_runner(
            linger_seconds=0.25,
            second_occupancy_change=True,
        ),
    )

    assert result.completed
    assert result.observations_seen == 2
    assert result.last_observation_id == 2
    assert len(result.decisions) == 1
    assert result.skipped_due_to_limit == 1
    assert len(SQLiteStore(request.database_path).get_applied_actions("fake-run")) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coordinator_rejects_non_supervisory_mode(tmp_path: Path) -> None:
    request = _request(tmp_path, SimulationMode.BASELINE)
    with pytest.raises(ValueError, match="only agent and rule"):
        await coordinate_simulation(
            request,
            settings=_settings(tmp_path),
            runner=_controlled_fake_runner(),
        )


@pytest.mark.integration
def test_public_fake_run_records_parent_baseline_and_aligned_reference(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    baseline = run_case(
        "baseline",
        period_name="smoke",
        settings=settings,
        fake=True,
    )
    controlled = run_case(
        "rule",
        period_name="smoke",
        settings=settings,
        fake=True,
    )

    assert baseline["status"] == "completed"
    assert baseline["is_fake"] is True
    assert baseline["data_status"] == "EXPLICIT_FAKE_TEST_DATA"
    assert controlled["status"] == "completed"
    assert controlled["parent_run_id"] == baseline["run_id"]
    assert controlled["coordinator"]["decision_count"] == 2

    store = SQLiteStore(settings.resolved_database_path())
    run = store.get_run(str(controlled["run_id"]))
    assert run is not None
    assert run.is_fake is True
    assert run.parent_run_id == baseline["run_id"]
    state = asyncio.run(
        SQLiteMCPService(store=store, settings=settings).get_current_building_state(run.run_id)
    )
    reference = state["observation"]["baseline_reference"]
    assert reference is not None
    assert reference["run_id"] == baseline["run_id"]
    assert reference["simulation_timestamp"] == state["observation"]["simulation_timestamp"]


@pytest.mark.integration
def test_real_run_prepares_selected_period_and_snapshots_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    source = prepared / "source.idf"
    baseline_model = prepared / "baseline.idf"
    agent_model = prepared / "agent_ready.idf"
    replay_model = prepared / "agent_replay.idf"
    actuator_map = prepared / "actuator_map.csv"
    schedule = prepared / "action_schedule.csv"
    provenance = prepared / "PROVENANCE.json"
    manifest = prepared / "preparation-manifest.json"
    for path, value in (
        (source, "source"),
        (baseline_model, "baseline-period-model"),
        (agent_model, "agent-period-model"),
        (replay_model, "replay-period-model"),
        (actuator_map, "logical_metric,component_type\n"),
        (schedule, "simulation_timestamp\n"),
        (provenance, "{}"),
        (manifest, '{"period":"smoke"}'),
    ):
        path.write_text(value, encoding="utf-8")
    weather = tmp_path / "weather.epw"
    weather.write_text("explicit test weather", encoding="utf-8")
    settings = Settings(
        ECOLOOP_DATABASE_PATH=tmp_path / "ecoloop.db",
        ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ECOLOOP_WEATHER_PATH=weather,
    )
    preparation_calls: list[object] = []
    preparation_outputs: list[Path | None] = []
    captured_requests: list[SimulationRequest] = []

    def fake_prepare(
        supplied_settings: object,
        period: object,
        source_override: Path | None = None,
        *,
        output_directory: Path | None = None,
    ) -> ModelArtifacts:
        del supplied_settings, source_override
        preparation_calls.append(period)
        preparation_outputs.append(output_directory)
        return ModelArtifacts(
            source_model=source,
            baseline_model=baseline_model,
            agent_ready_model=agent_model,
            agent_replay_model=replay_model,
            action_schedule=schedule,
            actuator_map=actuator_map,
            provenance=provenance,
            preparation_manifest=manifest,
        )

    def stopped_runner(request: SimulationRequest) -> SimulationResult:
        captured_requests.append(request)
        store = SQLiteStore(request.database_path)
        store.set_run_status(request.run_id, RunStatus.RUNNING)
        store.set_run_status(
            request.run_id,
            RunStatus.FAILED,
            error_summary="explicit test stopped before execution",
        )
        return SimulationResult(
            run_id=request.run_id,
            status="failed",
            exit_code=1,
            output_directory=request.output_directory,
            elapsed_seconds=0.0,
            progress_percent=0,
            warning_count=0,
            severe_count=0,
            fatal_count=0,
            observation_count=0,
            applied_action_count=0,
            error="explicit test stopped before execution",
        )

    monkeypatch.setattr(coordinator_module, "prepare_models", fake_prepare)
    monkeypatch.setattr(coordinator_module, "run_simulation", stopped_runner)
    result = run_case("baseline", period_name="smoke", settings=settings)

    assert result["status"] == "failed"
    assert len(preparation_calls) == 1
    assert preparation_outputs[0] is not None
    assert preparation_outputs[0].parent.name.startswith("baseline-")
    assert preparation_outputs[0].name == "prepared"
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.model_path.parent.name == "inputs"
    assert request.model_path.read_text(encoding="utf-8") == "baseline-period-model"
    assert request.process_timeout_seconds == settings.simulation_timeout_seconds
    assert (request.model_path.parent / "actuator_map.csv").is_file()
    run = SQLiteStore(settings.resolved_database_path()).get_run(request.run_id)
    assert run is not None
    assert len(str(run.metadata["preparation_fingerprint"])) == 64
    assert len(str(run.metadata["weather_sha256"])) == 64
    assert run.metadata["simulation_timeout_seconds"] == settings.simulation_timeout_seconds
