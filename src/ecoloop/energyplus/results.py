"""Official EnergyPlus CSV/SQLite result parsing and telemetry cross-checks."""

from __future__ import annotations

import csv
import math
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ecoloop.energyplus.logs import MessageSeverity, parse_error_file, severity_counts
from ecoloop.exceptions import EnergyPlusIntegrationError

_HEADER = re.compile(
    r"^\s*(?P<name>.*?)\s*(?:\[(?P<units>[^\]]+)\])?\s*(?:\((?P<frequency>[^)]+)\))?\s*$"
)
_KNOWN_FACILITY_FUELS = (
    "NaturalGas:Facility",
    "DistrictHeatingWater:Facility",
    "DistrictHeatingSteam:Facility",
    "DistrictCooling:Facility",
    "Propane:Facility",
    "FuelOilNo1:Facility",
    "FuelOilNo2:Facility",
    "Coal:Facility",
    "Gasoline:Facility",
    "Diesel:Facility",
    "OtherFuel1:Facility",
    "OtherFuel2:Facility",
)


@dataclass(frozen=True, slots=True)
class EnergyCrossCheck:
    """Comparison between official output energy and callback telemetry."""

    official_kwh: float
    telemetry_kwh: float
    absolute_difference_kwh: float
    difference_percent: float
    tolerance_percent: float
    passed: bool


@dataclass(frozen=True, slots=True)
class EnergyPlusResults:
    """Final metrics derived only from official EnergyPlus output files."""

    run_directory: Path
    source_file: Path | None
    completed: bool
    facility_electricity_kwh: float | None
    hvac_electricity_kwh: float | None
    other_fuels_kwh: dict[str, float]
    peak_electrical_demand_kw: float | None
    warning_count: int
    severe_count: int
    fatal_count: int
    output_rows: int
    parse_notes: tuple[str, ...] = ()
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _MetricValues:
    facility_electricity_kwh: float
    hvac_electricity_kwh: float | None
    other_fuels_kwh: dict[str, float]
    peak_electrical_demand_kw: float | None
    rows: int
    notes: tuple[str, ...] = ()


def _energy_to_kwh(value: float, units: str) -> float:
    unit = units.strip().casefold().replace(" ", "")
    factors = {
        "j": 1.0 / 3_600_000.0,
        "kj": 1.0 / 3_600.0,
        "mj": 1.0 / 3.6,
        "gj": 1_000.0 / 3.6,
        "wh": 1.0 / 1_000.0,
        "kwh": 1.0,
    }
    if unit not in factors:
        raise EnergyPlusIntegrationError(f"Unsupported EnergyPlus energy unit: {units!r}")
    return value * factors[unit]


def _demand_to_kw(value: float, units: str) -> float:
    unit = units.strip().casefold().replace(" ", "")
    factors = {"w": 1.0 / 1_000.0, "kw": 1.0}
    if unit not in factors:
        raise EnergyPlusIntegrationError(f"Unsupported EnergyPlus demand unit: {units!r}")
    return value * factors[unit]


def _is_finite_number(value: str) -> float | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = float(stripped)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        raise EnergyPlusIntegrationError("EnergyPlus output contains NaN or infinity.")
    return parsed


def _parse_header(header: str) -> tuple[str, str, str]:
    match = _HEADER.match(header)
    if not match:
        return header.strip(), "", ""
    return (
        match.group("name").strip(),
        (match.group("units") or "").strip(),
        (match.group("frequency") or "").strip(),
    )


def _metric_column(
    headers: tuple[str, ...],
    requested: str,
    *,
    allowed_units: frozenset[str],
) -> tuple[int, str, str] | None:
    candidates: list[tuple[int, str, str, int]] = []
    normalized_requested = requested.casefold()
    frequency_priority = {
        "runperiod": 4,
        "run period": 4,
        "timestep": 3,
        "zone timestep": 3,
        "hourly": 2,
        "daily": 1,
        "monthly": 0,
    }
    for index, header in enumerate(headers):
        name, units, frequency = _parse_header(header)
        if name.casefold() != normalized_requested:
            continue
        if units.casefold().replace(" ", "") not in allowed_units:
            continue
        priority = frequency_priority.get(frequency.casefold(), 0)
        candidates.append((index, units, frequency, priority))
    if not candidates:
        return None
    index, units, frequency, _ = sorted(candidates, key=lambda item: -item[3])[0]
    return index, units, frequency


def parse_energyplus_csv(path: Path) -> _MetricValues:
    """Parse EnergyPlus's official CSV output.

    SQLite is preferred because it exposes environment types. CSV remains a
    supported fallback and records that environment filtering is unavailable.
    """

    try:
        handle = path.open("r", encoding="utf-8-sig", errors="replace", newline="")
    except OSError as exc:
        raise EnergyPlusIntegrationError(f"Could not open EnergyPlus CSV {path}: {exc}") from exc
    with handle:
        reader = csv.reader(handle)
        try:
            headers = tuple(next(reader))
        except StopIteration as exc:
            raise EnergyPlusIntegrationError(f"EnergyPlus CSV is empty: {path}") from exc

        facility = _metric_column(
            headers,
            "Electricity:Facility",
            allowed_units=frozenset({"j", "kj", "mj", "gj", "wh", "kwh"}),
        )
        if facility is None:
            raise EnergyPlusIntegrationError(
                f"EnergyPlus CSV {path} lacks an Electricity:Facility energy column."
            )
        hvac = _metric_column(
            headers,
            "Electricity:HVAC",
            allowed_units=frozenset({"j", "kj", "mj", "gj", "wh", "kwh"}),
        )
        demand = _metric_column(
            headers,
            "Facility Total Electricity Demand Rate",
            allowed_units=frozenset({"w", "kw"}),
        )
        fuels = {
            fuel: _metric_column(
                headers,
                fuel,
                allowed_units=frozenset({"j", "kj", "mj", "gj", "wh", "kwh"}),
            )
            for fuel in _KNOWN_FACILITY_FUELS
        }
        fuels = {name: column for name, column in fuels.items() if column is not None}
        facility_values: list[float] = []
        hvac_values: list[float] = []
        demand_values: list[float] = []
        fuel_values: dict[str, list[float]] = {name: [] for name in fuels}
        rows = 0
        for row in reader:
            rows += 1
            if facility[0] < len(row):
                value = _is_finite_number(row[facility[0]])
                if value is not None:
                    facility_values.append(value)
            if hvac is not None and hvac[0] < len(row):
                value = _is_finite_number(row[hvac[0]])
                if value is not None:
                    hvac_values.append(value)
            if demand is not None and demand[0] < len(row):
                value = _is_finite_number(row[demand[0]])
                if value is not None:
                    demand_values.append(value)
            for name, column in fuels.items():
                if column is not None and column[0] < len(row):
                    value = _is_finite_number(row[column[0]])
                    if value is not None:
                        fuel_values[name].append(value)

    if not facility_values:
        raise EnergyPlusIntegrationError(
            f"EnergyPlus CSV {path} has no numeric Electricity:Facility values."
        )

    def total(values: list[float], frequency: str) -> float:
        if frequency.casefold().replace(" ", "") == "runperiod":
            return values[-1]
        return sum(values)

    facility_total = total(facility_values, facility[2])
    hvac_total = total(hvac_values, hvac[2]) if hvac is not None and hvac_values else None
    other_fuels: dict[str, float] = {}
    for name, values in fuel_values.items():
        column = fuels[name]
        if values and column is not None:
            other_fuels[name] = _energy_to_kwh(
                total(values, column[2]),
                column[1],
            )
    return _MetricValues(
        facility_electricity_kwh=_energy_to_kwh(facility_total, facility[1]),
        hvac_electricity_kwh=(
            _energy_to_kwh(hvac_total, hvac[1])
            if hvac is not None and hvac_total is not None
            else None
        ),
        other_fuels_kwh=other_fuels,
        peak_electrical_demand_kw=(
            _demand_to_kw(max(demand_values), demand[1])
            if demand is not None and demand_values
            else None
        ),
        rows=rows,
        notes=(
            "CSV fallback cannot identify environment type; use eplusout.sql for "
            "design-day-excluded official metrics.",
        ),
    )


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return frozenset(str(row[0]) for row in rows)


def _column_names(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    if table not in _table_names(connection):
        return frozenset()
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return frozenset(str(row[1]) for row in rows)


def _report_dictionary_table(connection: sqlite3.Connection) -> str:
    tables = _table_names(connection)
    if "ReportDataDictionary" in tables:
        return "ReportDataDictionary"
    if "ReportMeterDataDictionary" in tables:
        return "ReportMeterDataDictionary"
    raise EnergyPlusIntegrationError(
        "EnergyPlus SQLite output lacks ReportDataDictionary/ReportMeterDataDictionary."
    )


def _report_data_table(connection: sqlite3.Connection, dictionary: str) -> str:
    expected = "ReportData" if dictionary == "ReportDataDictionary" else "ReportMeterData"
    if expected not in _table_names(connection):
        raise EnergyPlusIntegrationError(f"EnergyPlus SQLite output lacks {expected}.")
    return expected


def _dictionary_index_column(columns: frozenset[str], dictionary: str) -> str:
    candidates = (
        "ReportDataDictionaryIndex",
        "ReportMeterDataDictionaryIndex",
    )
    for candidate in candidates:
        if candidate in columns:
            return candidate
    raise EnergyPlusIntegrationError(f"{dictionary} lacks its dictionary index column.")


def _weather_filter_sql(
    connection: sqlite3.Connection,
    *,
    data_alias: str,
) -> tuple[str, tuple[object, ...], str]:
    tables = _table_names(connection)
    if "Time" not in tables:
        return "", (), "No Time table; environment filtering unavailable."
    time_columns = _column_names(connection, "Time")
    if "TimeIndex" not in time_columns:
        return "", (), "Time table lacks TimeIndex; environment filtering unavailable."
    join = f' JOIN "Time" t ON t."TimeIndex" = {data_alias}."TimeIndex" '
    clauses: list[str] = []
    parameters: list[object] = []
    note = ""
    if "WarmupFlag" in time_columns:
        clauses.append('COALESCE(t."WarmupFlag", 0) = 0')
    if "EnvironmentPeriodIndex" in time_columns and "EnvironmentPeriods" in tables:
        environment_columns = _column_names(connection, "EnvironmentPeriods")
        if {
            "EnvironmentPeriodIndex",
            "EnvironmentType",
        }.issubset(environment_columns):
            join += (
                ' JOIN "EnvironmentPeriods" e ON e."EnvironmentPeriodIndex" = '
                't."EnvironmentPeriodIndex" '
            )
            # EnergyPlus environment type 3 is a weather-file RunPeriod.
            clauses.append('e."EnvironmentType" = ?')
            parameters.append(3)
        else:
            note = "EnvironmentPeriods lacks type columns; design-day filtering unavailable."
    else:
        note = "Environment table unavailable; design-day filtering unavailable."
    where = " AND ".join(clauses)
    return join + (f" WHERE {where}" if where else ""), tuple(parameters), note


def _dictionary_rows(
    connection: sqlite3.Connection,
    dictionary: str,
) -> tuple[dict[str, object], ...]:
    connection.row_factory = sqlite3.Row
    if dictionary == "ReportDataDictionary":
        rows = connection.execute('SELECT * FROM "ReportDataDictionary"').fetchall()
    elif dictionary == "ReportMeterDataDictionary":
        rows = connection.execute('SELECT * FROM "ReportMeterDataDictionary"').fetchall()
    else:
        raise EnergyPlusIntegrationError(f"Unsupported dictionary table: {dictionary}")
    return tuple(dict(row) for row in rows)


def _casefold_row_value(row: dict[str, object], *names: str) -> object | None:
    lookup = {key.casefold(): value for key, value in row.items()}
    for name in names:
        if name.casefold() in lookup:
            return lookup[name.casefold()]
    return None


def _object_to_int(value: object, label: str) -> int:
    if isinstance(value, (int, str)):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise EnergyPlusIntegrationError(f"EnergyPlus {label} is not an integer: {value!r}")


def _choose_dictionary_row(
    rows: tuple[dict[str, object], ...],
    name: str,
) -> dict[str, object] | None:
    matches = [
        row
        for row in rows
        if str(_casefold_row_value(row, "Name", "VariableName") or "").casefold() == name.casefold()
    ]
    if not matches:
        return None
    priority = {
        "run period": 5,
        "runperiod": 5,
        "zone timestep": 4,
        "timestep": 4,
        "hourly": 3,
        "daily": 2,
        "monthly": 1,
    }
    return sorted(
        matches,
        key=lambda row: (
            -priority.get(
                str(_casefold_row_value(row, "ReportingFrequency") or "").casefold(),
                0,
            )
        ),
    )[0]


def _values_for_dictionary(
    connection: sqlite3.Connection,
    data_table: str,
    index_column: str,
    dictionary_index: int,
) -> tuple[tuple[float, ...], str]:
    data_columns = _column_names(connection, data_table)
    if "Value" in data_columns:
        value_column = "Value"
    elif "VariableValue" in data_columns:
        value_column = "VariableValue"
    else:
        raise EnergyPlusIntegrationError(f"{data_table} lacks a numeric value column.")
    filter_sql, parameters, note = _weather_filter_sql(connection, data_alias="d")
    query = (
        f'SELECT d."{value_column}" FROM "{data_table}" d'  # noqa: S608
        f"{filter_sql}"
        + (" AND " if " WHERE " in filter_sql else " WHERE ")
        + f'd."{index_column}" = ?'
    )
    rows = connection.execute(query, (*parameters, dictionary_index)).fetchall()
    values: list[float] = []
    for row in rows:
        value = float(row[0])
        if not math.isfinite(value):
            raise EnergyPlusIntegrationError("EnergyPlus SQLite contains NaN or infinity.")
        values.append(value)
    return tuple(values), note


def parse_energyplus_sqlite(path: Path) -> _MetricValues:
    """Parse official output from EnergyPlus SQLite, excluding design/warmup periods."""

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise EnergyPlusIntegrationError(f"Could not open EnergyPlus SQLite {path}: {exc}") from exc
    try:
        dictionary = _report_dictionary_table(connection)
        data_table = _report_data_table(connection, dictionary)
        dictionary_columns = _column_names(connection, dictionary)
        data_columns = _column_names(connection, data_table)
        _dictionary_index_column(dictionary_columns, dictionary)
        data_index_column = _dictionary_index_column(data_columns, data_table)
        rows = _dictionary_rows(connection, dictionary)

        def metric(name: str) -> tuple[float, str, str, int] | None:
            row = _choose_dictionary_row(rows, name)
            if row is None:
                return None
            dictionary_index_value = _casefold_row_value(
                row,
                "ReportDataDictionaryIndex",
                "ReportMeterDataDictionaryIndex",
            )
            if dictionary_index_value is None:
                raise EnergyPlusIntegrationError(f"Dictionary row for {name} has no index.")
            units = str(_casefold_row_value(row, "Units") or "")
            frequency = str(_casefold_row_value(row, "ReportingFrequency") or "")
            values, note = _values_for_dictionary(
                connection,
                data_table,
                data_index_column,
                _object_to_int(dictionary_index_value, "dictionary index"),
            )
            if not values:
                return None
            total = (
                values[-1] if frequency.casefold().replace(" ", "") == "runperiod" else sum(values)
            )
            return total, units, note, len(values)

        facility = metric("Electricity:Facility")
        if facility is None:
            raise EnergyPlusIntegrationError(
                f"EnergyPlus SQLite {path} lacks weather-period Electricity:Facility data."
            )
        hvac = metric("Electricity:HVAC")
        demand = metric("Facility Total Electricity Demand Rate")
        other_fuels: dict[str, float] = {}
        notes = {facility[2]} if facility[2] else set()
        row_count = facility[3]
        for name in _KNOWN_FACILITY_FUELS:
            value = metric(name)
            if value is None:
                continue
            other_fuels[name] = _energy_to_kwh(value[0], value[1])
            if value[2]:
                notes.add(value[2])
        if hvac and hvac[2]:
            notes.add(hvac[2])
        if demand and demand[2]:
            notes.add(demand[2])
        # Demand values must use max rather than the sum returned by metric().
        peak_kw: float | None = None
        demand_row = _choose_dictionary_row(rows, "Facility Total Electricity Demand Rate")
        if demand_row is not None:
            index_value = _casefold_row_value(
                demand_row,
                "ReportDataDictionaryIndex",
                "ReportMeterDataDictionaryIndex",
            )
            if index_value is not None:
                demand_values, note = _values_for_dictionary(
                    connection,
                    data_table,
                    data_index_column,
                    _object_to_int(index_value, "dictionary index"),
                )
                demand_units = str(_casefold_row_value(demand_row, "Units") or "")
                if demand_values:
                    peak_kw = _demand_to_kw(max(demand_values), demand_units)
                if note:
                    notes.add(note)
        return _MetricValues(
            facility_electricity_kwh=_energy_to_kwh(facility[0], facility[1]),
            hvac_electricity_kwh=(_energy_to_kwh(hvac[0], hvac[1]) if hvac is not None else None),
            other_fuels_kwh=other_fuels,
            peak_electrical_demand_kw=peak_kw,
            rows=row_count,
            notes=tuple(sorted(notes)),
        )
    except sqlite3.Error as exc:
        raise EnergyPlusIntegrationError(
            f"Could not query EnergyPlus SQLite {path}: {exc}"
        ) from exc
    finally:
        connection.close()


def _completed_successfully(run_directory: Path) -> tuple[bool, str | None]:
    end_file = run_directory / "eplusout.end"
    if not end_file.is_file():
        return False, "eplusout.end is missing"
    text = end_file.read_text(encoding="utf-8", errors="replace")
    if "completed successfully" in text.casefold():
        return True, None
    last_line = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    return False, last_line or "EnergyPlus did not report successful completion"


def parse_results(run_directory: Path) -> EnergyPlusResults:
    """Parse a completed EnergyPlus run without deriving official totals from callbacks."""

    directory = run_directory.expanduser().resolve()
    completed, failure_reason = _completed_successfully(directory)
    err_path = directory / "eplusout.err"
    messages = parse_error_file(err_path) if err_path.is_file() else ()
    counts = severity_counts(messages)
    fatal_count = counts[MessageSeverity.FATAL.value]
    if fatal_count:
        completed = False
        failure_reason = failure_reason or f"EnergyPlus reported {fatal_count} fatal error(s)"

    sql_path = directory / "eplusout.sql"
    csv_path = directory / "eplusout.csv"
    metric_values: _MetricValues | None = None
    source: Path | None = None
    notes: list[str] = []
    if completed:
        if sql_path.is_file():
            try:
                metric_values = parse_energyplus_sqlite(sql_path)
                source = sql_path
            except EnergyPlusIntegrationError as exc:
                notes.append(f"SQLite parser unavailable: {exc}")
        if metric_values is None and csv_path.is_file():
            metric_values = parse_energyplus_csv(csv_path)
            source = csv_path
        if metric_values is None:
            raise EnergyPlusIntegrationError(
                f"Completed run {directory} has neither parseable eplusout.sql nor eplusout.csv."
            )

    return EnergyPlusResults(
        run_directory=directory,
        source_file=source,
        completed=completed,
        facility_electricity_kwh=(
            metric_values.facility_electricity_kwh if metric_values else None
        ),
        hvac_electricity_kwh=metric_values.hvac_electricity_kwh if metric_values else None,
        other_fuels_kwh=metric_values.other_fuels_kwh if metric_values else {},
        peak_electrical_demand_kw=(
            metric_values.peak_electrical_demand_kw if metric_values else None
        ),
        warning_count=counts[MessageSeverity.WARNING.value],
        severe_count=counts[MessageSeverity.SEVERE.value],
        fatal_count=fatal_count,
        output_rows=metric_values.rows if metric_values else 0,
        parse_notes=tuple(notes) + (metric_values.notes if metric_values else ()),
        failure_reason=failure_reason,
    )


def cross_check_telemetry_energy(
    official_kwh: float,
    telemetry_kwh: float,
    tolerance_percent: float,
) -> EnergyCrossCheck:
    """Cross-check callback telemetry against official output within a percentage tolerance."""

    for name, value in (
        ("official_kwh", official_kwh),
        ("telemetry_kwh", telemetry_kwh),
        ("tolerance_percent", tolerance_percent),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if official_kwh <= 0:
        raise ValueError("official_kwh must be positive")
    if telemetry_kwh < 0 or tolerance_percent <= 0:
        raise ValueError("telemetry_kwh must be non-negative and tolerance_percent positive")
    difference = abs(official_kwh - telemetry_kwh)
    percentage = difference / official_kwh * 100.0
    return EnergyCrossCheck(
        official_kwh=official_kwh,
        telemetry_kwh=telemetry_kwh,
        absolute_difference_kwh=difference,
        difference_percent=percentage,
        tolerance_percent=tolerance_percent,
        passed=percentage <= tolerance_percent,
    )


def sum_energy_values(values: Iterable[float], units: str) -> float:
    """Convert and sum finite official energy values; primarily useful in tests."""

    total = 0.0
    for value in values:
        if not math.isfinite(value):
            raise ValueError("Energy values must be finite")
        total += value
    return _energy_to_kwh(total, units)
