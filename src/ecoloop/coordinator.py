"""Concurrent supervisory coordination for controlled EnergyPlus runs."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters

from ecoloop import ENERGYPLUS_VERSION
from ecoloop.agent.audit import SQLiteDecisionSink
from ecoloop.agent.client import MCPStdioClient, default_server_parameters
from ecoloop.agent.loop import AgentHost, AgentHostConfig, AgentProtocolError
from ecoloop.agent.ollama_host import ModelBackend, OllamaModelBackend
from ecoloop.config import Settings, load_file_config, repository_root
from ecoloop.control.cadence import DecisionCadence, DecisionTrigger
from ecoloop.db.store import SQLiteStore
from ecoloop.energyplus.model import prepare_models
from ecoloop.energyplus.runtime import (
    SimulationMode,
    SimulationRequest,
    SimulationResult,
    run_simulation,
)
from ecoloop.evaluation import EvaluationError, load_verified_final_metrics
from ecoloop.exceptions import RunStateError
from ecoloop.mcp.sqlite_service import SQLiteMCPService
from ecoloop.schemas import (
    ActuatorCapabilities,
    BuildingObservation,
    ControlConstraints,
    MessageSeverity,
    ObservationInput,
    RunRecord,
    RunStatus,
    RunType,
)

SimulationRunner = Callable[[SimulationRequest], SimulationResult]


class CoordinatorError(RuntimeError):
    """Base class for bounded supervisory coordination failures."""


class CoordinatorTimeoutError(CoordinatorError):
    """Raised when the outer coordination deadline is exceeded."""


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    """Polling and failure bounds independent of simulated time."""

    observation_poll_seconds: float = 0.05
    overall_timeout_seconds: float | None = None
    decision_timeout_seconds: float | None = None
    maximum_decisions: int = 2_000
    maximum_consecutive_decision_failures: int = 3
    rule_decision_wait_seconds: float = 5.0
    terminal_settle_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not 0.01 <= self.observation_poll_seconds <= 5.0:
            raise ValueError("observation_poll_seconds must be in 0.01..5")
        if self.overall_timeout_seconds is not None and self.overall_timeout_seconds <= 0:
            raise ValueError("overall_timeout_seconds must be positive")
        if self.decision_timeout_seconds is not None and self.decision_timeout_seconds <= 0:
            raise ValueError("decision_timeout_seconds must be positive")
        if self.maximum_decisions < 1:
            raise ValueError("maximum_decisions must be positive")
        if self.maximum_consecutive_decision_failures < 1:
            raise ValueError("maximum_consecutive_decision_failures must be positive")
        if not 0 <= self.rule_decision_wait_seconds <= 300:
            raise ValueError("rule_decision_wait_seconds must be in 0..300")
        if not 0 <= self.terminal_settle_seconds <= 10:
            raise ValueError("terminal_settle_seconds must be in 0..10")


@dataclass(frozen=True, slots=True)
class CoordinatedDecision:
    """One observation-level coordinator outcome."""

    observation_id: int
    simulation_timestamp: datetime
    triggers: tuple[DecisionTrigger, ...]
    status: str
    fallback: bool
    latency_ms: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    """EnergyPlus result plus supervisory reliability accounting."""

    simulation: SimulationResult
    decisions: tuple[CoordinatedDecision, ...]
    observations_seen: int
    last_observation_id: int
    skipped_due_to_limit: int
    consecutive_failure_peak: int
    circuit_breaker_open: bool

    @property
    def completed(self) -> bool:
        """Return whether the underlying real simulation completed cleanly."""

        return self.simulation.completed

    @property
    def successful_decisions(self) -> int:
        """Count applied or deterministic-fallback outcomes."""

        return sum(item.status in {"applied", "fallback"} for item in self.decisions)


async def coordinate_simulation(
    request: SimulationRequest,
    *,
    settings: Settings | None = None,
    config: CoordinatorConfig | None = None,
    cadence: DecisionCadence | None = None,
    runner: SimulationRunner = run_simulation,
    mcp_parameters: StdioServerParameters | None = None,
    model_backend: ModelBackend | None = None,
) -> CoordinatorResult:
    """Run EnergyPlus in a worker thread while servicing due observations.

    Agent mode always traverses an official MCP stdio client/server connection.
    Rule mode invokes only the deterministic safety/fallback application service
    and never constructs or calls a model backend.
    """

    if request.mode not in {SimulationMode.AGENT, SimulationMode.RULE}:
        raise ValueError("coordinator supports only agent and rule simulation modes")
    if request.mode is SimulationMode.RULE and (
        model_backend is not None or mcp_parameters is not None
    ):
        raise ValueError("rule mode does not accept a model backend or MCP parameters")

    runtime_settings = settings or Settings()
    coordinator_config = config or CoordinatorConfig()
    decision_cadence = cadence or DecisionCadence(
        normal_interval_minutes=request.decision_interval_minutes
    )
    store = SQLiteStore(request.database_path)
    service = SQLiteMCPService(store=store, settings=runtime_settings)
    decision_timeout = _decision_timeout(runtime_settings, coordinator_config)
    effective_request = _request_with_bounded_wait(
        request,
        coordinator_config,
        decision_timeout=decision_timeout,
    )

    if request.mode is SimulationMode.RULE:
        return await _run_loop(
            effective_request,
            store=store,
            service=service,
            cadence=decision_cadence,
            config=coordinator_config,
            runner=runner,
            agent_host=None,
            decision_timeout=decision_timeout,
        )

    parameters = mcp_parameters or default_server_parameters(database_path=request.database_path)
    backend = model_backend or OllamaModelBackend(
        host=runtime_settings.ollama_host,
        model=runtime_settings.ollama_model,
        timeout_seconds=runtime_settings.llm_timeout_seconds,
        keep_alive=runtime_settings.ollama_keep_alive,
    )
    async with MCPStdioClient(parameters) as client:
        host = AgentHost(
            mcp_client=client,
            model=backend,
            config=AgentHostConfig(
                timeout_seconds=runtime_settings.llm_timeout_seconds,
                maximum_tool_rounds=runtime_settings.max_tool_rounds,
                maximum_consecutive_failures=(runtime_settings.max_consecutive_timeouts),
                state_token_budget=runtime_settings.state_token_budget,
            ),
            decision_sink=SQLiteDecisionSink(store),
            run_is_active=lambda run_id: _run_is_active(store, run_id),
        )
        return await _run_loop(
            effective_request,
            store=store,
            service=service,
            cadence=decision_cadence,
            config=coordinator_config,
            runner=runner,
            agent_host=host,
            decision_timeout=decision_timeout,
        )


def run_supervisory_simulation(
    request: SimulationRequest,
    *,
    settings: Settings | None = None,
    config: CoordinatorConfig | None = None,
    cadence: DecisionCadence | None = None,
) -> CoordinatorResult:
    """Synchronous CLI-friendly wrapper around :func:`coordinate_simulation`."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            coordinate_simulation(
                request,
                settings=settings,
                config=config,
                cadence=cadence,
            )
        )
    raise RuntimeError(
        "run_supervisory_simulation cannot run inside an active event loop; "
        "await coordinate_simulation instead"
    )


def run_case(
    mode: str,
    *,
    period_name: str = "smoke",
    settings: Settings | None = None,
    fake: bool = False,
    display_delay_seconds: float = 0.0,
    baseline_run_id: str | None = None,
) -> dict[str, Any]:
    """Run one CLI-selected case and return a JSON-ready outcome.

    The normal path always launches the isolated external EnergyPlus runtime.
    The deterministic plant is reachable only through the explicit ``fake``
    argument, and every associated database row is labelled ``is_fake``.
    """

    try:
        simulation_mode = SimulationMode(mode)
    except ValueError as exc:
        allowed = ", ".join(
            item.value
            for item in (
                SimulationMode.BASELINE,
                SimulationMode.RULE,
                SimulationMode.AGENT,
            )
        )
        raise ValueError(f"unsupported run mode {mode!r}; choose one of: {allowed}") from exc
    if simulation_mode not in {
        SimulationMode.BASELINE,
        SimulationMode.RULE,
        SimulationMode.AGENT,
    }:
        raise ValueError("run_case supports baseline, rule, and agent modes")
    if simulation_mode is SimulationMode.BASELINE and baseline_run_id is not None:
        raise ValueError("a baseline run cannot have a parent baseline_run_id")
    if not 0 <= display_delay_seconds <= 10:
        raise ValueError("display_delay_seconds must be in 0..10")
    file_config = load_file_config()
    if period_name not in file_config.periods:
        choices = ", ".join(sorted(file_config.periods))
        raise ValueError(f"unknown period {period_name!r}; choose one of: {choices}")

    runtime_settings = settings or Settings()
    database_path = runtime_settings.resolved_database_path()
    runs_directory = runtime_settings.resolved_runs_dir()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    runs_directory.mkdir(parents=True, exist_ok=True)
    run_id = _new_run_id(simulation_mode)
    run_directory = runs_directory / run_id
    output_directory = run_directory / "energyplus"
    generated = repository_root() / "models" / "generated"
    weather_path = runtime_settings.resolved_weather_path()
    if fake:
        prepared_model = generated / (
            "baseline.idf" if simulation_mode is SimulationMode.BASELINE else "agent_ready.idf"
        )
        prepared_actuator_map = generated / "actuator_map.csv"
        preparation_manifest = generated / "preparation-manifest.json"
        provenance_path: Path | None = None
    else:
        artifacts = prepare_models(
            runtime_settings,
            file_config.periods[period_name],
            output_directory=run_directory / "prepared",
        )
        prepared_model = (
            artifacts.baseline_model
            if simulation_mode is SimulationMode.BASELINE
            else artifacts.agent_ready_model
        )
        prepared_actuator_map = artifacts.actuator_map
        preparation_manifest = artifacts.preparation_manifest
        provenance_path = artifacts.provenance
    preparation_fingerprint = (
        _sha256_file(preparation_manifest) if preparation_manifest.is_file() else None
    )
    weather_sha256 = _sha256_file(weather_path) if weather_path.is_file() else None

    if fake:
        model_path = prepared_model
        actuator_map = prepared_actuator_map
    else:
        model_path, actuator_map = _copy_run_inputs(
            run_directory,
            model=prepared_model,
            actuator_map=prepared_actuator_map,
            preparation_manifest=preparation_manifest,
            provenance=provenance_path,
        )
    if not fake:
        _require_real_run_assets(
            model_path,
            weather_path,
            actuator_map=(None if simulation_mode is SimulationMode.BASELINE else actuator_map),
        )

    store = SQLiteStore(database_path)
    baseline = (
        None
        if simulation_mode is SimulationMode.BASELINE
        else _select_compatible_baseline(
            store,
            period_name=period_name,
            weather_path=weather_path,
            preparation_fingerprint=preparation_fingerprint,
            weather_sha256=weather_sha256,
            is_fake=fake,
            requested_run_id=baseline_run_id,
        )
    )
    store.create_run(
        run_id,
        RunType(simulation_mode.value),
        is_fake=fake,
        energyplus_version=("explicit-fake-plant" if fake else ENERGYPLUS_VERSION),
        model_path=model_path,
        weather_path=weather_path,
        period_name=period_name,
        parent_run_id=baseline.run_id if baseline is not None else None,
        metadata={
            "controller_mode": simulation_mode.value,
            "data_origin": (
                "explicit_deterministic_fake" if fake else "external_pyenergyplus_runtime_api"
            ),
            "preparation_fingerprint": preparation_fingerprint,
            "preparation_manifest_sha256": preparation_fingerprint,
            "weather_sha256": weather_sha256,
            "input_model_sha256": (_sha256_file(model_path) if model_path.is_file() else None),
            "period": {
                "start_month": file_config.periods[period_name].start_month,
                "start_day": file_config.periods[period_name].start_day,
                "end_month": file_config.periods[period_name].end_month,
                "end_day": file_config.periods[period_name].end_day,
            },
            "display_delay_seconds": display_delay_seconds,
            "decision_interval_minutes": runtime_settings.decision_interval_minutes,
            "maximum_action_hold_minutes": runtime_settings.max_action_hold_minutes,
            "simulation_timeout_seconds": runtime_settings.simulation_timeout_seconds,
        },
    )
    request = SimulationRequest(
        run_id=run_id,
        model_path=model_path,
        weather_path=weather_path,
        output_directory=output_directory,
        database_path=database_path,
        mode=simulation_mode,
        energyplus_home=runtime_settings.energyplus_home,
        actuator_map_path=(
            actuator_map if simulation_mode is not SimulationMode.BASELINE else None
        ),
        maximum_action_hold_minutes=runtime_settings.max_action_hold_minutes,
        process_timeout_seconds=runtime_settings.simulation_timeout_seconds,
        decision_interval_minutes=runtime_settings.decision_interval_minutes,
        demand_event_threshold_kw=runtime_settings.demand_threshold_kw,
        display_delay_seconds=display_delay_seconds,
    )
    runner: SimulationRunner = (
        _explicit_fake_runner(
            settings=runtime_settings,
            display_delay_seconds=display_delay_seconds,
        )
        if fake
        else run_simulation
    )
    if simulation_mode is SimulationMode.BASELINE:
        simulation = runner(request)
        payload = _run_payload(
            simulation,
            parent_run_id=None,
            is_fake=fake,
            period_name=period_name,
        )
        return _finalize_completed_real_run(
            payload,
            simulation,
            store=store,
            is_fake=fake,
        )

    result = _run_supervisory_with_runner(
        request,
        settings=runtime_settings,
        runner=runner,
    )
    payload = _run_payload(
        result.simulation,
        parent_run_id=baseline.run_id if baseline is not None else None,
        is_fake=fake,
        period_name=period_name,
    )
    payload["coordinator"] = {
        "observations_seen": result.observations_seen,
        "last_observation_id": result.last_observation_id,
        "decision_count": len(result.decisions),
        "successful_decisions": result.successful_decisions,
        "skipped_due_to_limit": result.skipped_due_to_limit,
        "consecutive_failure_peak": result.consecutive_failure_peak,
        "circuit_breaker_open": result.circuit_breaker_open,
    }
    return _finalize_completed_real_run(
        payload,
        result.simulation,
        store=store,
        is_fake=fake,
    )


def replay_run(
    source_run_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Replay a completed real action sequence without invoking Ollama."""

    runtime_settings = settings or Settings()
    store = SQLiteStore(runtime_settings.resolved_database_path())
    source = store.get_run(source_run_id)
    if source is None:
        raise ValueError(f"unknown run_id: {source_run_id}")
    if source.is_fake:
        raise ValueError("production replay refuses explicitly fake source runs")
    if source.status is not RunStatus.COMPLETED:
        raise ValueError("replay source must be a completed run")
    if source.run_type not in {RunType.AGENT, RunType.RULE, RunType.FIXED_OVERRIDE}:
        raise ValueError("replay source must be a controlled run")
    if source.weather_path is None:
        raise ValueError("replay source has no recorded weather path")

    service = SQLiteMCPService(store=store, settings=runtime_settings)
    replay_artifacts = _run_async(lambda: service.generate_replay_model(source_run_id))
    replay_model = Path(str(replay_artifacts["replay_model"])).resolve()
    replay_schedule = Path(str(replay_artifacts["action_schedule"])).resolve()
    if source.model_path is None:
        raise ValueError("replay source has no immutable model snapshot")
    actuator_map = Path(source.model_path).resolve().parent / "actuator_map.csv"
    weather_path = Path(source.weather_path).resolve()
    _require_real_run_assets(
        replay_model,
        weather_path,
        actuator_map=actuator_map,
    )

    replay_id = _new_run_id(SimulationMode.REPLAY)
    output_directory = runtime_settings.resolved_runs_dir() / replay_id / "energyplus"
    store.create_run(
        replay_id,
        RunType.REPLAY,
        is_fake=False,
        energyplus_version=source.energyplus_version or ENERGYPLUS_VERSION,
        model_path=replay_model,
        weather_path=weather_path,
        period_name=source.period_name,
        parent_run_id=source_run_id,
        metadata={
            "replay_source_run_id": source_run_id,
            "action_count": replay_artifacts["action_count"],
            "replay_method": replay_artifacts["method"],
            "preparation_fingerprint": source.metadata.get(
                "preparation_fingerprint",
                source.metadata.get("preparation_manifest_sha256"),
            ),
            "preparation_manifest_sha256": source.metadata.get(
                "preparation_manifest_sha256",
                source.metadata.get("preparation_fingerprint"),
            ),
            "weather_sha256": source.metadata.get("weather_sha256"),
            "actuator_map_sha256": _sha256_file(actuator_map),
            "replay_timing_source": replay_artifacts.get("timing_source"),
        },
    )
    result = run_simulation(
        SimulationRequest(
            run_id=replay_id,
            model_path=replay_model,
            weather_path=weather_path,
            output_directory=output_directory,
            database_path=runtime_settings.resolved_database_path(),
            mode=SimulationMode.REPLAY,
            energyplus_home=runtime_settings.energyplus_home,
            actuator_map_path=actuator_map,
            replay_schedule_path=replay_schedule,
            maximum_action_hold_minutes=runtime_settings.max_action_hold_minutes,
            process_timeout_seconds=runtime_settings.simulation_timeout_seconds,
            decision_interval_minutes=runtime_settings.decision_interval_minutes,
            demand_event_threshold_kw=runtime_settings.demand_threshold_kw,
        )
    )
    payload = _run_payload(
        result,
        parent_run_id=source_run_id,
        is_fake=False,
        period_name=source.period_name,
    )
    payload["replay"] = dict(replay_artifacts)
    return _finalize_completed_real_run(
        payload,
        result,
        store=store,
        is_fake=False,
    )


async def _run_loop(
    request: SimulationRequest,
    *,
    store: SQLiteStore,
    service: SQLiteMCPService,
    cadence: DecisionCadence,
    config: CoordinatorConfig,
    runner: SimulationRunner,
    agent_host: AgentHost | None,
    decision_timeout: float,
) -> CoordinatorResult:
    simulation_task = asyncio.create_task(
        asyncio.to_thread(runner, request),
        name=f"ecoloop-simulation-{request.run_id}",
    )
    deadline = asyncio.get_running_loop().time() + (
        config.overall_timeout_seconds
        if config.overall_timeout_seconds is not None
        else request.process_timeout_seconds + 30.0
    )
    decisions: list[CoordinatedDecision] = []
    decided_observations: set[int] = set()
    last_seen_observation_id = 0
    previous_observation: BuildingObservation | None = None
    last_decision_simulation_timestamp: datetime | None = None
    simulated_action_expiry: datetime | None = None
    observations_seen = 0
    skipped_due_to_limit = 0
    consecutive_failures = 0
    consecutive_failure_peak = 0
    control_disabled = False
    terminal_seen = False

    try:
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                simulation_task.cancel()
                raise CoordinatorTimeoutError(
                    f"coordinator exceeded its bounded deadline for run {request.run_id}"
                )

            observations = await asyncio.to_thread(
                store.get_recent_observations,
                request.run_id,
                limit=10_000,
            )
            fresh = [
                item for item in observations if item.observation_id > last_seen_observation_id
            ]
            simulation_done = simulation_task.done() or await _run_is_terminal(
                store,
                request.run_id,
            )
            for observation in fresh:
                observations_seen += 1
                last_seen_observation_id = observation.observation_id
                if simulation_done or terminal_seen:
                    previous_observation = observation
                    continue
                constraints = await _constraints(service, request.run_id)
                cadence_result = cadence.evaluate(
                    observation,
                    constraints,
                    previous_observation=previous_observation,
                    last_decision_simulation_timestamp=(last_decision_simulation_timestamp),
                    action_expires_at=simulated_action_expiry,
                )
                previous_observation = observation
                if (
                    not cadence_result.should_decide
                    or observation.observation_id in decided_observations
                ):
                    continue
                decided_observations.add(observation.observation_id)
                if control_disabled or len(decisions) >= config.maximum_decisions:
                    skipped_due_to_limit += 1
                    continue

                started = time.perf_counter()
                terminal_observation = observation
                try:
                    remaining_seconds = max(
                        0.01,
                        deadline - asyncio.get_running_loop().time(),
                    )
                    decision = await _await_decision_or_simulation(
                        _make_decision(
                            request.mode,
                            request.run_id,
                            observation,
                            service=service,
                            agent_host=agent_host,
                        ),
                        simulation_task=simulation_task,
                        timeout_seconds=min(decision_timeout, remaining_seconds),
                    )
                    if decision is None or await _run_is_terminal(store, request.run_id):
                        terminal_seen = True
                        previous_observation = observation
                        continue
                    status, fallback, result, terminal_observation_id = decision
                    decided_observations.add(terminal_observation_id)
                    if terminal_observation_id != observation.observation_id:
                        resolved_terminal = await asyncio.to_thread(
                            store.get_observation,
                            request.run_id,
                            terminal_observation_id,
                        )
                        if resolved_terminal is not None:
                            terminal_observation = resolved_terminal
                    error = None
                    consecutive_failures = 0
                except TimeoutError:
                    if simulation_task.done() or await _run_is_terminal(
                        store,
                        request.run_id,
                    ):
                        terminal_seen = True
                        previous_observation = observation
                        continue
                    recovered = await _recover_with_fallback(
                        request.run_id,
                        observation,
                        service=service,
                        store=store,
                        error="coordinator decision timeout",
                    )
                    if recovered is None:
                        terminal_seen = True
                        previous_observation = observation
                        continue
                    status, fallback, result, error = recovered
                    consecutive_failures = consecutive_failures + 1 if status == "failed" else 0
                except (
                    AgentProtocolError,
                    OSError,
                    RunStateError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    if simulation_task.done() or await _run_is_terminal(
                        store,
                        request.run_id,
                    ):
                        terminal_seen = True
                        previous_observation = observation
                        continue
                    message = _bounded_error(exc)
                    await asyncio.to_thread(
                        store.record_error,
                        request.run_id,
                        MessageSeverity.SEVERE,
                        "coordinator",
                        message,
                    )
                    recovered = await _recover_with_fallback(
                        request.run_id,
                        observation,
                        service=service,
                        store=store,
                        error=message,
                    )
                    if recovered is None:
                        terminal_seen = True
                        previous_observation = observation
                        continue
                    status, fallback, result, error = recovered
                    consecutive_failures = consecutive_failures + 1 if status == "failed" else 0
                consecutive_failure_peak = max(
                    consecutive_failure_peak,
                    consecutive_failures,
                )
                decisions.append(
                    CoordinatedDecision(
                        observation_id=observation.observation_id,
                        simulation_timestamp=observation.simulation_timestamp,
                        triggers=cadence_result.triggers,
                        status=status,
                        fallback=fallback,
                        latency_ms=max(
                            0.0,
                            (time.perf_counter() - started) * 1000.0,
                        ),
                        error=error,
                    )
                )
                if status in {"applied", "fallback"}:
                    last_decision_simulation_timestamp = terminal_observation.simulation_timestamp
                    hold_minutes = _hold_minutes(result)
                    simulated_action_expiry = terminal_observation.simulation_timestamp + timedelta(
                        minutes=hold_minutes
                    )
                if consecutive_failures >= config.maximum_consecutive_decision_failures:
                    control_disabled = True
                    skipped_due_to_limit += sum(
                        item.observation_id > observation.observation_id for item in fresh
                    )
                    break

            if simulation_task.done() or terminal_seen:
                break
            await asyncio.sleep(config.observation_poll_seconds)

        simulation = await simulation_task
        if config.terminal_settle_seconds:
            await asyncio.sleep(config.terminal_settle_seconds)
        final_observation = await asyncio.to_thread(
            store.get_current_observation,
            request.run_id,
        )
        if final_observation is not None:
            last_seen_observation_id = max(
                last_seen_observation_id,
                final_observation.observation_id,
            )
        return CoordinatorResult(
            simulation=simulation,
            decisions=tuple(decisions),
            observations_seen=observations_seen,
            last_observation_id=last_seen_observation_id,
            skipped_due_to_limit=skipped_due_to_limit,
            consecutive_failure_peak=consecutive_failure_peak,
            circuit_breaker_open=bool(
                agent_host is not None and agent_host.circuit_breaker.is_open(request.run_id)
            ),
        )
    finally:
        if not simulation_task.done():
            simulation_task.cancel()
            await asyncio.gather(simulation_task, return_exceptions=True)


async def _make_decision(
    mode: SimulationMode,
    run_id: str,
    observation: BuildingObservation,
    *,
    service: SQLiteMCPService,
    agent_host: AgentHost | None,
) -> tuple[str, bool, dict[str, Any], int]:
    if mode is SimulationMode.AGENT:
        if agent_host is None:
            raise CoordinatorError("agent mode requires an initialized MCP agent host")
        decision = await agent_host.decide(
            run_id,
            state_hint=observation.model_dump(mode="json"),
        )
        return (
            decision.status,
            decision.status == "fallback",
            decision.result,
            decision.observation_id,
        )
    result = await service.request_safe_fallback(run_id, observation.observation_id)
    return "fallback", True, result, observation.observation_id


async def _await_decision_or_simulation(
    decision: Coroutine[Any, Any, tuple[str, bool, dict[str, Any], int]],
    *,
    simulation_task: asyncio.Task[SimulationResult],
    timeout_seconds: float,
) -> tuple[str, bool, dict[str, Any], int] | None:
    """Cancel and drain a decision when its simulation reaches terminal first."""

    decision_task = asyncio.create_task(decision)
    try:
        completed, _ = await asyncio.wait(
            {decision_task, simulation_task},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if simulation_task in completed:
            decision_task.cancel()
            await asyncio.gather(decision_task, return_exceptions=True)
            return None
        if decision_task not in completed:
            decision_task.cancel()
            await asyncio.gather(decision_task, return_exceptions=True)
            raise TimeoutError
        return decision_task.result()
    finally:
        if not decision_task.done():
            decision_task.cancel()
            await asyncio.gather(decision_task, return_exceptions=True)


async def _run_is_terminal(store: SQLiteStore, run_id: str) -> bool:
    """Return whether durable run state has crossed its terminal boundary."""

    run = await asyncio.to_thread(store.get_run, run_id)
    return run is not None and run.status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.CANCELLED,
    }


async def _run_is_active(store: SQLiteStore, run_id: str) -> bool:
    """Return whether control-affecting calls are still allowed for a run."""

    run = await asyncio.to_thread(store.get_run, run_id)
    return run is not None and run.status is RunStatus.RUNNING


async def _recover_with_fallback(
    run_id: str,
    observation: BuildingObservation,
    *,
    service: SQLiteMCPService,
    store: SQLiteStore,
    error: str,
) -> tuple[str, bool, dict[str, Any], str | None] | None:
    if await _run_is_terminal(store, run_id):
        return None
    latest = await asyncio.to_thread(store.get_last_applied_action, run_id)
    if latest is not None and latest[0].observation_id == observation.observation_id:
        return (
            "applied",
            bool(latest[0].fallback),
            {
                "applied_action": latest[1].applied_action.model_dump(mode="json")
                if latest[1].applied_action is not None
                else None
            },
            error,
        )
    if await _run_is_terminal(store, run_id):
        return None
    try:
        result = await service.request_safe_fallback(
            run_id,
            observation.observation_id,
        )
    except (OSError, RunStateError, RuntimeError, TypeError, ValueError) as fallback_exc:
        if await _run_is_terminal(store, run_id):
            return None
        fallback_error = f"{error}; fallback failed: {_bounded_error(fallback_exc)}"
        await asyncio.to_thread(
            store.record_error,
            run_id,
            MessageSeverity.SEVERE,
            "coordinator-fallback",
            fallback_error,
        )
        return "failed", True, {}, fallback_error
    return "fallback", True, result, error


async def _constraints(
    service: SQLiteMCPService,
    run_id: str,
) -> ControlConstraints:
    payload = await service.get_constraints(run_id)
    return ControlConstraints.model_validate(payload)


def _request_with_bounded_wait(
    request: SimulationRequest,
    config: CoordinatorConfig,
    *,
    decision_timeout: float,
) -> SimulationRequest:
    if request.mode is SimulationMode.AGENT:
        wait_seconds = min(
            300.0,
            max(request.decision_wait_seconds, decision_timeout),
        )
    else:
        wait_seconds = min(
            300.0,
            max(
                request.decision_wait_seconds,
                config.rule_decision_wait_seconds,
            ),
        )
    process_timeout = request.process_timeout_seconds
    if config.overall_timeout_seconds is not None:
        process_timeout = min(
            process_timeout,
            max(0.01, config.overall_timeout_seconds * 0.8),
        )
    return replace(
        request,
        decision_wait_seconds=wait_seconds,
        process_timeout_seconds=process_timeout,
    )


def _decision_timeout(settings: Settings, config: CoordinatorConfig) -> float:
    if config.decision_timeout_seconds is not None:
        timeout = config.decision_timeout_seconds
    else:
        rounds = max(1, settings.max_tool_rounds)
        timeout = min(
            300.0,
            settings.llm_timeout_seconds * (rounds + 1) + 5.0,
        )
    if config.overall_timeout_seconds is not None:
        timeout = min(
            timeout,
            max(0.01, config.overall_timeout_seconds * 0.8),
        )
    return timeout


def _hold_minutes(result: dict[str, Any]) -> int:
    for key in ("applied_action", "action"):
        action = result.get(key)
        if isinstance(action, dict):
            value = action.get("hold_minutes")
            if isinstance(value, int) and value > 0:
                return value
    validation = result.get("validation")
    if isinstance(validation, dict):
        applied = validation.get("applied_action")
        if isinstance(applied, dict):
            value = applied.get("hold_minutes")
            if isinstance(value, int) and value > 0:
                return value
    return 60


def _bounded_error(exc: BaseException) -> str:
    return (" ".join(str(exc).split()) or exc.__class__.__name__)[:500]


def _run_supervisory_with_runner(
    request: SimulationRequest,
    *,
    settings: Settings,
    runner: SimulationRunner,
) -> CoordinatorResult:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            coordinate_simulation(
                request,
                settings=settings,
                runner=runner,
            )
        )
    raise RuntimeError(
        "run_case cannot run inside an active event loop; await coordinate_simulation instead"
    )


def _run_async(
    factory: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    raise RuntimeError("this synchronous operation cannot run inside an active event loop")


def _new_run_id(mode: SimulationMode) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{mode.value}-{timestamp}-{uuid.uuid4().hex[:8]}"


def _require_real_run_assets(
    model_path: Path,
    weather_path: Path,
    *,
    actuator_map: Path | None,
) -> None:
    missing = [
        f"{label}: {path}"
        for label, path in (
            ("prepared model", model_path),
            ("weather file", weather_path),
            ("actuator map", actuator_map),
        )
        if path is not None and not path.is_file()
    ]
    if missing:
        joined = "\n- ".join(missing)
        raise ValueError(
            "required real-run assets are missing:\n"
            f"- {joined}\n"
            "Run `python -m ecoloop doctor` and "
            "`python -m ecoloop prepare-model --period <period>`."
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_run_inputs(
    run_directory: Path,
    *,
    model: Path,
    actuator_map: Path,
    preparation_manifest: Path,
    provenance: Path | None,
) -> tuple[Path, Path]:
    """Snapshot prepared control inputs before another period can be built."""

    input_directory = run_directory / "inputs"
    input_directory.mkdir(parents=True, exist_ok=True)
    model_copy = input_directory / model.name
    actuator_copy = input_directory / "actuator_map.csv"
    shutil.copy2(model, model_copy)
    shutil.copy2(actuator_map, actuator_copy)
    shutil.copy2(
        preparation_manifest,
        input_directory / "preparation-manifest.json",
    )
    if provenance is not None and provenance.is_file():
        shutil.copy2(provenance, input_directory / "model-provenance.json")
    return model_copy, actuator_copy


def _select_compatible_baseline(
    store: SQLiteStore,
    *,
    period_name: str,
    weather_path: Path,
    preparation_fingerprint: str | None,
    weather_sha256: str | None,
    is_fake: bool,
    requested_run_id: str | None = None,
) -> RunRecord | None:
    """Select a verified baseline with exact immutable physical provenance."""

    expected_weather = str(weather_path.resolve())
    expected_version = "explicit-fake-plant" if is_fake else ENERGYPLUS_VERSION
    if preparation_fingerprint is None or weather_sha256 is None:
        if requested_run_id is not None:
            raise ValueError(
                "cannot lock the requested baseline because current model or weather "
                "provenance is missing"
            )
        return None
    requested = store.get_run(requested_run_id) if requested_run_id is not None else None
    if requested_run_id is not None and requested is None:
        raise ValueError(f"requested baseline run does not exist: {requested_run_id}")
    candidates = (
        [requested]
        if requested is not None
        else store.list_runs(
            status=RunStatus.COMPLETED,
            run_type=RunType.BASELINE,
            include_fake=True,
            limit=10_000,
        )
    )
    rejection_reason = "requested baseline is not compatible with the controlled run"
    for run in candidates:
        if (
            run.run_type is not RunType.BASELINE
            or run.status is not RunStatus.COMPLETED
            or run.is_fake != is_fake
            or run.period_name != period_name
            or run.weather_path != expected_weather
            or run.energyplus_version != expected_version
        ):
            rejection_reason = "requested baseline type, status, period, path, or version differs"
            continue
        recorded_family = run.metadata.get(
            "preparation_fingerprint",
            run.metadata.get("preparation_manifest_sha256"),
        )
        if recorded_family != preparation_fingerprint:
            rejection_reason = "requested baseline model-preparation fingerprint differs"
            continue
        if run.metadata.get("weather_sha256") != weather_sha256:
            rejection_reason = "requested baseline weather-content checksum differs"
            continue
        if not is_fake:
            try:
                load_verified_final_metrics(store, run.run_id)
            except EvaluationError:
                rejection_reason = "requested baseline lacks verified canonical final metrics"
                continue
        return run
    if requested_run_id is not None:
        raise ValueError(f"{rejection_reason}: {requested_run_id}")
    return None


def _run_payload(
    result: SimulationResult,
    *,
    parent_run_id: str | None,
    is_fake: bool,
    period_name: str | None,
) -> dict[str, Any]:
    payload = asdict(result)
    payload["output_directory"] = str(result.output_directory)
    payload["parent_run_id"] = parent_run_id
    payload["period_name"] = period_name
    payload["is_fake"] = is_fake
    payload["data_status"] = "EXPLICIT_FAKE_TEST_DATA" if is_fake else "REAL_ENERGYPLUS_OUTPUT"
    payload["simulation_status"] = result.status
    return payload


def _finalize_completed_real_run(
    payload: dict[str, Any],
    result: SimulationResult,
    *,
    store: SQLiteStore,
    is_fake: bool,
) -> dict[str, Any]:
    if is_fake:
        payload["evaluation"] = {
            "status": "not_applicable_fake",
            "verified_for_comparison": False,
            "verification_reasons": ["explicit fake runs never produce publishable metrics"],
        }
        return payload
    if not result.completed:
        payload["evaluation"] = {
            "status": "not_run",
            "verified_for_comparison": False,
            "verification_reasons": ["EnergyPlus simulation did not complete cleanly"],
        }
        return payload

    from ecoloop.evaluation import finalize_run_directory

    try:
        finalization = finalize_run_directory(
            store,
            result.run_id,
            result.output_directory,
            output_directory=result.output_directory.parent,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        payload["status"] = "evaluation_failed"
        payload["evaluation"] = {
            "status": "failed",
            "verified_for_comparison": False,
            "verification_reasons": [_bounded_error(exc)],
        }
        return payload

    verified = finalization.verified_for_comparison
    payload["evaluation"] = {
        "status": "completed" if verified else "failed_verification",
        "verified_for_comparison": verified,
        "verification_reasons": list(finalization.verification_reasons),
        "metrics_json_path": str(finalization.metrics_json_path),
        "metrics_csv_path": str(finalization.metrics_csv_path),
        "metrics": finalization.metrics.model_dump(mode="json"),
    }
    if not verified:
        payload["status"] = "evaluation_failed"
    return payload


def _explicit_fake_runner(
    *,
    settings: Settings,
    display_delay_seconds: float,
) -> SimulationRunner:
    """Return the deterministic test plant used only by ``run_case(fake=True)``."""

    def run(request: SimulationRequest) -> SimulationResult:
        started = time.monotonic()
        store = SQLiteStore(request.database_path)
        record = store.get_run(request.run_id)
        if record is None or not record.is_fake:
            raise CoordinatorError("explicit fake runner requires a pre-created is_fake=1 run")
        if record.status is RunStatus.PENDING:
            store.set_run_status(request.run_id, RunStatus.RUNNING)
        request.output_directory.mkdir(parents=True, exist_ok=True)

        simulated_start = datetime(2024, 7, 15, 8, 0, tzinfo=UTC)
        heating_setpoint = 21.0
        cooling_setpoint = 24.0
        zone_temperature = 24.4
        cumulative_energy = 0.0
        action_count = 0
        failure: str | None = None
        last_action_generation = 0

        for index in range(8):
            occupied = index < 4
            if request.mode is SimulationMode.BASELINE:
                heating_setpoint = 21.0 if occupied else 18.0
                cooling_setpoint = 24.0 if occupied else 28.0
            simulation_time = simulated_start + timedelta(minutes=15 * index)
            outdoor_temperature = 31.0 + index * 0.2
            pmv = (zone_temperature - 24.0) * 0.25
            demand_kw = (
                2.0
                + abs(outdoor_temperature - zone_temperature) * 0.15
                + max(0.0, zone_temperature - cooling_setpoint) * 1.8
                + max(0.0, heating_setpoint - zone_temperature) * 1.8
            )
            step_energy = demand_kw * 0.25
            cumulative_energy += step_energy
            wall_time = datetime.now(UTC)
            observation = store.record_observation(
                ObservationInput(
                    run_id=request.run_id,
                    timestamp=wall_time,
                    simulation_timestamp=simulation_time,
                    timestep_key=f"{request.run_id}|explicit-fake|{index:04d}",
                    environment="explicit_fake_test",
                    occupied=occupied,
                    occupancy_count=10.0 if occupied else 0.0,
                    zone_temperature_mean_c=zone_temperature,
                    zone_temperature_min_c=zone_temperature - 0.4,
                    zone_temperature_max_c=zone_temperature + 0.4,
                    operative_temperature_mean_c=zone_temperature,
                    operative_temperature_min_c=zone_temperature - 0.4,
                    operative_temperature_max_c=zone_temperature + 0.4,
                    relative_humidity_mean_percent=52.0,
                    relative_humidity_max_percent=55.0,
                    pmv_mean=pmv,
                    pmv_max_abs=abs(pmv),
                    ppd_mean_percent=min(100.0, 5.0 + abs(pmv) * 20.0),
                    ppd_max_percent=min(100.0, 7.0 + abs(pmv) * 22.0),
                    co2_mean_ppm=720.0 if occupied else 450.0,
                    co2_max_ppm=760.0 if occupied else 470.0,
                    outdoor_temperature_c=outdoor_temperature,
                    forecast_temperature_mean_c=outdoor_temperature + 0.8,
                    forecast_temperature_max_c=outdoor_temperature + 1.5,
                    forecast_solar_mean_w_m2=500.0,
                    heating_setpoint_c=heating_setpoint,
                    cooling_setpoint_c=cooling_setpoint,
                    facility_demand_kw=demand_kw,
                    timestep_electricity_kwh=step_energy,
                    cumulative_electricity_kwh=cumulative_energy,
                    hvac_electricity_kwh=max(0.0, step_energy - 0.35),
                    tariff_per_kwh=settings.tariff_per_kwh,
                    carbon_kg_per_kwh=settings.carbon_kg_per_kwh,
                    actuator_capabilities=ActuatorCapabilities(
                        heating_setpoint=(request.mode is not SimulationMode.BASELINE),
                        cooling_setpoint=(request.mode is not SimulationMode.BASELINE),
                    ),
                )
            )

            if request.mode is not SimulationMode.BASELINE and index in {0, 4}:
                action = _wait_for_fake_action(
                    store,
                    request,
                    observation.observation_id,
                )
                if action is None:
                    failure = (
                        "explicit fake plant did not receive a validated exact-"
                        f"observation action for observation {observation.observation_id}"
                    )
                    break
                proposal, validation = action
                if (
                    validation.applied_action is None
                    or proposal.action_generation <= last_action_generation
                ):
                    failure = "explicit fake plant received an invalid action sequence"
                    break
                applied = validation.applied_action
                heating_setpoint = float(applied.heating_setpoint_c)
                cooling_setpoint = float(applied.cooling_setpoint_c)
                last_action_generation = proposal.action_generation
                action_count += 1

            if zone_temperature > cooling_setpoint:
                zone_temperature -= min(
                    0.45,
                    (zone_temperature - cooling_setpoint) * 0.35 + 0.05,
                )
            elif zone_temperature < heating_setpoint:
                zone_temperature += min(
                    0.45,
                    (heating_setpoint - zone_temperature) * 0.35 + 0.05,
                )
            else:
                zone_temperature += (outdoor_temperature - zone_temperature) * 0.025
            if display_delay_seconds:
                time.sleep(display_delay_seconds)

        current = store.get_run(request.run_id)
        status = "failed" if failure is not None else "completed"
        if current is not None and current.status is RunStatus.RUNNING:
            store.set_run_status(
                request.run_id,
                RunStatus.FAILED if failure is not None else RunStatus.COMPLETED,
                error_summary=failure,
            )
        if failure is not None:
            store.record_error(
                request.run_id,
                MessageSeverity.SEVERE,
                "explicit-fake-plant",
                failure,
            )
        return SimulationResult(
            run_id=request.run_id,
            status=status,
            exit_code=1 if failure is not None else 0,
            output_directory=request.output_directory,
            elapsed_seconds=max(0.0, time.monotonic() - started),
            progress_percent=100 if failure is None else 0,
            warning_count=0,
            severe_count=1 if failure is not None else 0,
            fatal_count=0,
            observation_count=index + 1,
            applied_action_count=action_count,
            error=failure,
        )

    return run


def _wait_for_fake_action(
    store: SQLiteStore,
    request: SimulationRequest,
    observation_id: int,
) -> tuple[Any, Any] | None:
    deadline = time.monotonic() + min(
        60.0,
        max(1.0, request.decision_wait_seconds),
    )
    while time.monotonic() < deadline:
        latest = store.get_last_applied_action(request.run_id)
        if latest is not None and latest[0].observation_id == observation_id:
            return latest
        time.sleep(request.decision_poll_seconds)
    return None


__all__ = [
    "CoordinatedDecision",
    "CoordinatorConfig",
    "CoordinatorError",
    "CoordinatorResult",
    "CoordinatorTimeoutError",
    "coordinate_simulation",
    "replay_run",
    "run_case",
    "run_supervisory_simulation",
]
