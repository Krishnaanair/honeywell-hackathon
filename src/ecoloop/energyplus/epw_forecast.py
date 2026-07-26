"""Deterministic, bounded forecast context derived from a configured EPW file.

EPW weather is simulation input, not a live weather forecast. This module reads
only the calendar coordinate, dry-bulb temperature, and global horizontal
radiation fields needed by the supervisory controller. EPW radiation is an
hourly energy density in Wh/m2; for a one-hour EPW interval its numeric value is
also the interval-average irradiance in W/m2.
"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from statistics import fmean
from threading import RLock
from typing import Final, Literal

MIN_FORECAST_HOURS: Final = 1
MAX_FORECAST_HOURS: Final = 168
VERIFIED_CONFIGURED_EPW_SOURCE: Final = "verified_configured_epw_not_live_weather"

_EPW_HEADER_ROWS: Final = 8
_DRY_BULB_COLUMN: Final = 6
_GLOBAL_HORIZONTAL_RADIATION_COLUMN: Final = 13
_MIN_REQUIRED_COLUMNS: Final = _GLOBAL_HORIZONTAL_RADIATION_COLUMN + 1
_DRY_BULB_MISSING_VALUE: Final = 99.9
_SOLAR_MISSING_VALUE: Final = 9999.0
_CACHE_LIMIT: Final = 8

_EPWKey = tuple[int, int, int]


class EPWForecastError(ValueError):
    """Raised when configured EPW data cannot produce trustworthy context."""


@dataclass(frozen=True, slots=True)
class EPWFileMetadata:
    """Content identity used to keep the bounded parser cache safe."""

    resolved_path: Path
    size_bytes: int
    modified_time_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class EPWHourlyWeather:
    """The required weather fields for one EPW hourly interval."""

    month: int
    day: int
    epw_hour: int
    drybulb_temperature_c: float
    global_horizontal_solar_w_m2: float


@dataclass(frozen=True, slots=True)
class EPWForecastPoint:
    """One projected simulation-hour point backed by an EPW record."""

    offset_hours: int
    timestamp: datetime
    drybulb_temperature_c: float
    global_horizontal_solar_w_m2: float
    epw_month: int
    epw_day: int
    epw_hour: int
    leap_day_fallback: bool


@dataclass(frozen=True, slots=True)
class EPWForecastContext:
    """Bounded EPW context and aggregate features for one simulation instant."""

    simulation_timestamp: datetime
    horizon_hours: int
    points: tuple[EPWForecastPoint, ...]
    temperature_mean_c: float
    temperature_max_c: float
    solar_mean_w_m2: float
    file_metadata: EPWFileMetadata
    source: Literal["verified_configured_epw_not_live_weather"] = VERIFIED_CONFIGURED_EPW_SOURCE


@dataclass(frozen=True, slots=True)
class _EPWDataset:
    metadata: EPWFileMetadata
    rows: dict[_EPWKey, EPWHourlyWeather]
    has_leap_day: bool


_cache_lock = RLock()
_dataset_cache: OrderedDict[EPWFileMetadata, _EPWDataset] = OrderedDict()


def forecast_context_from_epw(
    path: Path,
    simulation_timestamp: datetime,
    hours: int,
) -> EPWForecastContext:
    """Return the next ``hours`` of deterministic context from configured EPW data.

    Matching uses the month, day, and hour shown by the simulation timestamp.
    EPW hours run from 1 through 24 and represent interval end times, so midnight
    maps to hour 24 of the preceding calendar date. When a leap-year simulation
    requests February 29 from a 365-day EPW, February 28 is repeated explicitly.
    """

    horizon = _validate_horizon(hours)
    _validate_timestamp(simulation_timestamp)
    dataset = _load_dataset(path)
    hour_start = simulation_timestamp.replace(minute=0, second=0, microsecond=0)

    points: list[EPWForecastPoint] = []
    for offset in range(1, horizon + 1):
        target = hour_start + timedelta(hours=offset)
        month, day, epw_hour = _epw_coordinate(target)
        key = (month, day, epw_hour)
        leap_day_fallback = False
        row = dataset.rows.get(key)
        if row is None and month == 2 and day == 29 and not dataset.has_leap_day:
            key = (2, 28, epw_hour)
            row = dataset.rows.get(key)
            leap_day_fallback = row is not None
        if row is None:
            raise EPWForecastError(
                "Configured EPW does not contain weather for "
                f"month={month}, day={day}, EPW hour={epw_hour}; "
                f"needed for simulation timestamp {target.isoformat()} "
                f"from {dataset.metadata.resolved_path}."
            )
        points.append(
            EPWForecastPoint(
                offset_hours=offset,
                timestamp=target,
                drybulb_temperature_c=row.drybulb_temperature_c,
                global_horizontal_solar_w_m2=row.global_horizontal_solar_w_m2,
                epw_month=row.month,
                epw_day=row.day,
                epw_hour=row.epw_hour,
                leap_day_fallback=leap_day_fallback,
            )
        )

    temperatures = [point.drybulb_temperature_c for point in points]
    solar = [point.global_horizontal_solar_w_m2 for point in points]
    return EPWForecastContext(
        simulation_timestamp=simulation_timestamp,
        horizon_hours=horizon,
        points=tuple(points),
        temperature_mean_c=fmean(temperatures),
        temperature_max_c=max(temperatures),
        solar_mean_w_m2=fmean(solar),
        file_metadata=dataset.metadata,
    )


def _validate_horizon(hours: int) -> int:
    if isinstance(hours, bool) or not isinstance(hours, int):
        raise EPWForecastError("Forecast horizon must be an integer from 1 through 168 hours.")
    if not MIN_FORECAST_HOURS <= hours <= MAX_FORECAST_HOURS:
        raise EPWForecastError("Forecast horizon must be from 1 through 168 hours.")
    return hours


def _validate_timestamp(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EPWForecastError("Simulation timestamp must include a timezone.")


def _epw_coordinate(interval_end: datetime) -> _EPWKey:
    if interval_end.hour == 0:
        previous_day = (interval_end - timedelta(days=1)).date()
        return previous_day.month, previous_day.day, 24
    return interval_end.month, interval_end.day, interval_end.hour


def _load_dataset(path: Path) -> _EPWDataset:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise EPWForecastError(f"Configured EPW file does not exist: {resolved}")

    try:
        before = resolved.stat()
        content = resolved.read_bytes()
        after = resolved.stat()
    except OSError as exc:
        raise EPWForecastError(f"Unable to read configured EPW file {resolved}: {exc}") from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(content) != after.st_size
    ):
        raise EPWForecastError(
            f"Configured EPW file changed while it was being read: {resolved}. Retry the request."
        )

    metadata = EPWFileMetadata(
        resolved_path=resolved,
        size_bytes=len(content),
        modified_time_ns=after.st_mtime_ns,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    with _cache_lock:
        cached = _dataset_cache.get(metadata)
        if cached is not None:
            _dataset_cache.move_to_end(metadata)
            return cached

    dataset = _parse_dataset(content, metadata)
    with _cache_lock:
        existing = _dataset_cache.get(metadata)
        if existing is not None:
            _dataset_cache.move_to_end(metadata)
            return existing
        _dataset_cache[metadata] = dataset
        while len(_dataset_cache) > _CACHE_LIMIT:
            _dataset_cache.popitem(last=False)
    return dataset


def _parse_dataset(content: bytes, metadata: EPWFileMetadata) -> _EPWDataset:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EPWForecastError(
            f"Configured EPW is not valid UTF-8 text: {metadata.resolved_path}"
        ) from exc

    physical_lines = text.splitlines()
    if len(physical_lines) <= _EPW_HEADER_ROWS:
        raise EPWForecastError(
            f"Configured EPW must contain eight header rows and hourly data: "
            f"{metadata.resolved_path}"
        )

    rows: dict[_EPWKey, EPWHourlyWeather] = {}
    reader = csv.reader(StringIO("\n".join(physical_lines[_EPW_HEADER_ROWS:])))
    for data_index, fields in enumerate(reader, start=1):
        line_number = _EPW_HEADER_ROWS + data_index
        if not fields or all(not field.strip() for field in fields):
            continue
        if len(fields) < _MIN_REQUIRED_COLUMNS:
            raise EPWForecastError(
                f"Invalid EPW data row at line {line_number}: expected at least "
                f"{_MIN_REQUIRED_COLUMNS} columns, found {len(fields)}."
            )

        month = _parse_integer(fields[1], "month", line_number)
        day = _parse_integer(fields[2], "day", line_number)
        epw_hour = _parse_integer(fields[3], "hour", line_number)
        if not 1 <= epw_hour <= 24:
            raise EPWForecastError(
                f"Invalid EPW hour at line {line_number}: {epw_hour}; expected 1 through 24."
            )
        try:
            date(2000, month, day)
        except ValueError as exc:
            raise EPWForecastError(
                f"Invalid EPW month/day at line {line_number}: {month}/{day}."
            ) from exc

        drybulb = _parse_finite_float(
            fields[_DRY_BULB_COLUMN],
            "dry-bulb temperature",
            line_number,
        )
        solar = _parse_finite_float(
            fields[_GLOBAL_HORIZONTAL_RADIATION_COLUMN],
            "global horizontal radiation",
            line_number,
        )
        if math.isclose(drybulb, _DRY_BULB_MISSING_VALUE, rel_tol=0.0, abs_tol=1e-9):
            raise EPWForecastError(
                f"Missing EPW dry-bulb temperature sentinel at line {line_number}."
            )
        if math.isclose(solar, _SOLAR_MISSING_VALUE, rel_tol=0.0, abs_tol=1e-9):
            raise EPWForecastError(
                f"Missing EPW global horizontal radiation sentinel at line {line_number}."
            )
        if solar < 0:
            raise EPWForecastError(
                f"EPW global horizontal radiation must be non-negative at line {line_number}."
            )

        key = (month, day, epw_hour)
        if key in rows:
            raise EPWForecastError(
                f"Duplicate EPW weather coordinate at line {line_number}: "
                f"month={month}, day={day}, hour={epw_hour}."
            )
        rows[key] = EPWHourlyWeather(
            month=month,
            day=day,
            epw_hour=epw_hour,
            drybulb_temperature_c=drybulb,
            global_horizontal_solar_w_m2=solar,
        )

    if not rows:
        raise EPWForecastError(f"Configured EPW contains no hourly data: {metadata.resolved_path}")
    return _EPWDataset(
        metadata=metadata,
        rows=rows,
        has_leap_day=any(month == 2 and day == 29 for month, day, _hour in rows),
    )


def _parse_integer(value: str, field_name: str, line_number: int) -> int:
    text = value.strip()
    try:
        parsed = int(text)
    except ValueError as exc:
        raise EPWForecastError(
            f"Invalid EPW {field_name} at line {line_number}: {text!r}; expected an integer."
        ) from exc
    return parsed


def _parse_finite_float(value: str, field_name: str, line_number: int) -> float:
    text = value.strip()
    try:
        parsed = float(text)
    except ValueError as exc:
        raise EPWForecastError(
            f"Invalid EPW {field_name} at line {line_number}: {text!r}; expected a number."
        ) from exc
    if not math.isfinite(parsed):
        raise EPWForecastError(
            f"Invalid EPW {field_name} at line {line_number}: value must be finite."
        )
    return parsed
