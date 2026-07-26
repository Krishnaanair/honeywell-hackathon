"""Dependency-aware real EnergyPlus, MCP stdio, and Ollama acceptance smoke."""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from typing import Any

import pytest

from ecoloop.agent.ollama_host import OllamaModelBackend
from ecoloop.config import Settings
from ecoloop.coordinator import run_case
from ecoloop.db.store import SQLiteStore
from ecoloop.energyplus.discovery import discover_energyplus
from ecoloop.evaluation import compare_and_write, load_verified_final_metrics
from ecoloop.schemas import (
    ControlAction,
    RunStatus,
    RunType,
    ToolCallTrace,
    ValidationResult,
)

REQUIRED_STATE_TOOL = "get_current_building_state"
REQUIRED_CONSTRAINT_TOOL = "get_constraints"
CANDIDATE_TOOLS = {"generate_candidate_actions", "evaluate_candidate_actions"}
TERMINAL_TOOLS = {"apply_control_action", "request_safe_fallback"}


def _real_settings(tmp_path: Path) -> Settings:
    """Return isolated settings or skip with actionable dependency fixes."""

    configured = Settings()
    installation = discover_energyplus(configured)
    reasons: list[str] = []
    if installation is None:
        reasons.append("EnergyPlus 26.1.0 was not discovered")
    else:
        if not installation.is_version_match:
            reasons.append(f"EnergyPlus version is {installation.version!r}, not required 26.1.0")
        if not installation.is_runtime_complete:
            reasons.append("PyEnergyPlus Runtime API or the dynamic library is unavailable")
        if not installation.is_model_tooling_complete:
            reasons.append("EnergyPlus schema/ConvertInputFormat tooling is unavailable")
    model = configured.resolved_model_path()
    weather = configured.resolved_weather_path()
    if not model.is_file():
        reasons.append(f"base model is missing: {model}")
    if not weather.is_file():
        reasons.append(f"weather file is missing: {weather}")
    if reasons:
        pytest.skip(
            "; ".join(reasons) + ". Install EnergyPlus 26.1.0, set ENERGYPLUS_HOME, install the "
            "checksummed weather file, then run `python -m ecoloop doctor`."
        )
    assert installation is not None

    settings = Settings(
        ENERGYPLUS_HOME=installation.home,
        ECOLOOP_MODEL_PATH=model,
        ECOLOOP_WEATHER_PATH=weather,
        ECOLOOP_DATABASE_PATH=tmp_path / "runs" / "closed-loop.db",
        ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ECOLOOP_SUBMISSION_DIR=tmp_path / "submission",
        OLLAMA_HOST=configured.ollama_host,
        OLLAMA_MODEL=configured.ollama_model,
        OLLAMA_KEEP_ALIVE=configured.ollama_keep_alive,
        LLM_TIMEOUT_SECONDS=max(60.0, configured.llm_timeout_seconds),
        ECOLOOP_MAX_TOOL_ROUNDS=configured.max_tool_rounds,
        ECOLOOP_MAX_CONSECUTIVE_TIMEOUTS=configured.max_consecutive_timeouts,
        ECOLOOP_STATE_TOKEN_BUDGET=configured.state_token_budget,
        ECOLOOP_ZONE_TIMESTEP_MINUTES=15,
        ECOLOOP_DECISION_INTERVAL_MINUTES=60,
        ECOLOOP_MAX_ACTION_HOLD_MINUTES=120,
        ECOLOOP_TARIFF_PER_KWH=configured.tariff_per_kwh,
        ECOLOOP_CARBON_KG_PER_KWH=configured.carbon_kg_per_kwh,
        ECOLOOP_DEMAND_THRESHOLD_KW=configured.demand_threshold_kw,
    )
    _require_ollama(settings)
    return settings


def _require_ollama(settings: Settings) -> None:
    """Skip when the loopback API or configured local model is unavailable."""

    try:
        backend = OllamaModelBackend(
            host=settings.ollama_host,
            model=settings.ollama_model,
            timeout_seconds=settings.llm_timeout_seconds,
            keep_alive=settings.ollama_keep_alive,
        )
        installed = asyncio.run(backend.available_models())
    except (OSError, RuntimeError, ValueError) as exc:
        pytest.skip(
            f"Ollama is unavailable at {settings.ollama_host}: {exc}. "
            "Start `ollama serve`, use a loopback OLLAMA_HOST, and rerun "
            "`python -m ecoloop doctor`."
        )
    if settings.ollama_model not in installed:
        pytest.skip(
            f"configured Ollama model is missing: {settings.ollama_model}. "
            f"Run `ollama pull {settings.ollama_model}` and rerun the test."
        )


def _assert_verified_run(payload: dict[str, Any]) -> None:
    """Require a completed real run and verified official-output finalization."""

    assert payload["status"] == "completed", payload
    assert payload["is_fake"] is False
    assert payload["data_status"] == "REAL_ENERGYPLUS_OUTPUT"
    evaluation = payload["evaluation"]
    assert evaluation["status"] == "completed", evaluation
    assert evaluation["verified_for_comparison"] is True, evaluation
    assert evaluation["verification_reasons"] == []
    assert Path(evaluation["metrics_json_path"]).is_file()
    assert Path(evaluation["metrics_csv_path"]).is_file()


def _decision_segment(
    traces: list[ToolCallTrace],
    terminal_trace: ToolCallTrace,
) -> list[ToolCallTrace]:
    """Return one MCP decision trace ending at the selected terminal call."""

    terminal_index = traces.index(terminal_trace)
    start_index = 0
    for index in range(terminal_index - 1, -1, -1):
        if traces[index].tool_name in TERMINAL_TOOLS:
            start_index = index + 1
            break
    return traces[start_index : terminal_index + 1]


def _assert_required_sequence(segment: list[ToolCallTrace]) -> None:
    """Require ordered state, constraints, candidates, then model apply."""

    names = [trace.tool_name for trace in segment]
    state_index = names.index(REQUIRED_STATE_TOOL)
    constraint_index = names.index(REQUIRED_CONSTRAINT_TOOL, state_index + 1)
    candidate_index = next(
        index
        for index in range(constraint_index + 1, len(names))
        if names[index] in CANDIDATE_TOOLS
    )
    apply_index = names.index("apply_control_action", candidate_index + 1)
    assert state_index < constraint_index < candidate_index < apply_index
    assert apply_index == len(names) - 1
    assert all(trace.success for trace in segment)


def _model_selected_actions(
    store: SQLiteStore,
    run_id: str,
    apply_trace: ToolCallTrace,
) -> list[tuple[ControlAction, ValidationResult]]:
    """Return accepted actions that correspond to the model's terminal call."""

    arguments = apply_trace.arguments["action"]
    assert isinstance(arguments, dict)
    observation_id = int(apply_trace.arguments["observation_id"])
    generation = int(arguments["action_generation"])
    return [
        (action, validation)
        for action, validation in store.get_applied_actions(run_id)
        if (
            action.observation_id == observation_id
            and action.action_generation == generation
            and not action.fallback
            and validation.accepted
            and validation.applied_action is not None
        )
    ]


def _has_subsequent_setpoint_and_temperature_response(
    store: SQLiteStore,
    run_id: str,
    model_actions: list[tuple[ControlAction, ValidationResult]],
) -> bool:
    """Prove a changed model action appears in later physical telemetry."""

    observations = store.get_recent_observations(run_id, limit=10_000)
    for action, validation in model_actions:
        before = store.get_observation(run_id, action.observation_id)
        applied = validation.applied_action
        if before is None or applied is None:
            continue
        changed = (
            abs(applied.heating_setpoint_c - before.heating_setpoint_c) >= 0.05
            or abs(applied.cooling_setpoint_c - before.cooling_setpoint_c) >= 0.05
        )
        if not changed:
            continue
        for after in observations:
            if after.simulation_timestamp <= before.simulation_timestamp:
                continue
            setpoints_reported = (
                abs(after.heating_setpoint_c - applied.heating_setpoint_c) < 0.05
                and abs(after.cooling_setpoint_c - applied.cooling_setpoint_c) < 0.05
            )
            temperature_responded = (
                abs(after.zone_temperature_mean_c - before.zone_temperature_mean_c) > 1e-5
            )
            if setpoints_reported and temperature_responded:
                return True
    return False


def _assert_official_metrics(metrics: Any) -> None:
    """Require verified EnergyPlus totals rather than a callback-only total."""

    assert metrics.status is RunStatus.COMPLETED
    assert metrics.facility_electricity_kwh > 0
    assert metrics.peak_electrical_demand_kw > 0
    assert metrics.fatal_count == 0
    assert metrics.energy_cross_check.passed
    assert any(
        Path(path).name.casefold() in {"eplusout.sql", "eplusout.csv"}
        for path in metrics.source_artifacts
    )


@pytest.mark.real_closed_loop
def test_real_energyplus_mcp_ollama_closed_loop_smoke(tmp_path: Path) -> None:
    """Prove baseline, model-selected actuation, response, and honest comparison."""

    settings = _real_settings(tmp_path)
    baseline_payload = run_case(
        "baseline",
        period_name="smoke",
        settings=settings,
    )
    _assert_verified_run(baseline_payload)
    baseline_run_id = str(baseline_payload["run_id"])

    agent_payload = run_case(
        "agent",
        period_name="smoke",
        settings=settings,
    )
    _assert_verified_run(agent_payload)
    agent_run_id = str(agent_payload["run_id"])
    assert agent_payload["parent_run_id"] == baseline_run_id
    assert int(agent_payload["applied_action_count"]) > 0

    store = SQLiteStore(settings.resolved_database_path())
    baseline_run = store.get_run(baseline_run_id)
    agent_run = store.get_run(agent_run_id)
    assert baseline_run is not None
    assert baseline_run.run_type is RunType.BASELINE
    assert baseline_run.status is RunStatus.COMPLETED
    assert not baseline_run.is_fake
    assert agent_run is not None
    assert agent_run.run_type is RunType.AGENT
    assert agent_run.status is RunStatus.COMPLETED
    assert not agent_run.is_fake
    assert agent_run.parent_run_id == baseline_run_id

    traces = store.get_recent_tool_calls(agent_run_id, limit=1_000)
    apply_traces = [
        trace
        for trace in traces
        if (
            trace.tool_name == "apply_control_action"
            and trace.success
            and isinstance(trace.result, dict)
            and trace.result.get("success") is True
            and isinstance(trace.arguments.get("action"), dict)
            and trace.arguments["action"].get("model") == settings.ollama_model
            and trace.arguments["action"].get("reason_code") != "CACHE_REUSE"
        )
    ]
    assert apply_traces, "the local model never completed a successful apply_control_action"
    response_evidence: (
        tuple[
            ToolCallTrace,
            list[ToolCallTrace],
            list[tuple[ControlAction, ValidationResult]],
        ]
        | None
    ) = None
    for apply_trace in apply_traces:
        candidate_segment = _decision_segment(traces, apply_trace)
        _assert_required_sequence(candidate_segment)
        candidate_actions = _model_selected_actions(store, agent_run_id, apply_trace)
        assert candidate_actions
        if _has_subsequent_setpoint_and_temperature_response(
            store,
            agent_run_id,
            candidate_actions,
        ):
            response_evidence = (apply_trace, candidate_segment, candidate_actions)
            break
    assert response_evidence is not None, (
        "no model-selected changed setpoint was reported by later telemetry "
        "with a subsequent zone-temperature response"
    )
    _, segment, model_actions = response_evidence

    state_trace = next(trace for trace in segment if trace.tool_name == REQUIRED_STATE_TOOL)
    assert state_trace.result is not None
    state_observation = state_trace.result["observation"]
    assert isinstance(state_observation, dict)
    baseline_reference = state_observation["baseline_reference"]
    assert isinstance(baseline_reference, dict)
    assert baseline_reference["run_id"] == baseline_run_id

    assert all(action.model == settings.ollama_model for action, _ in model_actions)

    baseline_metrics = load_verified_final_metrics(store, baseline_run_id)
    agent_metrics = load_verified_final_metrics(store, agent_run_id)
    _assert_official_metrics(baseline_metrics)
    _assert_official_metrics(agent_metrics)
    assert agent_metrics.llm_decision_count > 0
    assert agent_metrics.tool_call_count >= len(segment)

    comparison_directory = tmp_path / "verified-comparison"
    comparison = compare_and_write(
        store,
        baseline_run_id,
        agent_run_id,
        output_directory=comparison_directory,
    )
    assert comparison.json_path.is_file()
    assert comparison.csv_path.is_file()
    expected_saving = (
        100.0
        * (baseline_metrics.facility_electricity_kwh - agent_metrics.facility_electricity_kwh)
        / baseline_metrics.facility_electricity_kwh
    )
    assert comparison.comparison.electricity_saving_percent == pytest.approx(expected_saving)

    json_payload = json.loads(comparison.json_path.read_text(encoding="utf-8"))
    assert json_payload["baseline_run_id"] == baseline_run_id
    assert json_payload["agent_run_id"] == agent_run_id
    with comparison.csv_path.open(encoding="utf-8", newline="") as handle:
        csv_payload = next(csv.DictReader(handle))
    assert csv_payload["baseline_run_id"] == baseline_run_id
    assert csv_payload["agent_run_id"] == agent_run_id
    assert store.get_metrics(agent_run_id, verified_only=True)["comparison"]["verified"]
