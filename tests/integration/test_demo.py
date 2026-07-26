"""Managed demo orchestration helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoloop.config import Settings
from ecoloop.db.store import SQLiteStore
from ecoloop.demo import _find_reusable_baseline, _write_current_run
from ecoloop.schemas import (
    EnergyCrossCheck,
    FinalRunMetrics,
    RunStatus,
    RunType,
)


def _settings(tmp_path: Path) -> Settings:
    weather = tmp_path / "weather.epw"
    weather.write_text("weather", encoding="utf-8")
    return Settings(
        ECOLOOP_DATABASE_PATH=tmp_path / "ecoloop.db",
        ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ECOLOOP_WEATHER_PATH=weather,
    )


def test_current_run_file_is_replaced_atomically(tmp_path: Path) -> None:
    destination = _write_current_run(tmp_path, "agent-run-2")

    assert destination.read_text(encoding="utf-8") == "agent-run-2\n"
    assert list(tmp_path.glob(".current-run-*.tmp")) == []


def test_reusable_baseline_requires_verified_canonical_metrics(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SQLiteStore(settings.resolved_database_path())
    store.create_run(
        "baseline-unverified",
        RunType.BASELINE,
        is_fake=False,
        energyplus_version="26.1.0",
        weather_path=settings.resolved_weather_path(),
        period_name="demo",
    )
    store.set_run_status("baseline-unverified", RunStatus.RUNNING)
    store.set_run_status("baseline-unverified", RunStatus.COMPLETED)

    assert _find_reusable_baseline(store, settings, "demo") is None


def test_reusable_baseline_accepts_only_matching_verified_real_run(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SQLiteStore(settings.resolved_database_path())
    store.create_run(
        "baseline-verified",
        RunType.BASELINE,
        is_fake=False,
        energyplus_version="26.1.0",
        weather_path=settings.resolved_weather_path(),
        period_name="demo",
    )
    store.set_run_status("baseline-verified", RunStatus.RUNNING)
    store.set_run_status("baseline-verified", RunStatus.COMPLETED)
    now = datetime.now(UTC)
    metrics = FinalRunMetrics(
        run_id="baseline-verified",
        timestamp=now,
        run_type=RunType.BASELINE,
        status=RunStatus.COMPLETED,
        is_fake=False,
        simulation_start=datetime(2024, 7, 15, tzinfo=UTC),
        simulation_end=datetime(2024, 7, 18, tzinfo=UTC),
        facility_electricity_kwh=100.0,
        hvac_electricity_kwh=50.0,
        peak_electrical_demand_kw=20.0,
        cost=12.0,
        operational_carbon_kg=70.0,
        occupied_temperature_violation_percent=0.0,
        occupied_temperature_violation_degree_hours=0.0,
        pmv_compliance_percent=100.0,
        mean_ppd_percent=5.0,
        llm_decision_count=0,
        tool_call_count=0,
        timeout_count=0,
        fallback_count=0,
        invalid_action_count=0,
        safety_clamp_count=0,
        warning_count=0,
        severe_count=0,
        fatal_count=0,
        energy_cross_check=EnergyCrossCheck(
            official_kwh=100.0,
            telemetry_kwh=100.0,
            absolute_difference_kwh=0.0,
            difference_percent=0.0,
            tolerance_percent=2.0,
            passed=True,
        ),
        source_artifacts=("eplusout.sql",),
    )
    store.upsert_metric(
        "baseline-verified",
        "final_run_metrics",
        value_json=metrics.model_dump(mode="json"),
        source="test",
        verified=True,
    )

    selected = _find_reusable_baseline(store, settings, "demo")

    assert selected is not None
    assert selected.run_id == "baseline-verified"


def test_demo_rejects_invalid_port() -> None:
    from ecoloop.demo import run_demo

    with pytest.raises(ValueError, match="dashboard_port"):
        run_demo(dashboard_port=0)
