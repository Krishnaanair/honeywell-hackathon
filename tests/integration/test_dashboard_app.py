"""Render-level coverage for the production Streamlit dashboard."""

from datetime import UTC, datetime, timedelta

import pandas as pd
from streamlit.testing.v1 import AppTest

from ecoloop.config import get_settings, repository_root
from ecoloop.dashboard.app import (
    _format_duration_ms,
    _has_verified_evidence,
    _is_completed_controlled_run,
    _mode_badge,
    _preferred_controlled_index,
    _preferred_run_index,
)
from ecoloop.db.store import SQLiteStore
from ecoloop.schemas import BuildingTelemetry, RunStatus, RunType


def _run_app(database, monkeypatch, *, replay_run_id=None):
    monkeypatch.setenv("ECOLOOP_DATABASE_PATH", str(database))
    if replay_run_id is not None:
        monkeypatch.setenv("ECOLOOP_DEMO_REPLAY", "1")
        monkeypatch.setenv("ECOLOOP_DEMO_REPLAY_RUN_ID", replay_run_id)
    get_settings.cache_clear()
    app_path = repository_root() / "src" / "ecoloop" / "dashboard" / "app.py"
    try:
        return AppTest.from_file(str(app_path), default_timeout=20).run()
    finally:
        get_settings.cache_clear()


def _record_telemetry(store, run_id, now, *, steps=1):
    for index in range(1, steps + 1):
        store.record_telemetry(
            BuildingTelemetry(
                run_id=run_id,
                timestamp=now,
                simulation_timestamp=now + timedelta(minutes=15 * index),
                timestep_key=f"{run_id}:summer:07-15:00:{15 * index:02}:1",
                environment="summer",
                outdoor_temperature_c=31.0,
                facility_demand_kw=12.5,
                timestep_electricity_kwh=3.125,
                cumulative_electricity_kwh=3.125 * index,
                hvac_electricity_kwh=2.0,
                heating_setpoint_c=20.0,
                cooling_setpoint_c=24.0,
            )
        )


def test_dashboard_renders_command_center_without_exceptions(
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
    _record_telemetry(store, "dashboard-real-run", now)

    rendered = _run_app(database, monkeypatch)

    assert not rendered.exception
    assert len(rendered.metric) >= 3
    rendered_html = "\n".join(str(item.value) for item in rendered.markdown)
    assert "EcoLoop Control Room" in rendered_html
    assert "LIVE SIMULATION" in rendered_html
    assert "REAL DATABASE RECORD" in rendered_html
    assert "EnergyPlus simulation" in rendered_html
    assert "Live zone state" in rendered_html
    assert "Control actions (MCP)" in rendered_html
    assert "System console" in rendered_html
    assert "Performance vs baseline" in rendered_html
    assert "Closed-loop pipeline" in rendered_html
    # No zone rows and no events exist, so explicit empty states must render.
    rendered_captions = "\n".join(str(item.value) for item in rendered.caption)
    assert "No zone telemetry has been persisted" in rendered_captions
    assert "No events are persisted" in rendered_captions


def test_mode_badges_keep_fail_closed_data_mode_semantics() -> None:
    assert _mode_badge("running", replay_enabled=False, verified_evidence=False) == (
        "LIVE SIMULATION",
        "EnergyPlus telemetry · control loop active",
        "mode-live",
    )
    assert (
        _mode_badge("completed", replay_enabled=True, verified_evidence=True)[0]
        == "VERIFIED RUN REPLAY"
    )
    assert (
        _mode_badge("completed", replay_enabled=False, verified_evidence=True)[0]
        == "EVIDENCE CONSOLE"
    )
    assert (
        _mode_badge("completed", replay_enabled=False, verified_evidence=False)[0]
        == "COMPLETED RUN"
    )
    assert _mode_badge("failed", replay_enabled=False, verified_evidence=False)[0] == "FAILED"


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

    rendered = _run_app(database, monkeypatch, replay_run_id="unverified-baseline")

    assert not rendered.exception
    assert rendered.error
    assert "Replay is restricted" in rendered.error[0].value
    rendered_html = "\n".join(str(item.value) for item in rendered.markdown)
    assert "VERIFIED RUN REPLAY" not in rendered_html
    assert "DISCONNECTED" in rendered_html


def test_dashboard_replay_labels_verified_run_as_replay_not_live(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    store.create_run(
        "agent-verified",
        RunType.AGENT,
        is_fake=False,
        energyplus_version="26.1.0",
        model_path=tmp_path / "agent_ready.idf",
        weather_path=tmp_path / "weather.epw",
        period_name="smoke",
        timestamp=now,
    )
    store.set_run_status("agent-verified", RunStatus.RUNNING, timestamp=now)
    _record_telemetry(store, "agent-verified", now, steps=2)
    for name, payload in (
        ("energy_cross_check", {"passed": True}),
        ("finalization_verification", {"verified_for_comparison": True}),
    ):
        store.upsert_metric(
            "agent-verified",
            name,
            value_json=payload,
            source="test-official",
            verified=True,
            timestamp=now,
        )
    store.set_run_status(
        "agent-verified",
        RunStatus.COMPLETED,
        timestamp=now + timedelta(hours=1),
    )

    rendered = _run_app(database, monkeypatch, replay_run_id="agent-verified")

    assert not rendered.exception
    rendered_html = "\n".join(str(item.value) for item in rendered.markdown)
    assert "VERIFIED RUN REPLAY" in rendered_html
    assert "LIVE SIMULATION" not in rendered_html
    assert "LIVE DATA INGEST" not in rendered_html
