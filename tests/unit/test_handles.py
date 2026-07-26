from __future__ import annotations

from pathlib import Path

import pytest

from ecoloop.energyplus.handles import (
    HandleKind,
    HandleRegistry,
    HandleSpec,
    expand_wildcard_specs,
    parse_available_api_data,
)
from ecoloop.exceptions import HandleDiscoveryError

API_DATA = """what,name,key,type,unit
OutputVariable,Zone Mean Air Temperature,CORE_ZN,FLOAT,C
OutputVariable,Zone Mean Air Temperature,PERIMETER_ZN_1,FLOAT,C
OutputVariable,Site Outdoor Air Drybulb Temperature,Environment,FLOAT,C
Meter,Electricity:Facility,,FLOAT,J
Actuator,Schedule:Compact,Schedule Value,EcoLoop Actuated Heating Setpoint,FLOAT,C
"""


class FakeExchange:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.requests: list[tuple[str, str]] = []

    def api_data_fully_ready(self, state: object) -> bool:
        return self.ready

    def list_available_api_data_csv(self, state: object) -> bytes:
        return API_DATA.encode()

    def get_variable_handle(self, state: object, name: str, key: str) -> int:
        return {
            ("Zone Mean Air Temperature", "CORE_ZN"): 1,
            ("Zone Mean Air Temperature", "PERIMETER_ZN_1"): 2,
            ("Site Outdoor Air Drybulb Temperature", "Environment"): 3,
        }.get((name, key), -1)

    def get_meter_handle(self, state: object, name: str) -> int:
        return 4 if name == "Electricity:Facility" else -1

    def get_actuator_handle(
        self,
        state: object,
        component_type: str,
        control_type: str,
        key: str,
    ) -> int:
        if (
            component_type,
            control_type,
            key,
        ) == (
            "Schedule:Compact",
            "Schedule Value",
            "EcoLoop Actuated Heating Setpoint",
        ):
            return 5
        return -1

    def request_variable(self, state: object, name: str, key: str) -> None:
        self.requests.append((name, key))


def test_registry_defers_handles_until_api_ready(tmp_path: Path) -> None:
    registry = HandleRegistry(
        (
            HandleSpec(
                "zone_air_temperature_c",
                HandleKind.VARIABLE,
                "Zone Mean Air Temperature",
                "*",
            ),
        )
    )
    with pytest.raises(HandleDiscoveryError, match="not fully ready"):
        registry.resolve(FakeExchange(ready=False), object(), tmp_path)
    assert not (tmp_path / "api_points.csv").exists()


def test_wildcard_handle_resolution_and_api_dump(tmp_path: Path) -> None:
    registry = HandleRegistry(
        (
            HandleSpec(
                "zone_air_temperature_c",
                HandleKind.VARIABLE,
                "Zone Mean Air Temperature",
                "*",
            ),
            HandleSpec(
                "facility_electricity_j",
                HandleKind.METER,
                "Electricity:Facility",
            ),
        )
    )
    records = registry.resolve(FakeExchange(), object(), tmp_path)
    assert [(item.spec.key, item.handle) for item in records] == [
        ("", 4),
        ("CORE_ZN", 1),
        ("PERIMETER_ZN_1", 2),
    ]
    assert {item.units for item in records} == {"C", "J"}
    assert (tmp_path / "api_points.csv").read_text(encoding="utf-8") == API_DATA


def test_missing_required_handle_reports_near_matches(tmp_path: Path) -> None:
    registry = HandleRegistry(
        (
            HandleSpec(
                "outdoor_drybulb_c",
                HandleKind.VARIABLE,
                "Site Outdoor Dry Bulb Temperature",
                "Environment",
            ),
        )
    )
    with pytest.raises(HandleDiscoveryError) as captured:
        registry.resolve(FakeExchange(), object(), tmp_path)
    assert "Site Outdoor Air Drybulb Temperature" in str(captured.value)
    assert str(tmp_path / "api_points.csv") in str(captured.value)


def test_variable_requests_are_deduplicated() -> None:
    registry = HandleRegistry(
        (
            HandleSpec("one", HandleKind.VARIABLE, "Variable", "Key"),
            HandleSpec("two", HandleKind.VARIABLE, "Variable", "Key"),
        )
    )
    exchange = FakeExchange()
    registry.request_variables(exchange, object())
    assert exchange.requests == [("Variable", "Key")]


def test_available_point_parser_and_wildcard_expansion() -> None:
    points = parse_available_api_data(API_DATA)
    specs = expand_wildcard_specs(
        (
            HandleSpec(
                "zone",
                HandleKind.VARIABLE,
                "Zone Mean Air Temperature",
                "*",
            ),
        ),
        points,
    )
    assert [item.key for item in specs] == ["CORE_ZN", "PERIMETER_ZN_1"]
