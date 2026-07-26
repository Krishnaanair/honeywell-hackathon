from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from ecoloop.config import Settings, load_file_config
from ecoloop.coordinator import CoordinatorConfig, run_supervisory_simulation
from ecoloop.db.store import SQLiteStore
from ecoloop.energyplus.discovery import discover_energyplus
from ecoloop.energyplus.model import prepare_models
from ecoloop.energyplus.results import parse_results
from ecoloop.energyplus.runtime import SimulationMode, SimulationRequest, run_simulation


def _real_assets() -> tuple[Settings, object]:
    settings = Settings()
    installation = discover_energyplus(settings)
    reasons: list[str] = []
    if installation is None:
        reasons.append("EnergyPlus 26.1.0 was not discovered")
    elif not installation.is_runtime_complete:
        reasons.append("EnergyPlus 26.1.0 Runtime API installation is incomplete")
    elif not installation.is_model_tooling_complete:
        reasons.append("ConvertInputFormat/schema is unavailable")
    if not settings.resolved_model_path().is_file():
        reasons.append(f"model is missing: {settings.resolved_model_path()}")
    if not settings.resolved_weather_path().is_file():
        reasons.append(f"weather is missing: {settings.resolved_weather_path()}")
    if reasons:
        pytest.skip("; ".join(reasons))
    return settings, installation


@pytest.mark.real_energyplus
def test_real_model_preparation_is_byte_stable() -> None:
    settings, _ = _real_assets()
    period = load_file_config().periods["smoke"]
    first = prepare_models(settings, period)
    first_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (first.baseline_model, first.agent_ready_model)
    }
    second = prepare_models(settings, period)
    second_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (second.baseline_model, second.agent_ready_model)
    }
    assert first_hashes == second_hashes


@pytest.mark.real_energyplus
def test_real_one_day_runtime_api_baseline(tmp_path: Path) -> None:
    settings, installation = _real_assets()
    artifacts = prepare_models(settings, load_file_config().periods["smoke"])
    output = tmp_path / "baseline-output"
    result = run_simulation(
        SimulationRequest(
            run_id="real-baseline-smoke",
            model_path=artifacts.baseline_model,
            weather_path=settings.resolved_weather_path(),
            output_directory=output,
            database_path=tmp_path / "baseline.db",
            mode=SimulationMode.BASELINE,
            energyplus_home=installation.home,
        )
    )
    assert result.completed, result.error
    assert result.observation_count == 96
    official = parse_results(output)
    assert official.completed
    assert official.facility_electricity_kwh is not None
    assert official.facility_electricity_kwh > 0
    assert official.other_fuels_kwh == {}
    assert official.warning_count == 0

    connection = sqlite3.connect(tmp_path / "baseline.db")
    pmv_count, ppd_count = connection.execute(
        """
        SELECT COUNT(pmv), COUNT(ppd_percent)
        FROM zone_telemetry
        WHERE run_id = ?
        """,
        ("real-baseline-smoke",),
    ).fetchone()
    connection.close()
    assert int(pmv_count) > 0
    assert int(ppd_count) > 0


@pytest.mark.real_energyplus
def test_real_fixed_override_changes_reported_setpoints(tmp_path: Path) -> None:
    settings, installation = _real_assets()
    artifacts = prepare_models(settings, load_file_config().periods["smoke"])
    output = tmp_path / "fixed-output"
    database = tmp_path / "fixed.db"
    baseline_output = tmp_path / "proof-baseline-output"
    baseline_result = run_simulation(
        SimulationRequest(
            run_id="real-fixed-proof-baseline",
            model_path=artifacts.baseline_model,
            weather_path=settings.resolved_weather_path(),
            output_directory=baseline_output,
            database_path=database,
            mode=SimulationMode.BASELINE,
            energyplus_home=installation.home,
        )
    )
    assert baseline_result.completed, baseline_result.error
    result = run_simulation(
        SimulationRequest(
            run_id="real-fixed-override",
            model_path=artifacts.agent_ready_model,
            weather_path=settings.resolved_weather_path(),
            output_directory=output,
            database_path=database,
            mode=SimulationMode.FIXED_OVERRIDE,
            energyplus_home=installation.home,
            actuator_map_path=artifacts.actuator_map,
            fixed_heating_setpoint_c=21.0,
            fixed_cooling_setpoint_c=25.0,
        )
    )
    assert result.completed, result.error
    assert result.applied_action_count > 0
    baseline_official = parse_results(baseline_output)
    fixed_official = parse_results(output)
    assert baseline_official.hvac_electricity_kwh is not None
    assert fixed_official.hvac_electricity_kwh is not None
    assert abs(baseline_official.hvac_electricity_kwh - fixed_official.hvac_electricity_kwh) > 0.01

    connection = sqlite3.connect(database)
    rows = connection.execute(
        """
        SELECT heating_setpoint_c, cooling_setpoint_c
        FROM zone_telemetry
        WHERE run_id = ?
        """,
        ("real-fixed-override",),
    ).fetchall()
    connection.close()
    assert rows
    assert any(
        heating is not None
        and cooling is not None
        and abs(float(heating) - 21.0) < 0.05
        and abs(float(cooling) - 25.0) < 0.05
        for heating, cooling in rows
    )


@pytest.mark.real_energyplus
def test_real_one_day_rule_controller_closes_action_handshake(
    tmp_path: Path,
) -> None:
    settings, installation = _real_assets()
    artifacts = prepare_models(settings, load_file_config().periods["smoke"])
    database = tmp_path / "rule.db"
    result = run_supervisory_simulation(
        SimulationRequest(
            run_id="real-rule-smoke",
            model_path=artifacts.agent_ready_model,
            weather_path=settings.resolved_weather_path(),
            output_directory=tmp_path / "rule-output",
            database_path=database,
            mode=SimulationMode.RULE,
            energyplus_home=installation.home,
            actuator_map_path=artifacts.actuator_map,
            decision_poll_seconds=0.01,
        ),
        settings=settings,
        config=CoordinatorConfig(
            observation_poll_seconds=0.01,
            overall_timeout_seconds=180.0,
            rule_decision_wait_seconds=2.0,
            terminal_settle_seconds=0.1,
        ),
    )

    assert result.completed, result.simulation.error
    assert result.decisions
    assert result.successful_decisions > 0
    assert result.simulation.applied_action_count > 0
    applied = SQLiteStore(database).get_applied_actions("real-rule-smoke")
    assert applied
    assert all(action.model == "deterministic-fallback" for action, _ in applied)
    connection = sqlite3.connect(database)
    timeout_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM simulation_messages
            WHERE lower(message) LIKE '%bounded decision wait timed out%'
            """
        ).fetchone()[0]
    )
    error_count = int(connection.execute("SELECT COUNT(*) FROM errors").fetchone()[0])
    connection.close()
    assert timeout_count == 0
    assert error_count == 0
