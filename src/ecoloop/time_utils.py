"""Time conversion helpers for deterministic EnergyPlus timestep identities."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta, tzinfo

UTC = UTC


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""

    return datetime.now(tz=UTC)


def require_aware(value: datetime) -> datetime:
    """Return *value* or raise when it has no unambiguous timezone."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value


def as_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC."""

    return require_aware(value).astimezone(UTC)


def isoformat_utc(value: datetime) -> str:
    """Serialize an aware datetime in canonical UTC ``Z`` form."""

    return as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""

    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return as_utc(parsed)


def energyplus_interval_datetime(
    *,
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    timezone_info: tzinfo = UTC,
) -> datetime:
    """Convert an EnergyPlus end-of-interval clock to a datetime.

    EnergyPlus tabular/output timestamps conventionally label sub-hourly
    intervals using hours 1..24 and minutes within that ending hour.  Therefore
    hour=1, minute=15 denotes 00:15, and hour=24, minute=60 denotes midnight on
    the following day.  This helper intentionally models that convention rather
    than guessing from a raw Runtime API clock.
    """

    if not 1 <= hour <= 24:
        raise ValueError("EnergyPlus interval hour must be in 1..24")
    if not 1 <= minute <= 60:
        raise ValueError("EnergyPlus interval minute must be in 1..60")
    if timezone_info.utcoffset(None) is None:
        # ZoneInfo may require an actual datetime to determine its offset, so
        # construct first and validate below instead of rejecting it here.
        pass
    try:
        local_midnight = datetime(year, month, day, tzinfo=timezone_info)
    except ValueError as exc:
        raise ValueError(
            f"invalid EnergyPlus calendar date: {year:04d}-{month:02d}-{day:02d}"
        ) from exc
    result = local_midnight + timedelta(hours=hour - 1, minutes=minute)
    return require_aware(result)


def runtime_clock_datetime(
    *,
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int = 0,
    timezone_info: tzinfo = UTC,
) -> datetime:
    """Convert a conventional 0..23 Runtime API clock without interval shifting."""

    if not 0 <= hour <= 23:
        raise ValueError("runtime hour must be in 0..23")
    if not 0 <= minute <= 59:
        raise ValueError("runtime minute must be in 0..59")
    if not 0 <= second <= 59:
        raise ValueError("runtime second must be in 0..59")
    try:
        result = datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone_info,
        )
    except ValueError as exc:
        raise ValueError("invalid Runtime API calendar clock") from exc
    return require_aware(result)


def timestep_key(
    *,
    run_id: str,
    environment: str,
    simulation_timestamp: datetime,
    zone_timestep_number: int,
) -> str:
    """Build a stable, compact identity for duplicate callback suppression."""

    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    if not environment.strip():
        raise ValueError("environment must not be empty")
    if zone_timestep_number < 1:
        raise ValueError("zone_timestep_number must be positive")
    canonical = "\x1f".join(
        (
            run_id.strip(),
            environment.strip().casefold(),
            isoformat_utc(simulation_timestamp),
            str(zone_timestep_number),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"ts-{digest}"


def elapsed_minutes(earlier: datetime, later: datetime) -> float:
    """Return signed elapsed minutes between two aware timestamps."""

    return (as_utc(later) - as_utc(earlier)).total_seconds() / 60.0


def quantize_datetime(value: datetime, interval_minutes: int) -> datetime:
    """Floor an aware timestamp to a deterministic minute interval."""

    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    utc_value = as_utc(value)
    epoch_minutes = int(utc_value.timestamp() // 60)
    quantized_minutes = epoch_minutes - (epoch_minutes % interval_minutes)
    return datetime.fromtimestamp(quantized_minutes * 60, tz=UTC)


def replace_simulation_year(value: datetime, target_year: int) -> datetime:
    """Replace a synthetic EnergyPlus year, handling leap day deterministically."""

    require_aware(value)
    try:
        return value.replace(year=target_year)
    except ValueError:
        if value.month == 2 and value.day == 29:
            return value.replace(year=target_year, day=28)
        raise


def inclusive_day_count(start: date, end: date) -> int:
    """Return inclusive calendar-day count for a valid run period."""

    if end < start:
        raise ValueError("end date must not precede start date")
    return (end - start).days + 1
