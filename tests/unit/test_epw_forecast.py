from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecoloop.energyplus.epw_forecast import (
    VERIFIED_CONFIGURED_EPW_SOURCE,
    EPWForecastError,
    forecast_context_from_epw,
)

_HEADERS = (
    "LOCATION,Test City,Test State,IND,TMYx,000000,0.0,0.0,5.5,10.0",
    "DESIGN CONDITIONS,0",
    "TYPICAL/EXTREME PERIODS,0",
    "GROUND TEMPERATURES,0",
    "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0",
    "COMMENTS 1,Tiny deterministic fixture",
    "COMMENTS 2,Only forecast fields are populated",
    "DATA PERIODS,1,1,Data,Sunday,1/1,12/31",
)


def _weather_row(
    month: int,
    day: int,
    hour: int,
    drybulb_c: object,
    solar_wh_m2: object,
) -> str:
    fields: list[object] = [
        2021,
        month,
        day,
        hour,
        60,
        "?9?9?9?9",
        drybulb_c,
        10.0,
        50,
        101325,
        0,
        0,
        300,
        solar_wh_m2,
    ]
    return ",".join(str(field) for field in fields)


def _write_epw(path: Path, rows: list[str]) -> Path:
    path.write_text("\n".join((*_HEADERS, *rows)) + "\n", encoding="utf-8")
    return path


def test_forecast_returns_bounded_horizon_and_aggregate_features(tmp_path: Path) -> None:
    epw = _write_epw(
        tmp_path / "weather.epw",
        [
            _weather_row(7, 1, 11, 30.0, 100.0),
            _weather_row(7, 1, 12, 33.0, 500.0),
            _weather_row(7, 1, 13, 36.0, 900.0),
        ],
    )

    context = forecast_context_from_epw(
        epw,
        datetime(2026, 7, 1, 10, 15, tzinfo=UTC),
        hours=3,
    )

    assert context.source == VERIFIED_CONFIGURED_EPW_SOURCE
    assert context.horizon_hours == 3
    assert [point.offset_hours for point in context.points] == [1, 2, 3]
    assert [point.timestamp.hour for point in context.points] == [11, 12, 13]
    assert [point.epw_hour for point in context.points] == [11, 12, 13]
    assert context.temperature_mean_c == pytest.approx(33.0)
    assert context.temperature_max_c == pytest.approx(36.0)
    assert context.solar_mean_w_m2 == pytest.approx(500.0)
    assert len(context.file_metadata.sha256) == 64
    assert context.file_metadata.resolved_path == epw.resolve()


@pytest.mark.parametrize("hours", [0, 169, True, 1.5])
def test_forecast_rejects_horizon_outside_one_to_168(
    tmp_path: Path,
    hours: object,
) -> None:
    epw = _write_epw(tmp_path / "weather.epw", [_weather_row(1, 1, 1, 20.0, 0.0)])

    with pytest.raises(EPWForecastError, match="1 through 168"):
        forecast_context_from_epw(
            epw,
            datetime(2026, 1, 1, tzinfo=UTC),
            hours=hours,  # type: ignore[arg-type]
        )


def test_forecast_maps_midnight_to_epw_hour_24_and_rolls_year(tmp_path: Path) -> None:
    epw = _write_epw(
        tmp_path / "weather.epw",
        [
            _weather_row(12, 31, 24, 18.0, 0.0),
            _weather_row(1, 1, 1, 17.0, 0.0),
            _weather_row(1, 1, 2, 16.0, 0.0),
        ],
    )

    context = forecast_context_from_epw(
        epw,
        datetime(2026, 12, 31, 23, 45, tzinfo=UTC),
        hours=3,
    )

    assert [point.timestamp for point in context.points] == [
        datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2027, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2027, 1, 1, 2, 0, tzinfo=UTC),
    ]
    assert [(point.epw_month, point.epw_day, point.epw_hour) for point in context.points] == [
        (12, 31, 24),
        (1, 1, 1),
        (1, 1, 2),
    ]


def test_forecast_repeats_february_28_when_epw_has_no_leap_day(tmp_path: Path) -> None:
    epw = _write_epw(
        tmp_path / "weather.epw",
        [_weather_row(2, 28, 1, 24.0, 100.0)],
    )

    context = forecast_context_from_epw(
        epw,
        datetime(2024, 2, 29, 0, 20, tzinfo=UTC),
        hours=1,
    )

    point = context.points[0]
    assert point.timestamp == datetime(2024, 2, 29, 1, 0, tzinfo=UTC)
    assert (point.epw_month, point.epw_day, point.epw_hour) == (2, 28, 1)
    assert point.leap_day_fallback is True


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("2021,1,1,1", "expected at least 14 columns"),
        (_weather_row(1, 1, 1, "not-a-number", 0.0), "expected a number"),
        (_weather_row(1, 1, 1, "nan", 0.0), "must be finite"),
        (_weather_row(1, 1, 1, 20.0, -1.0), "must be non-negative"),
        (_weather_row(1, 1, 1, 99.9, 0.0), "Missing EPW dry-bulb"),
        (_weather_row(1, 1, 1, 20.0, 9999), "Missing EPW global horizontal"),
    ],
)
def test_forecast_rejects_invalid_data_rows(
    tmp_path: Path,
    row: str,
    message: str,
) -> None:
    epw = _write_epw(tmp_path / "invalid.epw", [row])

    with pytest.raises(EPWForecastError, match=message):
        forecast_context_from_epw(
            epw,
            datetime(2026, 1, 1, tzinfo=UTC),
            hours=1,
        )


@pytest.mark.parametrize("hour", [0, 25])
def test_forecast_rejects_invalid_epw_hour(tmp_path: Path, hour: int) -> None:
    epw = _write_epw(
        tmp_path / "invalid-hour.epw",
        [_weather_row(1, 1, hour, 20.0, 0.0)],
    )

    with pytest.raises(EPWForecastError, match="expected 1 through 24"):
        forecast_context_from_epw(
            epw,
            datetime(2026, 1, 1, tzinfo=UTC),
            hours=1,
        )


def test_forecast_reports_missing_epw_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.epw"

    with pytest.raises(EPWForecastError, match="does not exist"):
        forecast_context_from_epw(
            missing,
            datetime(2026, 1, 1, tzinfo=UTC),
            hours=1,
        )


def test_forecast_cache_is_invalidated_by_content_identity(tmp_path: Path) -> None:
    epw = _write_epw(tmp_path / "weather.epw", [_weather_row(1, 1, 1, 20.0, 0.0)])
    original_stat = epw.stat()
    first = forecast_context_from_epw(
        epw,
        datetime(2026, 1, 1, tzinfo=UTC),
        hours=1,
    )

    _write_epw(epw, [_weather_row(1, 1, 1, 30.0, 0.0)])
    os.utime(epw, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = forecast_context_from_epw(
        epw,
        datetime(2026, 1, 1, tzinfo=UTC),
        hours=1,
    )

    assert first.points[0].drybulb_temperature_c == 20.0
    assert second.points[0].drybulb_temperature_c == 30.0
    assert first.file_metadata.sha256 != second.file_metadata.sha256
