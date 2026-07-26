from __future__ import annotations

from pathlib import Path

from ecoloop.energyplus.store_adapter import SQLiteRuntimeStoreAdapter
from ecoloop.schemas import RunStatus


def _observation_values() -> dict[str, object]:
    return {
        "environment": 3,
        "outdoor_drybulb_c": 32.0,
        "facility_demand_w": 20_000.0,
        "timestep_energy_kwh": 5.0,
        "cumulative_energy_kwh": 10.0,
        "timestep_hvac_energy_kwh": 3.0,
        "zone_temperature": {"mean": 24.0, "min": 23.0, "max": 25.0},
        "operative_temperature": {"mean": 24.1, "min": 23.1, "max": 25.1},
        "relative_humidity": {"mean": 50.0, "min": 45.0, "max": 55.0},
        "heating_setpoint": {"mean": 20.0, "min": 20.0, "max": 20.0},
        "cooling_setpoint": {"mean": 24.0, "min": 24.0, "max": 24.0},
        "occupancy_count": 20.0,
        "occupied": True,
    }


def test_sqlite_runtime_adapter_maps_telemetry_and_observation(tmp_path: Path) -> None:
    database = tmp_path / "ecoloop.db"
    adapter = SQLiteRuntimeStoreAdapter(database, "run-1")
    adapter.create_run(
        "run-1",
        {
            "mode": "baseline",
            "energyplus_version": "26.1.0",
            "model_path": str(tmp_path / "model.idf"),
            "weather_path": str(tmp_path / "weather.epw"),
            "output_directory": str(tmp_path / "output"),
        },
    )
    values = _observation_values()
    timestamp = "2014-07-15T00:15:00"
    adapter.insert_telemetry("run-1", timestamp, "step-1", values)
    adapter.insert_zone_telemetry(
        "run-1",
        timestamp,
        "step-1",
        "CORE_ZN",
        {
            "zone_air_temperature_c": 24.0,
            "zone_operative_temperature_c": 24.1,
            "zone_relative_humidity_pct": 50.0,
            "zone_occupant_count": 20.0,
            "heating_setpoint_c": 20.0,
            "cooling_setpoint_c": 24.0,
        },
    )
    observation_id = adapter.insert_observation(
        "run-1",
        timestamp,
        "step-1",
        values,
    )
    assert observation_id == 1
    observation = adapter.store.get_current_observation("run-1")
    assert observation is not None
    assert observation.facility_demand_kw == 20.0
    assert observation.zone_temperature_mean_c == 24.0
    assert [zone.zone_name for zone in observation.zones] == ["CORE_ZN"]
    assert observation.zones[0].operative_temperature_c == 24.1
    adapter.update_run("run-1", {"status": "completed", "progress_percent": 100})
    run = adapter.store.get_run("run-1")
    assert run is not None and run.status is RunStatus.COMPLETED
