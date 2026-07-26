"""Integration coverage for dashboard read-only queries."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ecoloop.dashboard import queries
from ecoloop.db.store import SQLiteStore
from ecoloop.schemas import (
    BuildingTelemetry,
    RunStatus,
    RunType,
    ZoneTelemetry,
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
        "metadata": {
            "preparation_fingerprint": "a" * 64,
            "weather_sha256": "b" * 64,
        },
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
            value_json={
                "run_id": run_id,
                "simulation_start": now.isoformat(),
                "simulation_end": (now + timedelta(days=1)).isoformat(),
            },
            source="test-official",
            verified=True,
            timestamp=now,
        )
        store.upsert_metric(
            run_id,
            "energy_cross_check",
            value_json={"passed": True},
            source="test-official",
            verified=True,
            timestamp=now,
        )
        store.upsert_metric(
            run_id,
            "finalization_verification",
            value_json={"verified_for_comparison": True},
            source="test-official",
            verified=True,
            timestamp=now,
        )

    allowed, message = queries.compare_status(database, "baseline", "agent")
    assert allowed
    assert "compatible" in message.lower()

    store.upsert_metric(
        "agent",
        "final_run_metrics",
        value_json={
            "run_id": "agent",
            "simulation_start": now.isoformat(),
            "simulation_end": (now + timedelta(days=1, minutes=15)).isoformat(),
        },
        source="test-window-mismatch",
        verified=True,
        timestamp=now,
    )
    allowed, message = queries.compare_status(database, "baseline", "agent")
    assert not allowed
    assert "simulation windows" in message.lower()


def test_comparison_status_requires_matching_weather_checksum_and_publication_checks(
    tmp_path,
):
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    common = {
        "energyplus_version": "26.1.0",
        "weather_path": tmp_path / "weather.epw",
        "period_name": "smoke",
        "timestamp": now,
    }
    for run_id, run_type, weather_sha in (
        ("baseline", RunType.BASELINE, "b" * 64),
        ("agent", RunType.AGENT, "c" * 64),
    ):
        store.create_run(
            run_id,
            run_type,
            model_path=tmp_path / f"{run_id}.idf",
            metadata={
                "preparation_fingerprint": "a" * 64,
                "weather_sha256": weather_sha,
            },
            **common,
        )
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
    assert not allowed
    assert "weather-content checksum" in message.lower()

    store.create_run(
        "agent-unverified",
        RunType.AGENT,
        energyplus_version="26.1.0",
        model_path=tmp_path / "agent-unverified.idf",
        weather_path=tmp_path / "weather.epw",
        period_name="smoke",
        metadata={
            "preparation_fingerprint": "a" * 64,
            "weather_sha256": "b" * 64,
        },
        timestamp=now,
    )
    store.set_run_status("agent-unverified", RunStatus.RUNNING, timestamp=now)
    store.set_run_status(
        "agent-unverified",
        RunStatus.COMPLETED,
        timestamp=now + timedelta(minutes=1),
    )
    store.upsert_metric(
        "agent-unverified",
        "final_run_metrics",
        value_json={"run_id": "agent-unverified"},
        source="test-official",
        verified=True,
        timestamp=now,
    )

    allowed, message = queries.compare_status(database, "baseline", "agent-unverified")
    assert not allowed
    assert "energy cross-check" in message.lower()


def _seed_statistics_run(tmp_path):
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    metadata = {
        "data_origin": "external_pyenergyplus_runtime_api",
        "preparation_fingerprint": "a" * 64,
        "weather_sha256": "b" * 64,
        "input_model_sha256": "c" * 64,
    }
    store.create_run(
        "agent-real",
        RunType.AGENT,
        energyplus_version="26.1.0",
        model_path=tmp_path / "agent_ready.idf",
        weather_path=tmp_path / "weather.epw",
        period_name="smoke",
        metadata=metadata,
        timestamp=now,
    )
    store.set_run_status("agent-real", RunStatus.RUNNING, timestamp=now)
    for index, values in enumerate(
        (
            (10.0, 2.0, 20.0, 24.0),
            (12.0, 5.0, 19.0, 25.0),
        ),
        start=1,
    ):
        simulation_time = now + timedelta(minutes=15 * index)
        demand, cumulative, heating, cooling = values
        timestep_key = f"agent-real:summer:07-15:00:{15 * index:02}:1"
        store.record_telemetry(
            BuildingTelemetry(
                run_id="agent-real",
                timestamp=now,
                simulation_timestamp=simulation_time,
                timestep_key=timestep_key,
                environment="summer",
                outdoor_temperature_c=30.0 + index,
                facility_demand_kw=demand,
                timestep_electricity_kwh=cumulative - (2.0 if index == 2 else 0.0),
                cumulative_electricity_kwh=cumulative,
                hvac_electricity_kwh=1.0 + index,
                heating_setpoint_c=heating,
                cooling_setpoint_c=cooling,
            )
        )
        store.record_zone_telemetry(
            ZoneTelemetry(
                run_id="agent-real",
                timestamp=now,
                simulation_timestamp=simulation_time,
                timestep_key=timestep_key,
                environment="summer",
                zone_name="Zone A",
                mean_air_temperature_c=22.0 + index,
                operative_temperature_c=21.0 + 2 * index,
                relative_humidity_percent=40.0 + 10 * index,
                occupant_count=float(index),
                pmv=0.2 if index == 1 else 0.8,
                ppd_percent=6.0 if index == 1 else 22.0,
                co2_ppm=500.0 + 300 * index,
                heating_setpoint_c=heating,
                cooling_setpoint_c=cooling,
            )
        )
        store.record_zone_telemetry(
            ZoneTelemetry(
                run_id="agent-real",
                timestamp=now,
                simulation_timestamp=simulation_time,
                timestep_key=timestep_key,
                environment="summer",
                zone_name="Zone B",
                mean_air_temperature_c=20.0 + index,
                operative_temperature_c=20.0 + index,
                relative_humidity_percent=45.0,
                occupant_count=0.0,
                heating_setpoint_c=heating,
                cooling_setpoint_c=cooling,
            )
        )

    timestamp = now.isoformat().replace("+00:00", "Z")
    with sqlite3.connect(database) as connection:
        observation_id = connection.execute(
            """
            INSERT INTO observations (
                run_id, timestamp, simulation_timestamp, timestep_key,
                environment, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "agent-real",
                timestamp,
                timestamp,
                "agent-real:observation:1",
                "summer",
                "{}",
            ),
        ).lastrowid
        assert observation_id is not None
        action = {
            "candidate_id": "candidate-1",
            "heating_setpoint_c": 21.0,
            "cooling_setpoint_c": 24.0,
            "hold_minutes": 60,
        }
        action_json = json.dumps(action, sort_keys=True)
        for generation in (1, 2):
            connection.execute(
                """
                INSERT INTO proposed_actions (
                    action_id, run_id, timestamp, observation_id, action_generation,
                    proposed_values_json, applied_values_json, validation_result_json,
                    clamp_details_json, expiry, model, latency_ms, reason_code,
                    explanation, fallback_status, cache_hit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"action-{generation}",
                    "agent-real",
                    timestamp,
                    observation_id,
                    generation,
                    action_json,
                    action_json,
                    "{}",
                    "[]" if generation == 2 else '[{"field":"heating_setpoint_c"}]',
                    timestamp,
                    "qwen3:8b",
                    100.0 * generation,
                    "energy_optimization",
                    "Selected a bounded candidate.",
                    "active" if generation == 1 else None,
                    0,
                ),
            )
        connection.execute(
            """
            INSERT INTO applied_actions (
                action_id, run_id, timestamp, observation_id, action_generation,
                proposed_values_json, applied_values_json, validation_result_json,
                clamp_details_json, expiry, model, latency_ms, reason_code,
                explanation, fallback_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "action-1",
                "agent-real",
                timestamp,
                observation_id,
                1,
                action_json,
                action_json,
                "{}",
                '[{"field":"heating_setpoint_c"}]',
                timestamp,
                "qwen3:8b",
                100.0,
                "energy_optimization",
                "Selected a bounded candidate.",
                "active",
            ),
        )
        for index, completed in enumerate((1, 0), start=1):
            connection.execute(
                """
                INSERT INTO agent_decisions (
                    decision_id, run_id, timestamp, observation_id, action_generation,
                    model, latency_ms, reason_code, explanation, fallback_status,
                    candidate_scores_json, state_summary_json, completed, timeout_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"decision-{index}",
                    "agent-real",
                    timestamp,
                    observation_id,
                    index if completed else None,
                    "qwen3:8b",
                    100.0 * index,
                    "energy_optimization" if completed else None,
                    "Selected a bounded candidate." if completed else None,
                    None if completed else "decision_timeout",
                    "[]",
                    "{}",
                    completed,
                    0 if completed else 1,
                ),
            )
        for index, success in enumerate((1, 1, 0), start=1):
            connection.execute(
                """
                INSERT INTO tool_calls (
                    call_id, run_id, timestamp, observation_id, sequence,
                    tool_name, arguments_json, result_json, success, error,
                    duration_ms, control_affecting
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"tool-{index}",
                    "agent-real",
                    timestamp,
                    observation_id,
                    index,
                    "apply_control_action" if index == 2 else "get_constraints",
                    "{}",
                    "{}" if success else None,
                    success,
                    None if success else "bounded failure",
                    10.0 * index,
                    1 if index == 2 else 0,
                ),
            )

    scalar_metrics = {
        "facility_electricity_kwh": (120.0, "kWh"),
        "hvac_electricity_kwh": (50.0, "kWh"),
        "tool_call_count": (3.0, "count"),
        "timeout_count": (1.0, "count"),
        "fallback_count": (1.0, "count"),
        "safety_clamp_count": (1.0, "count"),
        "invalid_action_count": (2.0, "count"),
        "average_decision_latency_ms": (150.0, "ms"),
        "p95_decision_latency_ms": (195.0, "ms"),
    }
    for name, (value, units) in scalar_metrics.items():
        store.upsert_metric(
            "agent-real",
            name,
            value=value,
            units=units,
            source="verified-test",
            verified=True,
            timestamp=now,
        )
    for name, payload in (
        (
            "final_run_metrics",
            {
                "run_id": "agent-real",
                "simulation_start": now.isoformat(),
                "simulation_end": (now + timedelta(minutes=30)).isoformat(),
            },
        ),
        ("energy_cross_check", {"passed": True}),
        ("finalization_verification", {"verified_for_comparison": True}),
    ):
        store.upsert_metric(
            "agent-real",
            name,
            value_json=payload,
            source="verified-test",
            verified=True,
            timestamp=now,
        )
    store.set_run_status(
        "agent-real",
        RunStatus.COMPLETED,
        timestamp=now + timedelta(hours=1),
    )
    return database, store, now, metadata


def test_run_statistics_reports_real_persisted_control_and_coverage(tmp_path):
    database, _, _, _ = _seed_statistics_run(tmp_path)

    statistics = queries.run_statistics(database, "agent-real")

    assert statistics["telemetry_steps"] == 2
    assert statistics["zone_rows"] == 4
    assert statistics["distinct_zones"] == 2
    assert statistics["zone_row_completeness_percent"] == 100.0
    assert statistics["latest_facility_demand_kw"] == 12.0
    assert statistics["latest_occupancy_count"] == 2.0
    assert statistics["heating_setpoint"] == {
        "minimum": 19.0,
        "mean": 19.5,
        "maximum": 20.0,
        "latest": 19.0,
    }
    assert statistics["applied_heating_setpoint"]["mean"] == 21.0
    assert statistics["proposed_action_count"] == 2
    assert statistics["applied_action_count"] == 1
    assert statistics["action_application_percent"] == 50.0
    assert statistics["decision_count"] == 2
    assert statistics["decision_completion_percent"] == 50.0
    assert statistics["tool_call_count"] == 3
    assert statistics["failed_tool_call_count"] == 1
    assert statistics["control_affecting_tool_call_count"] == 1
    assert statistics["timeout_count"] == 1
    assert statistics["fallback_count"] == 1
    assert statistics["safety_clamp_count"] == 1
    assert statistics["invalid_action_count"] == 2
    assert statistics["p95_decision_latency_ms"] == 195.0
    assert statistics["comfort_field_coverage_percent"]["co2_ppm"] == 50.0
    assert statistics["energy_cross_check_passed"] is True
    assert statistics["finalization_verified_for_comparison"] is True
    assert statistics["data_origin"] == "external_pyenergyplus_runtime_api"
    assert statistics["preparation_fingerprint"] == "a" * 64


def test_comfort_distribution_uses_occupied_zone_samples(tmp_path):
    database, _, _, _ = _seed_statistics_run(tmp_path)

    distribution = queries.comfort_distribution(database, "agent-real")
    replay_distribution = queries.comfort_distribution(
        database,
        "agent-real",
        simulation_end=datetime(2026, 7, 15, 0, 15, tzinfo=UTC),
    )

    assert distribution["zone_name"].tolist() == ["Zone A", "Zone B"]
    zone_a = distribution.iloc[0]
    zone_b = distribution.iloc[1]
    assert zone_a["samples"] == 2
    assert zone_a["occupied_samples"] == 2
    assert zone_a["operative_temperature_mean_c"] == 24.0
    assert zone_a["occupied_pmv_compliance_percent"] == 50.0
    assert zone_a["occupied_mean_ppd_percent"] == 14.0
    assert zone_a["co2_coverage_percent"] == 100.0
    assert zone_b["occupied_samples"] == 0
    assert zone_b["co2_coverage_percent"] == 0.0
    assert replay_distribution["samples"].tolist() == [1, 1]
    assert replay_distribution["occupied_samples"].tolist() == [1, 0]


def test_comparison_statistics_requires_compatible_verified_real_runs(tmp_path):
    database, store, now, metadata = _seed_statistics_run(tmp_path)
    store.create_run(
        "baseline-real",
        RunType.BASELINE,
        energyplus_version="26.1.0",
        model_path=tmp_path / "baseline.idf",
        weather_path=tmp_path / "weather.epw",
        period_name="smoke",
        metadata=metadata,
        timestamp=now,
    )
    store.set_run_status("baseline-real", RunStatus.RUNNING, timestamp=now)
    for name, value, units in (
        ("facility_electricity_kwh", 100.0, "kWh"),
        ("hvac_electricity_kwh", 40.0, "kWh"),
    ):
        store.upsert_metric(
            "baseline-real",
            name,
            value=value,
            units=units,
            source="verified-test",
            verified=True,
            timestamp=now,
        )
    for name, payload in (
        (
            "final_run_metrics",
            {
                "run_id": "baseline-real",
                "simulation_start": now.isoformat(),
                "simulation_end": (now + timedelta(minutes=30)).isoformat(),
            },
        ),
        ("energy_cross_check", {"passed": True}),
        ("finalization_verification", {"verified_for_comparison": True}),
    ):
        store.upsert_metric(
            "baseline-real",
            name,
            value_json=payload,
            source="verified-test",
            verified=True,
            timestamp=now,
        )
    store.set_run_status(
        "baseline-real",
        RunStatus.COMPLETED,
        timestamp=now + timedelta(hours=1),
    )

    comparison = queries.comparison_statistics(database, "baseline-real", "agent-real")

    assert comparison["compatible"] is True
    assert comparison["preparation_fingerprint_match"] is True
    assert comparison["weather_sha256_match"] is True
    assert comparison["energy_cross_checks_passed"] is True
    electricity = comparison["metrics"]["facility_electricity_kwh"]
    assert electricity == {
        "baseline": 100.0,
        "controlled": 120.0,
        "absolute_delta": 20.0,
        "percent_delta": 20.0,
        "units": "kWh",
        "lower_is_better": True,
    }


def test_statistics_reject_fake_runs(tmp_path):
    database = tmp_path / "ecoloop.db"
    store = SQLiteStore(database)
    store.create_run("fake-run", RunType.AGENT, is_fake=True)

    with pytest.raises(queries.DashboardDataError, match="Fake runs"):
        queries.run_statistics(database, "fake-run")
    with pytest.raises(queries.DashboardDataError, match="Fake runs"):
        queries.comfort_distribution(database, "fake-run")
