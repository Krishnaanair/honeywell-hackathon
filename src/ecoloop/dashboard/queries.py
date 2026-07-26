"""Read-only dashboard queries over the durable SQLite audit bus."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd


class DashboardDataError(RuntimeError):
    """Raised when dashboard data is missing or incompatible."""


@contextmanager
def readonly_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite database in read-only mode."""

    path = database_path.expanduser().resolve()
    if not path.is_file():
        raise DashboardDataError(f"Run database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


def list_runs(database_path: Path, *, include_fake: bool = False) -> pd.DataFrame:
    """Return recent runs; production dashboard queries exclude fake runs."""

    if include_fake:
        query = """
        SELECT run_id, timestamp, updated_at, run_type, status, is_fake,
               energyplus_version, model_path, weather_path, period_name,
               started_at, completed_at, progress_percent, error_summary,
               metadata_json
        FROM runs
        ORDER BY timestamp DESC
        """
    else:
        query = """
        SELECT run_id, timestamp, updated_at, run_type, status, is_fake,
               energyplus_version, model_path, weather_path, period_name,
               started_at, completed_at, progress_percent, error_summary,
               metadata_json
        FROM runs
        WHERE is_fake = 0
        ORDER BY timestamp DESC
        """
    return _read_frame(database_path, query)


def get_run(database_path: Path, run_id: str) -> dict[str, Any] | None:
    """Return one run as a dictionary."""

    with readonly_connection(database_path) as connection:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def telemetry(database_path: Path, run_id: str, *, limit: int | None = None) -> pd.DataFrame:
    """Return facility telemetry in simulated-time order."""

    if limit is not None:
        _validate_limit(limit)
        query = """
        SELECT simulation_timestamp, timestamp, environment,
               outdoor_temperature_c, facility_demand_kw,
               timestep_electricity_kwh, cumulative_electricity_kwh,
               hvac_electricity_kwh, heating_setpoint_c, cooling_setpoint_c
        FROM telemetry
        WHERE run_id = ?
        ORDER BY simulation_timestamp
        LIMIT ?
        """
        parameters: tuple[Any, ...] = (run_id, limit)
    else:
        query = """
        SELECT simulation_timestamp, timestamp, environment,
               outdoor_temperature_c, facility_demand_kw,
               timestep_electricity_kwh, cumulative_electricity_kwh,
               hvac_electricity_kwh, heating_setpoint_c, cooling_setpoint_c
        FROM telemetry
        WHERE run_id = ?
        ORDER BY simulation_timestamp
        """
        parameters = (run_id,)
    return _read_frame(
        database_path,
        query,
        parameters,
        parse_dates=("simulation_timestamp", "timestamp"),
    )


def zone_telemetry(database_path: Path, run_id: str, *, limit: int | None = None) -> pd.DataFrame:
    """Return zone telemetry in simulated-time and zone order."""

    if limit is not None:
        _validate_limit(limit)
        query = """
        SELECT simulation_timestamp, timestamp, zone_name,
               mean_air_temperature_c, operative_temperature_c,
               relative_humidity_percent, occupant_count, pmv, ppd_percent,
               co2_ppm, heating_setpoint_c, cooling_setpoint_c
        FROM zone_telemetry
        WHERE run_id = ?
        ORDER BY simulation_timestamp, zone_name
        LIMIT ?
        """
        parameters: tuple[Any, ...] = (run_id, limit)
    else:
        query = """
        SELECT simulation_timestamp, timestamp, zone_name,
               mean_air_temperature_c, operative_temperature_c,
               relative_humidity_percent, occupant_count, pmv, ppd_percent,
               co2_ppm, heating_setpoint_c, cooling_setpoint_c
        FROM zone_telemetry
        WHERE run_id = ?
        ORDER BY simulation_timestamp, zone_name
        """
        parameters = (run_id,)
    return _read_frame(
        database_path,
        query,
        parameters,
        parse_dates=("simulation_timestamp", "timestamp"),
    )


def recent_actions(database_path: Path, run_id: str, *, limit: int = 25) -> pd.DataFrame:
    """Return proposed actions with their applied values and validation."""

    _validate_limit(limit)
    frame = _read_frame(
        database_path,
        """
        SELECT timestamp, observation_id, action_generation, action_id,
               proposed_values_json, applied_values_json,
               validation_result_json, clamp_details_json, expiry, model,
               latency_ms, reason_code, explanation, fallback_status, cache_hit
        FROM proposed_actions
        WHERE run_id = ?
        ORDER BY observation_id DESC, action_generation DESC
        LIMIT ?
        """,
        (run_id, limit),
        parse_dates=("timestamp", "expiry"),
    )
    for column in (
        "proposed_values_json",
        "applied_values_json",
        "validation_result_json",
        "clamp_details_json",
    ):
        if column in frame:
            frame[column.removesuffix("_json")] = frame[column].map(_json_display)
    return frame


def recent_decisions(database_path: Path, run_id: str, *, limit: int = 50) -> pd.DataFrame:
    """Return local-model/rule decision summaries."""

    _validate_limit(limit)
    frame = _read_frame(
        database_path,
        """
        SELECT timestamp, observation_id, action_generation, model, latency_ms,
               reason_code, explanation, fallback_status,
               candidate_scores_json, state_summary_json, completed, timeout_count
        FROM agent_decisions
        WHERE run_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (run_id, limit),
        parse_dates=("timestamp",),
    )
    if "candidate_scores_json" in frame:
        frame["candidate_scores"] = frame["candidate_scores_json"].map(_json_display)
    if "state_summary_json" in frame:
        frame["state_summary"] = frame["state_summary_json"].map(_json_display)
    return frame


def recent_tool_calls(database_path: Path, run_id: str, *, limit: int = 50) -> pd.DataFrame:
    """Return recent genuine MCP tool calls."""

    _validate_limit(limit)
    return _read_frame(
        database_path,
        """
        SELECT timestamp, sequence, tool_name, success, duration_ms,
               control_affecting, error
        FROM tool_calls
        WHERE run_id = ?
        ORDER BY timestamp DESC, sequence DESC
        LIMIT ?
        """,
        (run_id, limit),
        parse_dates=("timestamp",),
    )


def errors_and_messages(database_path: Path, run_id: str, *, limit: int = 100) -> pd.DataFrame:
    """Return bounded error and simulation-message rows."""

    _validate_limit(limit)
    return _read_frame(
        database_path,
        """
        SELECT timestamp, severity, source, message, occurrence_count, last_seen_at
        FROM errors
        WHERE run_id = ?
        UNION ALL
        SELECT timestamp, severity, 'energyplus-message' AS source,
               message, occurrence_count, last_seen_at
        FROM simulation_messages
        WHERE run_id = ?
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        (run_id, run_id, limit),
        parse_dates=("timestamp", "last_seen_at"),
    )


def verified_metrics(database_path: Path, run_id: str) -> dict[str, dict[str, Any]]:
    """Return only metrics explicitly marked verified."""

    with readonly_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT metric_name, value, value_json, units, source, timestamp
            FROM metrics
            WHERE run_id = ? AND verified = 1
            ORDER BY metric_name
            """,
            (run_id,),
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        if item.get("value_json"):
            try:
                item["structured_value"] = json.loads(str(item["value_json"]))
            except json.JSONDecodeError:
                item["structured_value"] = None
        result[str(item["metric_name"])] = item
    return result


def compare_status(
    database_path: Path, baseline_run_id: str, controlled_run_id: str
) -> tuple[bool, str]:
    """Check whether two real completed runs are eligible for display comparison."""

    baseline = get_run(database_path, baseline_run_id)
    controlled = get_run(database_path, controlled_run_id)
    if baseline is None or controlled is None:
        return False, "One or both run IDs do not exist."
    if baseline["is_fake"] or controlled["is_fake"]:
        return False, "Fake runs are excluded from the production comparison."
    if baseline["run_type"] != "baseline":
        return False, "The reference run is not a baseline run."
    if controlled["run_type"] not in {"agent", "rule", "replay", "fixed_override"}:
        return False, "The selected controlled run type is not comparable."
    if baseline["status"] != "completed" or controlled["status"] != "completed":
        return False, "Both runs must be complete."
    for key, label in (
        ("energyplus_version", "EnergyPlus version"),
        ("weather_path", "weather"),
        ("period_name", "period"),
    ):
        if baseline.get(key) != controlled.get(key):
            return False, f"{label} does not match."
    baseline_metadata = _json_object(baseline.get("metadata_json"))
    controlled_metadata = _json_object(controlled.get("metadata_json"))
    baseline_fingerprint = _first_text(
        baseline_metadata,
        "preparation_fingerprint",
        "preparation_manifest_sha256",
        "model_preparation_sha256",
    )
    controlled_fingerprint = _first_text(
        controlled_metadata,
        "preparation_fingerprint",
        "preparation_manifest_sha256",
        "model_preparation_sha256",
    )
    if baseline_fingerprint is None or controlled_fingerprint is None:
        return False, "Model-preparation fingerprint is missing."
    if baseline_fingerprint != controlled_fingerprint:
        return False, "Model-preparation fingerprint does not match."
    baseline_metrics = verified_metrics(database_path, baseline_run_id)
    controlled_metrics = verified_metrics(database_path, controlled_run_id)
    if "final_run_metrics" not in baseline_metrics:
        return False, "Baseline final metrics are not verified."
    if "final_run_metrics" not in controlled_metrics:
        return False, "Controlled final metrics are not verified."
    return True, "Compatible completed real runs."


def aligned_cumulative_energy(
    database_path: Path, baseline_run_id: str, controlled_run_id: str
) -> pd.DataFrame:
    """Align cumulative telemetry for visualization only, by simulated timestamp."""

    baseline = telemetry(database_path, baseline_run_id)
    controlled = telemetry(database_path, controlled_run_id)
    required = {"simulation_timestamp", "cumulative_electricity_kwh"}
    if not required.issubset(baseline) or not required.issubset(controlled):
        return pd.DataFrame()
    return baseline[list(required)].merge(
        controlled[list(required)],
        on="simulation_timestamp",
        how="inner",
        suffixes=("_baseline", "_controlled"),
    )


def _read_frame(
    database_path: Path,
    query: str,
    parameters: tuple[Any, ...] = (),
    *,
    parse_dates: tuple[str, ...] = (),
) -> pd.DataFrame:
    with readonly_connection(database_path) as connection:
        frame = pd.read_sql_query(query, connection, params=parameters)
    for column in parse_dates:
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    return frame


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be in 1..10000")


def _json_display(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)
    return json.dumps(parsed, ensure_ascii=True, sort_keys=True)


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_text(values: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value:
            return value
    return None
