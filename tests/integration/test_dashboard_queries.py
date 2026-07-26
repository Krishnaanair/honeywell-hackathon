"""Integration coverage for dashboard read-only queries."""

from datetime import UTC, datetime, timedelta

from ecoloop.dashboard import queries
from ecoloop.db.store import SQLiteStore
from ecoloop.schemas import (
    BuildingTelemetry,
    RunStatus,
    RunType,
)


def test_dashboard_queries_hide_fake_and_read_real_telemetry(tmp_path):
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    store.create_run(
        "real-run",
        RunType.BASELINE,
        is_fake=False,
        energyplus_version="26.1.0",
        model_path=tmp_path / "building.idf",
        weather_path=tmp_path / "weather.epw",
        period_name="smoke",
        timestamp=now,
    )
    store.set_run_status("real-run", RunStatus.RUNNING, timestamp=now)
    store.create_run(
        "fake-run",
        RunType.BASELINE,
        is_fake=True,
        period_name="smoke",
        timestamp=now,
    )
    store.record_telemetry(
        BuildingTelemetry(
            run_id="real-run",
            timestamp=now,
            simulation_timestamp=now + timedelta(minutes=15),
            timestep_key="real-run:summer:07-15:00:15:1",
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

    runs = queries.list_runs(database)
    assert runs["run_id"].tolist() == ["real-run"]
    telemetry = queries.telemetry(database, "real-run")
    assert telemetry["facility_demand_kw"].tolist() == [12.5]


def test_comparison_status_refuses_incomplete_runs(tmp_path):
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    common = {
        "energyplus_version": "26.1.0",
        "model_path": tmp_path / "building.idf",
        "weather_path": tmp_path / "weather.epw",
        "period_name": "smoke",
        "timestamp": now,
    }
    store.create_run("baseline", RunType.BASELINE, **common)
    store.create_run("agent", RunType.AGENT, **common)

    allowed, message = queries.compare_status(database, "baseline", "agent")
    assert not allowed
    assert "complete" in message.lower()


def test_comparison_status_accepts_distinct_prepared_models_with_same_fingerprint(
    tmp_path,
):
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    common = {
        "energyplus_version": "26.1.0",
        "weather_path": tmp_path / "weather.epw",
        "period_name": "smoke",
        "metadata": {"preparation_fingerprint": "a" * 64},
        "timestamp": now,
    }
    store.create_run(
        "baseline",
        RunType.BASELINE,
        model_path=tmp_path / "baseline.idf",
        **common,
    )
    store.create_run(
        "agent",
        RunType.AGENT,
        model_path=tmp_path / "agent_ready.idf",
        **common,
    )
    for run_id in ("baseline", "agent"):
        store.set_run_status(run_id, RunStatus.RUNNING, timestamp=now)
        store.set_run_status(
            run_id,
            RunStatus.COMPLETED,
            timestamp=now + timedelta(minutes=1),
        )
        store.upsert_metric(
            run_id,
            "final_run_metrics",
            value_json={"run_id": run_id},
            source="test-official",
            verified=True,
            timestamp=now,
        )

    allowed, message = queries.compare_status(database, "baseline", "agent")
    assert allowed
    assert "compatible" in message.lower()
