"""Verified finalization and comparison integration tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecoloop.db.store import SQLiteStore
from ecoloop.energyplus.results import EnergyPlusResults
from ecoloop.evaluation import (
    EvaluationError,
    FinalizationResult,
    compare_and_write,
    finalize_run,
    load_verified_final_metrics,
)
from ecoloop.schemas import (
    ActuatorCapabilities,
    BuildingTelemetry,
    CandidateAction,
    ClampDetail,
    ControlAction,
    ObservationInput,
    ReasonCode,
    RunStatus,
    RunType,
    ToolCallTrace,
    ValidationCode,
    ValidationIssue,
    ValidationResult,
    ZoneTelemetry,
)

WALL_TIME = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
SIMULATION_START = datetime(2001, 7, 15, 0, 15, tzinfo=UTC)


def _assets(root: Path) -> tuple[Path, Path, Path, Path]:
    model_directory = root / "models" / "generated"
    model_directory.mkdir(parents=True, exist_ok=True)
    baseline_model = model_directory / "baseline.idf"
    agent_model = model_directory / "agent_ready.idf"
    manifest = model_directory / "preparation-manifest.json"
    weather = root / "weather" / "test.epw"
    weather.parent.mkdir(parents=True, exist_ok=True)
    baseline_model.write_text("Version,26.1;\n", encoding="utf-8")
    agent_model.write_text("Version,26.1;\n", encoding="utf-8")
    manifest.write_text(
        json.dumps({"energyplus_version": "26.1.0", "period": "smoke"}),
        encoding="utf-8",
    )
    weather.write_text("explicit integration-test weather fixture\n", encoding="utf-8")
    return baseline_model, agent_model, manifest, weather


def _official_results(
    root: Path,
    run_id: str,
    *,
    electricity_kwh: float,
    hvac_kwh: float,
) -> EnergyPlusResults:
    output = root / "runs" / run_id
    output.mkdir(parents=True, exist_ok=True)
    source = output / "eplusout.sql"
    source.write_bytes(b"explicit integration-test official output fixture")
    (output / "eplusout.end").write_text(
        "EnergyPlus Completed Successfully.\n",
        encoding="utf-8",
    )
    (output / "eplusout.err").write_text(
        "Program Version,EnergyPlus, Version 26.1.0\n",
        encoding="utf-8",
    )
    return EnergyPlusResults(
        run_directory=output,
        source_file=source,
        completed=True,
        facility_electricity_kwh=electricity_kwh,
        hvac_electricity_kwh=hvac_kwh,
        other_fuels_kwh={"NaturalGas:Facility": 10.0},
        peak_electrical_demand_kw=50.0,
        warning_count=2,
        severe_count=0,
        fatal_count=0,
        output_rows=96,
    )


def _observation(run_id: str, timestep_key: str) -> ObservationInput:
    return ObservationInput(
        run_id=run_id,
        timestamp=WALL_TIME,
        simulation_timestamp=SIMULATION_START,
        timestep_key=timestep_key,
        environment="RUN PERIOD 1",
        occupied=True,
        occupancy_count=5.0,
        zone_temperature_mean_c=23.0,
        zone_temperature_min_c=23.0,
        zone_temperature_max_c=23.0,
        operative_temperature_mean_c=23.0,
        operative_temperature_min_c=23.0,
        operative_temperature_max_c=23.0,
        heating_setpoint_c=20.0,
        cooling_setpoint_c=24.0,
        facility_demand_kw=50.0,
        timestep_electricity_kwh=1.0,
        cumulative_electricity_kwh=1.0,
        tariff_per_kwh=0.12,
        carbon_kg_per_kwh=0.70,
        actuator_capabilities=ActuatorCapabilities(
            heating_setpoint=True,
            cooling_setpoint=True,
        ),
    )


def _seed_run(
    store: SQLiteStore,
    root: Path,
    *,
    run_id: str,
    run_type: RunType,
    model_path: Path,
    weather_path: Path,
    telemetry_kwh: float,
    with_agent_audit: bool,
    is_fake: bool = False,
) -> None:
    store.create_run(
        run_id,
        run_type,
        is_fake=is_fake,
        energyplus_version="26.1.0",
        model_path=model_path,
        weather_path=weather_path,
        period_name="smoke",
        metadata={"output_directory": str(root / "runs" / run_id)},
        timestamp=WALL_TIME,
    )
    store.set_run_status(run_id, RunStatus.RUNNING, timestamp=WALL_TIME)
    for index, (temperature, pmv, ppd, co2) in enumerate(
        ((21.0, 0.8, 20.0, 900.0), (24.0, 0.5, 10.0, 800.0)),
        start=1,
    ):
        simulation_time = SIMULATION_START + timedelta(minutes=15 * (index - 1))
        timestep_key = f"{run_id}-ts-{index}"
        assert store.record_telemetry(
            BuildingTelemetry(
                run_id=run_id,
                timestamp=WALL_TIME,
                simulation_timestamp=simulation_time,
                timestep_key=timestep_key,
                environment="RUN PERIOD 1",
                outdoor_temperature_c=32.0,
                facility_demand_kw=50.0,
                timestep_electricity_kwh=telemetry_kwh / 2,
                cumulative_electricity_kwh=telemetry_kwh / 2 * index,
                hvac_electricity_kwh=telemetry_kwh * 0.3,
                heating_setpoint_c=20.0,
                cooling_setpoint_c=24.0,
            )
        )
        assert store.record_zone_telemetry(
            ZoneTelemetry(
                run_id=run_id,
                timestamp=WALL_TIME,
                simulation_timestamp=simulation_time,
                timestep_key=timestep_key,
                environment="RUN PERIOD 1",
                zone_name="Office",
                mean_air_temperature_c=temperature,
                operative_temperature_c=temperature,
                relative_humidity_percent=50.0,
                occupant_count=5.0,
                pmv=pmv,
                ppd_percent=ppd,
                co2_ppm=co2,
                heating_setpoint_c=20.0,
                cooling_setpoint_c=24.0,
            )
        )
    if with_agent_audit:
        persisted = store.record_observation(_observation(run_id, f"{run_id}-observation"))
        store.record_tool_call(
            ToolCallTrace(
                call_id=f"{run_id}-tool-1",
                run_id=run_id,
                timestamp=WALL_TIME,
                observation_id=persisted.observation_id,
                sequence=1,
                tool_name="apply_control_action",
                arguments={"run_id": run_id},
                result={"success": True},
                success=True,
                duration_ms=10.0,
                control_affecting=True,
            )
        )
        store.record_agent_decision(
            decision_id=f"{run_id}-decision-1",
            run_id=run_id,
            observation_id=persisted.observation_id,
            model="qwen3:8b",
            latency_ms=100.0,
            completed=True,
            fallback_status="model_timeout",
            timestamp=WALL_TIME,
        )
        proposed = CandidateAction(
            candidate_id="proposed",
            heating_setpoint_c=20.5,
            cooling_setpoint_c=24.0,
            hold_minutes=60,
        )
        applied = proposed.model_copy(update={"heating_setpoint_c": 20.0})
        action = ControlAction(
            action_id=f"{run_id}-action-1",
            run_id=run_id,
            observation_id=persisted.observation_id,
            action_generation=1,
            timestamp=WALL_TIME,
            expires_at=WALL_TIME + timedelta(minutes=60),
            action=proposed,
            model="deterministic-fallback",
            latency_ms=0.0,
            reason_code=ReasonCode.TIMEOUT_FALLBACK,
            explanation="Explicit integration-test fallback.",
            fallback=True,
        )
        validation = ValidationResult(
            run_id=run_id,
            observation_id=persisted.observation_id,
            action_generation=1,
            timestamp=WALL_TIME,
            accepted=True,
            proposed_action=proposed,
            applied_action=applied,
            applied_expires_at=WALL_TIME + timedelta(minutes=60),
            fallback_status="active",
        )
        assert store.apply_validated_action(
            action,
            validation,
            expected_run_id=run_id,
            timestamp=WALL_TIME,
        ).applied
        clamp_payload = [
            ClampDetail(
                code=ValidationCode.RATE_CLAMPED,
                field="heating_setpoint_c",
                proposed_value=20.5,
                applied_value=20.0,
                message="Explicit integration-test clamp.",
            ).model_dump(mode="json")
        ]
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                """
                UPDATE applied_actions
                SET clamp_details_json = ?
                WHERE action_id = ?
                """,
                (json.dumps(clamp_payload), action.action_id),
            )
        invalid_action = action.model_copy(
            update={
                "action_id": f"{run_id}-invalid-2",
                "action_generation": 2,
            }
        )
        invalid_validation = ValidationResult(
            run_id=run_id,
            observation_id=persisted.observation_id,
            action_generation=2,
            timestamp=WALL_TIME,
            accepted=False,
            proposed_action=invalid_action.action,
            applied_action=None,
            issues=(
                ValidationIssue(
                    code=ValidationCode.INVALID_DEADBAND,
                    message="Explicit integration-test rejection.",
                ),
            ),
            fallback_status="rejected",
        )
        assert store.record_proposed_action(invalid_action, invalid_validation)
    store.set_run_status(
        run_id,
        RunStatus.COMPLETED,
        timestamp=WALL_TIME + timedelta(hours=1),
    )


def _finalized_pair(
    root: Path,
) -> tuple[SQLiteStore, FinalizationResult, FinalizationResult]:
    baseline_model, agent_model, _, weather = _assets(root)
    store = SQLiteStore(root / "runs" / "ecoloop.db")
    _seed_run(
        store,
        root,
        run_id="baseline",
        run_type=RunType.BASELINE,
        model_path=baseline_model,
        weather_path=weather,
        telemetry_kwh=100.0,
        with_agent_audit=False,
    )
    _seed_run(
        store,
        root,
        run_id="agent",
        run_type=RunType.AGENT,
        model_path=agent_model,
        weather_path=weather,
        telemetry_kwh=90.0,
        with_agent_audit=True,
    )
    baseline = finalize_run(
        store,
        "baseline",
        _official_results(
            root,
            "baseline",
            electricity_kwh=100.0,
            hvac_kwh=60.0,
        ),
        timestamp=WALL_TIME,
    )
    agent = finalize_run(
        store,
        "agent",
        _official_results(
            root,
            "agent",
            electricity_kwh=90.0,
            hvac_kwh=50.0,
        ),
        timestamp=WALL_TIME,
    )
    return store, baseline, agent


@pytest.mark.integration
def test_finalization_combines_official_telemetry_comfort_and_audit(
    tmp_path: Path,
) -> None:
    store, _, result = _finalized_pair(tmp_path)
    metrics = result.metrics
    assert result.verified_for_comparison
    assert metrics.facility_electricity_kwh == 90.0
    assert metrics.hvac_electricity_kwh == 50.0
    assert metrics.energy_cross_check.passed
    assert metrics.occupied_temperature_violation_percent == 50.0
    assert metrics.occupied_temperature_violation_degree_hours == 0.25
    assert metrics.pmv_compliance_percent == 50.0
    assert metrics.mean_ppd_percent == 15.0
    assert metrics.maximum_occupied_co2_ppm == 900.0
    assert metrics.llm_decision_count == 1
    assert metrics.tool_call_count == 1
    assert metrics.average_decision_latency_ms == 100.0
    assert metrics.p95_decision_latency_ms == 100.0
    assert metrics.timeout_count == 1
    assert metrics.fallback_count == 1
    assert metrics.invalid_action_count == 1
    assert metrics.safety_clamp_count == 1
    assert metrics.warning_count == 2
    assert metrics.fatal_count == 0
    assert result.metrics_json_path.is_file()
    assert result.metrics_csv_path.is_file()
    persisted = store.get_metrics("agent")
    assert persisted["final_run_metrics"]["verified"]
    assert persisted["facility_electricity_kwh"]["value"] == 90.0
    assert load_verified_final_metrics(store, "agent") == metrics


@pytest.mark.integration
def test_comparison_requires_verified_compatible_runs_and_writes_files(
    tmp_path: Path,
) -> None:
    store, _, _ = _finalized_pair(tmp_path)
    output = tmp_path / "explicit-comparison"
    artifacts = compare_and_write(
        store,
        "baseline",
        "agent",
        output_directory=output,
        timestamp=WALL_TIME,
    )
    assert artifacts.comparison.electricity_saving_percent == pytest.approx(10.0)
    assert artifacts.comparison.peak_reduction_percent == 0.0
    assert artifacts.comparison.other_fuels == {"NaturalGas:Facility": (10.0, 10.0)}
    assert artifacts.json_path == output.resolve() / "comparison.json"
    assert artifacts.csv_path == output.resolve() / "comparison.csv"
    assert artifacts.json_path.is_file()
    assert artifacts.csv_path.read_text(encoding="utf-8").startswith("baseline_run_id,")
    assert store.get_metrics("agent")["comparison"]["verified"]


@pytest.mark.integration
def test_failed_energy_crosscheck_is_persisted_but_not_publishable(
    tmp_path: Path,
) -> None:
    baseline_model, _, _, weather = _assets(tmp_path)
    store = SQLiteStore(tmp_path / "runs" / "ecoloop.db")
    _seed_run(
        store,
        tmp_path,
        run_id="bad-crosscheck",
        run_type=RunType.BASELINE,
        model_path=baseline_model,
        weather_path=weather,
        telemetry_kwh=80.0,
        with_agent_audit=False,
    )
    result = finalize_run(
        store,
        "bad-crosscheck",
        _official_results(
            tmp_path,
            "bad-crosscheck",
            electricity_kwh=100.0,
            hvac_kwh=60.0,
        ),
        timestamp=WALL_TIME,
    )
    assert not result.verified_for_comparison
    assert not result.metrics.energy_cross_check.passed
    assert not store.get_metrics("bad-crosscheck")["final_run_metrics"]["verified"]
    with pytest.raises(EvaluationError, match="did not pass verification"):
        load_verified_final_metrics(store, "bad-crosscheck")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("version", "versions do not match"),
        ("weather", "weather files do not match"),
        ("period", "periods do not match"),
        ("preparation", "fingerprints do not match"),
    ],
)
def test_comparison_refuses_mismatched_provenance(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    root = tmp_path / mutation
    store, _, _ = _finalized_pair(root)
    with sqlite3.connect(store.path) as connection:
        if mutation == "version":
            connection.execute(
                "UPDATE runs SET energyplus_version = '25.1.0' WHERE run_id = 'agent'"
            )
        elif mutation == "period":
            connection.execute("UPDATE runs SET period_name = 'different' WHERE run_id = 'agent'")
        elif mutation == "weather":
            weather = root / "weather" / "different.epw"
            weather.write_text("different weather fixture\n", encoding="utf-8")
            connection.execute(
                "UPDATE runs SET weather_path = ? WHERE run_id = 'agent'",
                (str(weather.resolve()),),
            )
        else:
            model_directory = root / "different-preparation"
            model_directory.mkdir()
            model = model_directory / "agent_ready.idf"
            model.write_text("Version,26.1;\n", encoding="utf-8")
            (model_directory / "preparation-manifest.json").write_text(
                '{"different":true}\n',
                encoding="utf-8",
            )
            connection.execute(
                "UPDATE runs SET model_path = ? WHERE run_id = 'agent'",
                (str(model.resolve()),),
            )
    with pytest.raises(EvaluationError, match=message):
        compare_and_write(store, "baseline", "agent")


@pytest.mark.integration
def test_comparison_refuses_fake_and_incomplete_runs(tmp_path: Path) -> None:
    baseline_model, agent_model, _, weather = _assets(tmp_path)
    store = SQLiteStore(tmp_path / "runs" / "ecoloop.db")
    store.create_run(
        "incomplete",
        RunType.BASELINE,
        energyplus_version="26.1.0",
        model_path=baseline_model,
        weather_path=weather,
        period_name="smoke",
    )
    store.create_run(
        "other",
        RunType.AGENT,
        energyplus_version="26.1.0",
        model_path=agent_model,
        weather_path=weather,
        period_name="smoke",
    )
    with pytest.raises(EvaluationError, match="not completed"):
        compare_and_write(store, "incomplete", "other")

    fake_root = tmp_path / "fake"
    fake_baseline_model, fake_agent_model, _, fake_weather = _assets(fake_root)
    fake_store = SQLiteStore(fake_root / "runs" / "ecoloop.db")
    for run_id, run_type, model in (
        ("fake-baseline", RunType.BASELINE, fake_baseline_model),
        ("fake-agent", RunType.AGENT, fake_agent_model),
    ):
        _seed_run(
            fake_store,
            fake_root,
            run_id=run_id,
            run_type=run_type,
            model_path=model,
            weather_path=fake_weather,
            telemetry_kwh=100.0,
            with_agent_audit=False,
            is_fake=True,
        )
        finalize_run(
            fake_store,
            run_id,
            _official_results(
                fake_root,
                run_id,
                electricity_kwh=100.0,
                hvac_kwh=60.0,
            ),
            timestamp=WALL_TIME,
            allow_fake=True,
        )
    with pytest.raises(EvaluationError, match="fake"):
        compare_and_write(fake_store, "fake-baseline", "fake-agent")


@pytest.mark.integration
def test_finalization_refuses_incomplete_official_output(tmp_path: Path) -> None:
    baseline_model, _, _, weather = _assets(tmp_path)
    store = SQLiteStore(tmp_path / "runs" / "ecoloop.db")
    _seed_run(
        store,
        tmp_path,
        run_id="incomplete-official",
        run_type=RunType.BASELINE,
        model_path=baseline_model,
        weather_path=weather,
        telemetry_kwh=100.0,
        with_agent_audit=False,
    )
    results = _official_results(
        tmp_path,
        "incomplete-official",
        electricity_kwh=100.0,
        hvac_kwh=60.0,
    )
    incomplete = replace(
        results,
        completed=False,
        failure_reason="EnergyPlus did not complete",
    )
    with pytest.raises(EvaluationError, match="incomplete"):
        finalize_run(store, "incomplete-official", incomplete)
