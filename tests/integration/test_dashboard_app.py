"""Render-level coverage for the production Streamlit dashboard."""

from datetime import UTC, datetime, timedelta

import pandas as pd
from streamlit.testing.v1 import AppTest

from ecoloop.config import get_settings, repository_root
from ecoloop.dashboard.app import (
    _diagnostic_groups,
    _format_duration_ms,
    _has_verified_evidence,
    _is_completed_controlled_run,
    _preferred_controlled_index,
    _preferred_run_index,
)
from ecoloop.db.store import SQLiteStore
from ecoloop.schemas import BuildingTelemetry, RunStatus, RunType


def test_dashboard_renders_all_production_tabs_without_exceptions(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    store.create_run(
        "dashboard-real-run",
        RunType.BASELINE,
        is_fake=False,
        energyplus_version="26.1.0",
        model_path=tmp_path / "building.idf",
        weather_path=tmp_path / "weather.epw",
        period_name="smoke",
        timestamp=now,
    )
    store.set_run_status("dashboard-real-run", RunStatus.RUNNING, timestamp=now)
    store.record_telemetry(
        BuildingTelemetry(
            run_id="dashboard-real-run",
            timestamp=now,
            simulation_timestamp=now + timedelta(minutes=15),
            timestep_key="dashboard-real-run:summer:07-15:00:15:1",
            environment="summer",
            outdoor_temperature_c=31.0,
            facility_demand_kw=12.5,
            timestep_electricity_kwh=3.125,
            cumulative_electricity_kwh=3.125,
            hvac_electricity_kwh=2.0,
            heating_setpoint_c=20.0,
            cooling_setpoint_c=24.0,
        )
    )

    monkeypatch.setenv("ECOLOOP_DATABASE_PATH", str(database))
    get_settings.cache_clear()
    app_path = repository_root() / "src" / "ecoloop" / "dashboard" / "app.py"
    try:
        rendered = AppTest.from_file(str(app_path), default_timeout=20).run()
    finally:
        get_settings.cache_clear()

    assert not rendered.exception
    assert [tab.label for tab in rendered.tabs] == [
        "Live Operations",
        "Baseline vs Agent",
        "Comfort and IAQ",
        "Agent Decisions",
        "Reliability and Errors",
        "Methodology",
    ]
    assert len(rendered.metric) >= 9
    rendered_html = "\n".join(str(item.value) for item in rendered.markdown)
    assert "EcoLoop Control Room" in rendered_html
    assert "LIVE SIMULATION" in rendered_html
    assert "REAL DATABASE RECORD" in rendered_html
    assert "PHYSICAL STATE" in rendered_html
    assert "HOW TO READ THE EVIDENCE" in rendered_html


def test_dashboard_prefers_agent_evidence_and_formats_long_latency() -> None:
    controlled = pd.DataFrame(
        [
            {"run_id": "replay-newest", "run_type": "replay"},
            {"run_id": "agent-week", "run_type": "agent"},
            {"run_id": "rule-smoke", "run_type": "rule"},
        ]
    )

    assert _preferred_controlled_index(controlled) == 1
    assert _format_duration_ms(14_672.0137) == "14.67 s"
    assert _format_duration_ms(128.84) == "128.84 ms"


def test_dashboard_prefers_current_real_run_then_completed_agent() -> None:
    runs = pd.DataFrame(
        [
            {
                "run_id": "rule-newest",
                "run_type": "rule",
                "status": "completed",
                "is_fake": 0,
            },
            {
                "run_id": "agent-completed",
                "run_type": "agent",
                "status": "completed",
                "is_fake": 0,
            },
            {
                "run_id": "agent-fake",
                "run_type": "agent",
                "status": "completed",
                "is_fake": 1,
            },
        ]
    )

    assert _preferred_run_index(runs, "rule-newest") == 0
    assert _preferred_run_index(runs, "agent-fake") == 1
    assert _preferred_run_index(runs, "stale-run-id") == 1


def test_dashboard_replay_evidence_checks_fail_closed() -> None:
    verified = {
        "finalization_verification": {"structured_value": {"verified_for_comparison": True}},
        "energy_cross_check": {"structured_value": {"passed": True}},
    }
    missing_cross_check = {
        "finalization_verification": {"structured_value": {"verified_for_comparison": True}}
    }

    assert _has_verified_evidence(verified)
    assert not _has_verified_evidence(missing_cross_check)
    assert _is_completed_controlled_run(
        {
            "run_type": "agent",
            "status": "completed",
            "is_fake": 0,
        }
    )
    assert not _is_completed_controlled_run(
        {
            "run_type": "baseline",
            "status": "completed",
            "is_fake": 0,
        }
    )
    assert not _is_completed_controlled_run(
        {
            "run_type": "agent",
            "status": "completed",
            "is_fake": 1,
        }
    )


def test_dashboard_replay_rejects_unverified_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    store.create_run(
        "unverified-baseline",
        RunType.BASELINE,
        is_fake=False,
        energyplus_version="26.1.0",
        model_path=tmp_path / "building.idf",
        weather_path=tmp_path / "weather.epw",
        period_name="smoke",
        timestamp=now,
    )
    store.set_run_status("unverified-baseline", RunStatus.RUNNING, timestamp=now)
    store.set_run_status("unverified-baseline", RunStatus.COMPLETED, timestamp=now)

    monkeypatch.setenv("ECOLOOP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ECOLOOP_DEMO_REPLAY", "1")
    monkeypatch.setenv("ECOLOOP_DEMO_REPLAY_RUN_ID", "unverified-baseline")
    get_settings.cache_clear()
    app_path = repository_root() / "src" / "ecoloop" / "dashboard" / "app.py"
    try:
        rendered = AppTest.from_file(str(app_path), default_timeout=20).run()
    finally:
        get_settings.cache_clear()

    assert not rendered.exception
    assert rendered.error
    assert "Replay is restricted" in rendered.error[0].value
    rendered_html = "\n".join(str(item.value) for item in rendered.markdown)
    assert "REAL RUN REPLAY" not in rendered_html


def test_dashboard_separates_energyplus_and_controller_diagnostics() -> None:
    errors = pd.DataFrame(
        [
            {
                "severity": "severe",
                "source": "coordinator",
                "occurrence_count": 3,
            },
            {
                "severity": "warning",
                "source": "energyplus-message",
                "occurrence_count": 1,
            },
            {
                "severity": "information",
                "source": "energyplus-message",
                "occurrence_count": 1,
            },
        ]
    )

    energyplus, controller = _diagnostic_groups(errors)

    assert energyplus["severity"].tolist() == ["warning"]
    assert controller["severity"].tolist() == ["severe"]
