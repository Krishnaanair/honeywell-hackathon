from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from ecoloop.energyplus.results import (
    cross_check_telemetry_energy,
    parse_energyplus_csv,
    parse_energyplus_sqlite,
    parse_results,
)


def test_csv_parser_converts_energy_and_demand_units(tmp_path: Path) -> None:
    path = tmp_path / "eplusout.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            (
                "Date/Time",
                "Electricity:Facility [J](TimeStep)",
                "Electricity:HVAC [J](TimeStep)",
                "Facility Total Electricity Demand Rate [W](TimeStep)",
                "NaturalGas:Facility [J](TimeStep)",
            )
        )
        writer.writerow(("07/15  00:15:00", 3_600_000, 1_800_000, 10_000, 7_200_000))
        writer.writerow(("07/15  00:30:00", 7_200_000, 3_600_000, 15_000, 3_600_000))

    result = parse_energyplus_csv(path)
    assert result.facility_electricity_kwh == pytest.approx(3.0)
    assert result.hvac_electricity_kwh == pytest.approx(1.5)
    assert result.peak_electrical_demand_kw == pytest.approx(15.0)
    assert result.other_fuels_kwh["NaturalGas:Facility"] == pytest.approx(3.0)
    assert result.notes


def _create_energyplus_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE ReportDataDictionary (
            ReportDataDictionaryIndex INTEGER PRIMARY KEY,
            IsMeter INTEGER,
            Name TEXT,
            ReportingFrequency TEXT,
            Units TEXT
        );
        CREATE TABLE ReportData (
            ReportDataIndex INTEGER PRIMARY KEY,
            TimeIndex INTEGER,
            ReportDataDictionaryIndex INTEGER,
            Value REAL
        );
        CREATE TABLE Time (
            TimeIndex INTEGER PRIMARY KEY,
            EnvironmentPeriodIndex INTEGER,
            WarmupFlag INTEGER
        );
        CREATE TABLE EnvironmentPeriods (
            EnvironmentPeriodIndex INTEGER PRIMARY KEY,
            EnvironmentType INTEGER
        );
        """
    )
    connection.executemany(
        "INSERT INTO ReportDataDictionary VALUES (?, ?, ?, ?, ?)",
        (
            (1, 1, "Electricity:Facility", "Zone Timestep", "J"),
            (2, 1, "Electricity:HVAC", "Zone Timestep", "J"),
            (3, 0, "Facility Total Electricity Demand Rate", "Zone Timestep", "W"),
            (4, 1, "NaturalGas:Facility", "Zone Timestep", "J"),
        ),
    )
    connection.executemany(
        "INSERT INTO EnvironmentPeriods VALUES (?, ?)",
        ((1, 1), (2, 3)),
    )
    connection.executemany(
        "INSERT INTO Time VALUES (?, ?, ?)",
        ((1, 1, 0), (2, 2, 1), (3, 2, 0), (4, 2, 0)),
    )
    values = (
        (1, 1, 1, 360_000_000),
        (2, 2, 1, 360_000_000),
        (3, 3, 1, 3_600_000),
        (4, 4, 1, 7_200_000),
        (5, 3, 2, 1_800_000),
        (6, 4, 2, 3_600_000),
        (7, 3, 3, 10_000),
        (8, 4, 3, 15_000),
        (9, 3, 4, 7_200_000),
        (10, 4, 4, 3_600_000),
    )
    connection.executemany("INSERT INTO ReportData VALUES (?, ?, ?, ?)", values)
    connection.commit()
    connection.close()


def test_sqlite_parser_excludes_design_days_and_warmup(tmp_path: Path) -> None:
    path = tmp_path / "eplusout.sql"
    _create_energyplus_sqlite(path)
    result = parse_energyplus_sqlite(path)
    assert result.facility_electricity_kwh == pytest.approx(3.0)
    assert result.hvac_electricity_kwh == pytest.approx(1.5)
    assert result.peak_electrical_demand_kw == pytest.approx(15.0)
    assert result.other_fuels_kwh["NaturalGas:Facility"] == pytest.approx(3.0)
    assert result.rows == 2


def test_failed_run_never_returns_energy_totals(tmp_path: Path) -> None:
    (tmp_path / "eplusout.end").write_text("EnergyPlus Terminated--Fatal Error\n")
    (tmp_path / "eplusout.err").write_text("**  Fatal  ** invalid model\n")
    result = parse_results(tmp_path)
    assert not result.completed
    assert result.facility_electricity_kwh is None
    assert result.fatal_count == 1


def test_energy_cross_check_formula() -> None:
    result = cross_check_telemetry_energy(100.0, 99.0, 2.0)
    assert result.passed
    assert result.difference_percent == pytest.approx(1.0)
