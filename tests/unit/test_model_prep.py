from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ecoloop.energyplus.model import (
    ModelProvenance,
    SchemaNavigator,
    _portable_provenance_payload,
    _sha256_model_text,
    _write_json,
    patch_model,
)
from ecoloop.energyplus.replay import replay_actions_from_store
from ecoloop.exceptions import EnergyPlusIntegrationError
from ecoloop.schemas import ValidationResult
from tests.unit._factories import NOW, action, candidate, observation


def _container(properties: dict[str, object]) -> dict[str, object]:
    return {
        "patternProperties": {
            ".*": {
                "type": "object",
                "properties": properties,
            }
        }
    }


def _schema() -> dict[str, object]:
    field = {"type": "string"}
    return {
        "properties": {
            "RunPeriod": _container(
                {
                    "begin_month": field,
                    "begin_day_of_month": field,
                    "end_month": field,
                    "end_day_of_month": field,
                }
            ),
            "Timestep": _container({"number_of_timesteps_per_hour": field}),
            "ScheduleTypeLimits": _container(
                {
                    "lower_limit_value": field,
                    "upper_limit_value": field,
                    "unit_type": field,
                    "numeric_type": field,
                }
            ),
            "Schedule:Compact": _container(
                {
                    "schedule_type_limits_name": field,
                    "data": field,
                }
            ),
            "Schedule:Constant": _container(
                {
                    "schedule_type_limits_name": field,
                    "hourly_value": field,
                }
            ),
            "ThermostatSetpoint:DualSetpoint": _container(
                {
                    "heating_setpoint_temperature_schedule_name": field,
                    "cooling_setpoint_temperature_schedule_name": field,
                }
            ),
            "ThermostatSetpoint:SingleHeating": _container(
                {"setpoint_temperature_schedule_name": field}
            ),
            "ThermostatSetpoint:SingleCooling": _container(
                {"setpoint_temperature_schedule_name": field}
            ),
            "Output:Variable": _container(
                {
                    "key_value": field,
                    "variable_name": field,
                    "reporting_frequency": field,
                }
            ),
            "Output:Meter": _container(
                {
                    "key_name": field,
                    "reporting_frequency": field,
                }
            ),
            "Output:SQLite": _container({"option_type": field}),
            "People": _container(
                {
                    "zone_or_zonelist_or_space_or_spacelist_name": field,
                    "activity_level_schedule_name": field,
                    "clothing_insulation_schedule_name": field,
                    "air_velocity_schedule_name": field,
                    "work_efficiency_schedule_name": field,
                    "clothing_insulation_calculation_method": field,
                    "mean_radiant_temperature_calculation_type": field,
                    "thermal_comfort_model_1_type": field,
                }
            ),
        }
    }


@dataclass(frozen=True)
class Period:
    start_month: int = 7
    start_day: int = 15
    end_month: int = 7
    end_day: int = 15


def _source() -> dict[str, object]:
    return {
        "RunPeriod": {
            "First": {},
            "Second": {},
        },
        "Timestep": {"Original": {"number_of_timesteps_per_hour": 6}},
        "ScheduleTypeLimits": {
            "Temperature": {
                "unit_type": "Temperature",
                "numeric_type": "Continuous",
            }
        },
        "ThermostatSetpoint:DualSetpoint": {
            "Office": {
                "heating_setpoint_temperature_schedule_name": "Old Heat",
                "cooling_setpoint_temperature_schedule_name": "Old Cool",
            }
        },
        "People": {
            "Office People": {
                "zone_or_zonelist_or_space_or_spacelist_name": "Office",
                "activity_level_schedule_name": "Activity",
            }
        },
    }


def test_structured_model_patch_is_deterministic_and_keeps_one_period() -> None:
    schema = SchemaNavigator(_schema())
    first, metadata = patch_model(_source(), schema, Period(), 15, controlled=True)
    second, _ = patch_model(_source(), schema, Period(), 15, controlled=True)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert list(first["RunPeriod"]) == ["EcoLoop Evaluation Period"]
    assert first["Timestep"]["Original"]["number_of_timesteps_per_hour"] == 4
    thermostat = first["ThermostatSetpoint:DualSetpoint"]["Office"]
    assert thermostat["heating_setpoint_temperature_schedule_name"].startswith("EcoLoop Actuated")
    assert metadata["thermostat_objects_patched"] == 1
    assert metadata["people_objects_fanger_enabled"] == 1
    people = first["People"]["Office People"]
    assert people["thermal_comfort_model_1_type"] == "Fanger"
    assert people["clothing_insulation_calculation_method"] == "ClothingInsulationSchedule"
    assert people["clothing_insulation_schedule_name"] == "EcoLoop Summer Office Clothing"
    assert people["work_efficiency_schedule_name"] == "EcoLoop Work Efficiency"
    assert people["air_velocity_schedule_name"] == "EcoLoop Air Velocity"
    assert first["Schedule:Constant"] == {
        "EcoLoop Air Velocity": {
            "schedule_type_limits_name": "EcoLoop Air Velocity Limits",
            "hourly_value": 0.1,
        },
        "EcoLoop Summer Office Clothing": {
            "schedule_type_limits_name": "EcoLoop Clothing Insulation Limits",
            "hourly_value": 0.5,
        },
        "EcoLoop Work Efficiency": {
            "schedule_type_limits_name": "EcoLoop Work Efficiency Limits",
            "hourly_value": 0.0,
        },
    }
    assert metadata["fanger_people_zone_map"] == {"Office People": "Office"}
    output_names = {fields["variable_name"] for fields in first["Output:Variable"].values()}
    assert "Zone Thermal Comfort Fanger Model PMV" in output_names
    assert "Zone Thermal Comfort Fanger Model PPD" in output_names


def test_schedule_data_and_output_meter_match_epjson_shapes() -> None:
    model, _ = patch_model(
        _source(),
        SchemaNavigator(_schema()),
        Period(),
        15,
        controlled=False,
    )
    heating = model["Schedule:Compact"]["EcoLoop Reference Heating Setpoint"]
    assert heating["data"][0] == {"field": "Through: 12/31"}
    meter = model["Output:Meter"]["EcoLoop Electricity:Facility"]
    assert meter == {
        "key_name": "Electricity:Facility",
        "reporting_frequency": "Timestep",
    }


def test_provenance_serialization_excludes_machine_absolute_paths(
    tmp_path: Path,
) -> None:
    repository_model = tmp_path / "building.idf"
    repository_model.write_text("Version,26.1;\n", encoding="utf-8")
    provenance = ModelProvenance(
        source_path=Path("C:/Users/example/EnergyPlus/ExampleFiles/building.idf"),
        source_name="building.idf",
        energyplus_version="26.1.0",
        energyplus_home=Path("C:/Users/example/EnergyPlusV26-1-0"),
        license_path=Path("C:/Users/example/EnergyPlusV26-1-0/LICENSE.txt"),
        selection_reasons=("version-matched official example",),
    )
    payload = _portable_provenance_payload(provenance, repository_model)
    serialized = json.dumps(payload)
    assert "C:\\\\" not in serialized
    assert "C:/" not in serialized
    assert "Users/example" not in serialized
    assert payload["repository_model"] == "building.idf"
    assert len(payload["repository_model_sha256"]) == 64


def test_model_text_sha256_is_identical_for_lf_and_crlf(tmp_path: Path) -> None:
    lf_model = tmp_path / "lf.idf"
    crlf_model = tmp_path / "crlf.idf"
    canonical = b"Version,26.1;\nTimestep,4;\n"
    lf_model.write_bytes(canonical)
    crlf_model.write_bytes(canonical.replace(b"\n", b"\r\n"))

    assert _sha256_model_text(lf_model) == hashlib.sha256(canonical).hexdigest()
    assert _sha256_model_text(crlf_model) == _sha256_model_text(lf_model)


def test_generated_json_uses_lf_line_endings(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"

    _write_json(output, {"source_model": "models/base/building.idf", "verified": True})

    serialized = output.read_bytes()
    assert serialized.endswith(b"\n")
    assert b"\r\n" not in serialized
    assert json.loads(serialized) == {
        "source_model": "models/base/building.idf",
        "verified": True,
    }


class _ReplayStore:
    def __init__(self, has_actions: bool = True) -> None:
        proposed = action()
        validation = ValidationResult(
            run_id=proposed.run_id,
            observation_id=proposed.observation_id,
            action_generation=proposed.action_generation,
            timestamp=NOW,
            accepted=True,
            proposed_action=proposed.action,
            applied_action=candidate(
                heating_setpoint_c=20.5,
                cooling_setpoint_c=24.5,
                hold_minutes=60,
            ),
            applied_expires_at=proposed.expires_at,
        )
        self.pairs = [(proposed, validation)] if has_actions else []

    def get_applied_actions(self, run_id: str, *, limit: int = 10_000):
        return self.pairs[:limit]

    def get_observation(self, run_id: str, observation_id: int):
        return observation(run_id=run_id, observation_id=observation_id)


def test_replay_actions_use_simulation_timestamp_and_applied_values() -> None:
    actions = replay_actions_from_store("run-agent-1", _ReplayStore())
    assert len(actions) == 1
    assert actions[0].simulation_timestamp == NOW.replace(tzinfo=None).isoformat()
    assert actions[0].heating_setpoint_c == 20.5
    assert actions[0].cooling_setpoint_c == 24.5
    assert actions[0].hold_minutes == 60


def test_replay_generation_rejects_run_without_applied_actions() -> None:
    with pytest.raises(EnergyPlusIntegrationError, match="no accepted applied actions"):
        replay_actions_from_store("run-agent-1", _ReplayStore(has_actions=False))
