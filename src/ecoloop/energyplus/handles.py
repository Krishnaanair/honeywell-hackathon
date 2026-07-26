"""Readiness-safe EnergyPlus Data Exchange handle discovery."""

from __future__ import annotations

import csv
import difflib
import io
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ecoloop.exceptions import HandleDiscoveryError

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class HandleKind(StrEnum):
    """Supported EnergyPlus exchange handle categories."""

    VARIABLE = "variable"
    METER = "meter"
    ACTUATOR = "actuator"


@dataclass(frozen=True, slots=True)
class HandleSpec:
    """One logical EnergyPlus point requested by EcoLoop."""

    logical_metric: str
    kind: HandleKind
    name: str
    key: str = ""
    required: bool = True
    units: str | None = None
    control_type: str | None = None

    def requested_label(self) -> str:
        """Return a human-readable point identifier."""

        if self.kind is HandleKind.ACTUATOR:
            return f"{self.name}/{self.control_type or ''}/{self.key}"
        if self.key:
            return f"{self.name}/{self.key}"
        return self.name


@dataclass(frozen=True, slots=True)
class HandleRecord:
    """Resolved or unavailable handle metadata."""

    spec: HandleSpec
    handle: int
    units: str | None
    available: bool
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AvailableAPIPoint:
    """Parsed row from ``list_available_api_data_csv``."""

    category: str
    name: str
    key: str
    control_type: str | None
    units: str | None
    raw: tuple[str, ...]

    @property
    def label(self) -> str:
        """Return a concise API-point label."""

        fields = [self.category, self.name]
        if self.control_type:
            fields.append(self.control_type)
        if self.key:
            fields.append(self.key)
        return " | ".join(fields)


class ExchangeProtocol(Protocol):
    """Subset of the Data Exchange API needed by handle discovery."""

    def api_data_fully_ready(self, state: object) -> bool: ...

    def list_available_api_data_csv(self, state: object) -> bytes: ...

    def get_variable_handle(self, state: object, variable_name: str, variable_key: str) -> int: ...

    def get_meter_handle(self, state: object, meter_name: str) -> int: ...

    def get_actuator_handle(
        self,
        state: object,
        component_type: str,
        control_type: str,
        actuator_key: str,
    ) -> int: ...

    def request_variable(self, state: object, variable_name: str, variable_key: str) -> None: ...


def normalize_point_name(value: str) -> str:
    """Normalize EnergyPlus names for case- and punctuation-insensitive matching."""

    return _NON_ALNUM.sub("", value.casefold())


def parse_available_api_data(data: bytes | str) -> tuple[AvailableAPIPoint, ...]:
    """Parse the EnergyPlus available API data CSV across known column layouts."""

    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    reader = csv.reader(io.StringIO(text))
    rows = [tuple(item.strip().strip('"') for item in row) for row in reader if row]
    if not rows:
        return ()

    first = tuple(item.casefold() for item in rows[0])
    has_header = bool(first and first[0] in {"what", "type", "category"})
    points: list[AvailableAPIPoint] = []
    for row in rows[1:] if has_header else rows:
        if len(row) < 2:
            continue
        category = row[0]
        category_key = normalize_point_name(category)
        if "actuator" in category_key:
            # 26.1: what, component type, control type, key, units.
            # Some earlier releases included a data-type column before units.
            name = row[1] if len(row) > 1 else ""
            control_type = row[2] if len(row) > 2 else ""
            key = row[3] if len(row) > 3 else ""
            units = row[-1] if len(row) > 4 else None
        elif "meter" in category_key:
            # 26.1: what, meter name, units.
            name = row[1] if len(row) > 1 else ""
            key = ""
            control_type = None
            units = row[-1] if len(row) > 2 else None
        else:
            # 26.1: what, variable name, key, units.
            name = row[1] if len(row) > 1 else ""
            key = row[2] if len(row) > 2 else ""
            control_type = None
            units = row[-1] if len(row) > 3 else None
        points.append(
            AvailableAPIPoint(
                category=category,
                name=name,
                key=key,
                control_type=control_type or None,
                units=units or None,
                raw=row,
            )
        )
    return tuple(points)


def _spec_match_blob(spec: HandleSpec) -> str:
    return normalize_point_name(
        " ".join(
            value
            for value in (spec.kind.value, spec.name, spec.control_type or "", spec.key)
            if value
        )
    )


def _point_match_blob(point: AvailableAPIPoint) -> str:
    return normalize_point_name(
        " ".join(
            value
            for value in (point.category, point.name, point.control_type or "", point.key)
            if value
        )
    )


def suggest_available_points(
    spec: HandleSpec,
    points: tuple[AvailableAPIPoint, ...],
    *,
    limit: int = 5,
) -> tuple[str, ...]:
    """Return normalized near matches for a missing handle."""

    requested = _spec_match_blob(spec)
    ranked: list[tuple[float, str]] = []
    for point in points:
        candidate = _point_match_blob(point)
        ratio = difflib.SequenceMatcher(a=requested, b=candidate).ratio()
        if normalize_point_name(spec.name) in candidate:
            ratio += 0.25
        if spec.key and normalize_point_name(spec.key) in candidate:
            ratio += 0.15
        ranked.append((ratio, point.label))
    ranked.sort(key=lambda item: (-item[0], item[1].casefold()))
    return tuple(label for score, label in ranked[:limit] if score >= 0.35)


def _category_matches(kind: HandleKind, category: str) -> bool:
    normalized = normalize_point_name(category)
    if kind is HandleKind.VARIABLE:
        return "outputvariable" in normalized or normalized == "variable"
    if kind is HandleKind.METER:
        return "meter" in normalized
    return "actuator" in normalized


def _available_units(
    spec: HandleSpec,
    points: tuple[AvailableAPIPoint, ...],
) -> str | None:
    for point in points:
        if not _category_matches(spec.kind, point.category):
            continue
        if normalize_point_name(point.name) != normalize_point_name(spec.name):
            continue
        if spec.kind is HandleKind.ACTUATOR and normalize_point_name(
            point.control_type or ""
        ) != normalize_point_name(spec.control_type or ""):
            continue
        if spec.key and normalize_point_name(point.key) != normalize_point_name(spec.key):
            continue
        return point.units
    return None


def expand_wildcard_specs(
    specs: tuple[HandleSpec, ...],
    points: tuple[AvailableAPIPoint, ...],
) -> tuple[HandleSpec, ...]:
    """Expand a ``*`` key into actual keys advertised by EnergyPlus."""

    expanded: list[HandleSpec] = []
    for spec in specs:
        if spec.key != "*":
            expanded.append(spec)
            continue
        matches = [
            point
            for point in points
            if _category_matches(spec.kind, point.category)
            and normalize_point_name(point.name) == normalize_point_name(spec.name)
            and (
                spec.kind is not HandleKind.ACTUATOR
                or normalize_point_name(point.control_type or "")
                == normalize_point_name(spec.control_type or "")
            )
        ]
        if not matches:
            expanded.append(spec)
            continue
        seen: set[str] = set()
        for point in matches:
            key = point.key
            normalized_key = normalize_point_name(key)
            if not key or normalized_key in seen:
                continue
            seen.add(normalized_key)
            expanded.append(replace(spec, key=key, units=spec.units or point.units))
    return tuple(expanded)


class HandleRegistry:
    """Request, resolve, cache, and diagnose EnergyPlus exchange handles."""

    def __init__(self, specs: tuple[HandleSpec, ...]) -> None:
        self._original_specs = specs
        self._records: dict[str, HandleRecord] = {}
        self._resolved = False

    @property
    def records(self) -> tuple[HandleRecord, ...]:
        """Return records in deterministic logical/key order."""

        return tuple(
            sorted(
                self._records.values(),
                key=lambda item: (
                    item.spec.logical_metric.casefold(),
                    item.spec.key.casefold(),
                    item.spec.kind.value,
                ),
            )
        )

    @property
    def is_resolved(self) -> bool:
        """Return whether discovery has completed."""

        return self._resolved

    def request_variables(self, exchange: ExchangeProtocol, state: object) -> None:
        """Issue all variable requests before EnergyPlus begins execution."""

        requested: set[tuple[str, str]] = set()
        for spec in self._original_specs:
            if spec.kind is not HandleKind.VARIABLE or not spec.required:
                continue
            pair = (spec.name.casefold(), spec.key.casefold())
            if pair in requested:
                continue
            requested.add(pair)
            exchange.request_variable(state, spec.name, spec.key)

    def resolve(
        self,
        exchange: ExchangeProtocol,
        state: object,
        run_directory: Path,
    ) -> tuple[HandleRecord, ...]:
        """Resolve handles only after readiness and persist point data on failure."""

        if self._resolved:
            return self.records
        if not exchange.api_data_fully_ready(state):
            raise HandleDiscoveryError(
                "EnergyPlus API data are not fully ready; handle discovery was correctly deferred."
            )

        raw_csv = exchange.list_available_api_data_csv(state)
        run_directory.mkdir(parents=True, exist_ok=True)
        dump_path = run_directory / "api_points.csv"
        dump_path.write_bytes(raw_csv)
        available = parse_available_api_data(raw_csv)
        specs = expand_wildcard_specs(self._original_specs, available)
        missing_required: list[HandleRecord] = []

        for spec in specs:
            handle = self._get_handle(exchange, state, spec)
            suggestions = () if handle >= 0 else suggest_available_points(spec, available)
            record = HandleRecord(
                spec=spec,
                handle=handle,
                units=spec.units or _available_units(spec, available),
                available=handle >= 0,
                suggestions=suggestions,
            )
            key = self._record_key(spec)
            if key in self._records:
                continue
            self._records[key] = record
            if not record.available and spec.required:
                missing_required.append(record)

        self._resolved = True
        if missing_required:
            details = []
            for record in missing_required:
                suggestion_text = (
                    f"; near matches: {', '.join(record.suggestions)}"
                    if record.suggestions
                    else "; no near match advertised"
                )
                details.append(f"{record.spec.requested_label()}{suggestion_text}")
            raise HandleDiscoveryError(
                "Required EnergyPlus handles are unavailable: "
                + " | ".join(details)
                + f". Full API data were written to {dump_path}."
            )
        return self.records

    @staticmethod
    def _record_key(spec: HandleSpec) -> str:
        return "\0".join(
            (
                spec.kind.value,
                normalize_point_name(spec.logical_metric),
                normalize_point_name(spec.name),
                normalize_point_name(spec.control_type or ""),
                normalize_point_name(spec.key),
            )
        )

    @staticmethod
    def _get_handle(exchange: ExchangeProtocol, state: object, spec: HandleSpec) -> int:
        if spec.kind is HandleKind.VARIABLE:
            return exchange.get_variable_handle(state, spec.name, spec.key)
        if spec.kind is HandleKind.METER:
            return exchange.get_meter_handle(state, spec.name)
        if not spec.control_type:
            raise HandleDiscoveryError(
                f"Actuator {spec.logical_metric} is missing its control type."
            )
        return exchange.get_actuator_handle(state, spec.name, spec.control_type, spec.key)

    def by_logical_metric(self, metric: str) -> tuple[HandleRecord, ...]:
        """Return all records for a logical metric."""

        normalized = normalize_point_name(metric)
        return tuple(
            record
            for record in self.records
            if normalize_point_name(record.spec.logical_metric) == normalized
        )


def default_observation_specs() -> tuple[HandleSpec, ...]:
    """Return required and capability-dependent telemetry specifications."""

    variable = HandleKind.VARIABLE
    meter = HandleKind.METER
    return (
        HandleSpec("zone_air_temperature_c", variable, "Zone Mean Air Temperature", "*"),
        HandleSpec(
            "zone_operative_temperature_c",
            variable,
            "Zone Operative Temperature",
            "*",
            required=False,
        ),
        HandleSpec(
            "zone_relative_humidity_pct",
            variable,
            "Zone Air Relative Humidity",
            "*",
            required=False,
        ),
        HandleSpec(
            "zone_occupant_count",
            variable,
            "Zone People Occupant Count",
            "*",
            required=False,
        ),
        HandleSpec(
            "zone_pmv",
            variable,
            "Zone Thermal Comfort Fanger Model PMV",
            "*",
            required=False,
        ),
        HandleSpec(
            "zone_ppd",
            variable,
            "Zone Thermal Comfort Fanger Model PPD",
            "*",
            required=False,
        ),
        HandleSpec(
            "zone_co2_ppm",
            variable,
            "Zone Air CO2 Concentration",
            "*",
            required=False,
        ),
        HandleSpec(
            "outdoor_drybulb_c",
            variable,
            "Site Outdoor Air Drybulb Temperature",
            "Environment",
        ),
        HandleSpec(
            "heating_setpoint_c",
            variable,
            "Zone Thermostat Heating Setpoint Temperature",
            "*",
        ),
        HandleSpec(
            "cooling_setpoint_c",
            variable,
            "Zone Thermostat Cooling Setpoint Temperature",
            "*",
        ),
        HandleSpec(
            "facility_demand_w",
            variable,
            "Facility Total Electricity Demand Rate",
            "Whole Building",
            required=False,
        ),
        HandleSpec(
            "facility_electricity_j",
            meter,
            "Electricity:Facility",
            required=False,
        ),
        HandleSpec(
            "facility_net_electricity_j",
            meter,
            "ElectricityNet:Facility",
        ),
        HandleSpec(
            "hvac_electricity_j",
            meter,
            "Electricity:HVAC",
            required=False,
        ),
    )
