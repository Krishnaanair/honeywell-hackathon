"""EnergyPlus and audit timestamp conversion tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from ecoloop.time_utils import (
    as_utc,
    elapsed_minutes,
    energyplus_interval_datetime,
    inclusive_day_count,
    isoformat_utc,
    parse_iso_datetime,
    quantize_datetime,
    replace_simulation_year,
    runtime_clock_datetime,
    timestep_key,
)


def test_energyplus_interval_clock_uses_end_of_interval_convention() -> None:
    assert energyplus_interval_datetime(year=2026, month=7, day=15, hour=1, minute=15) == datetime(
        2026, 7, 15, 0, 15, tzinfo=UTC
    )
    assert energyplus_interval_datetime(year=2026, month=7, day=15, hour=1, minute=60) == datetime(
        2026, 7, 15, 1, 0, tzinfo=UTC
    )
    assert energyplus_interval_datetime(year=2026, month=7, day=15, hour=24, minute=60) == datetime(
        2026, 7, 16, 0, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("hour", "minute"),
    [(0, 15), (25, 15), (1, 0), (1, 61)],
)
def test_energyplus_interval_clock_rejects_invalid_components(hour: int, minute: int) -> None:
    with pytest.raises(ValueError):
        energyplus_interval_datetime(
            year=2026,
            month=7,
            day=15,
            hour=hour,
            minute=minute,
        )


def test_runtime_clock_is_not_shifted() -> None:
    result = runtime_clock_datetime(
        year=2026,
        month=7,
        day=15,
        hour=0,
        minute=15,
        second=30,
    )
    assert result == datetime(2026, 7, 15, 0, 15, 30, tzinfo=UTC)


def test_iso_round_trip_normalizes_offsets_to_utc() -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 7, 15, 15, 30, tzinfo=india)
    encoded = isoformat_utc(local)
    assert encoded == "2026-07-15T10:00:00.000000Z"
    assert parse_iso_datetime(encoded) == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    assert as_utc(local) == parse_iso_datetime(encoded)


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        as_utc(datetime(2026, 7, 15))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone"):
        parse_iso_datetime("2026-07-15T10:00:00")


def test_timestep_key_is_stable_and_sensitive_to_identity() -> None:
    timestamp = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    first = timestep_key(
        run_id="run-1",
        environment="RUN PERIOD 1",
        simulation_timestamp=timestamp,
        zone_timestep_number=4,
    )
    same = timestep_key(
        run_id="run-1",
        environment="run period 1",
        simulation_timestamp=timestamp,
        zone_timestep_number=4,
    )
    changed = timestep_key(
        run_id="run-1",
        environment="run period 1",
        simulation_timestamp=timestamp + timedelta(minutes=15),
        zone_timestep_number=4,
    )
    assert first == same
    assert first != changed
    assert first.startswith("ts-")


def test_elapsed_and_quantization_use_utc_instants() -> None:
    india = timezone(timedelta(hours=5, minutes=30))
    earlier = datetime(2026, 7, 15, 15, 31, tzinfo=india)
    later = datetime(2026, 7, 15, 10, 16, tzinfo=UTC)
    assert elapsed_minutes(earlier, later) == 15.0
    assert quantize_datetime(earlier, 15) == datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def test_replace_synthetic_leap_year_is_deterministic() -> None:
    leap_day = datetime(2024, 2, 29, 12, tzinfo=UTC)
    assert replace_simulation_year(leap_day, 2025) == datetime(2025, 2, 28, 12, tzinfo=UTC)


def test_inclusive_day_count_validates_period_order() -> None:
    assert inclusive_day_count(date(2026, 7, 15), date(2026, 7, 21)) == 7
    with pytest.raises(ValueError, match="precede"):
        inclusive_day_count(date(2026, 7, 21), date(2026, 7, 15))
