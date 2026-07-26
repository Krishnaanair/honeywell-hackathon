"""Render-level coverage for the production Streamlit dashboard."""

from datetime import UTC, datetime, timedelta

from streamlit.testing.v1 import AppTest

from ecoloop.config import get_settings, repository_root
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
