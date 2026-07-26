"""Read-only dashboard queries over the durable SQLite audit bus."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import pandas as pd


class DashboardDataError(RuntimeError):
    """Raised when dashboard data is missing or incompatible."""


class NumericSummary(TypedDict):
    """Stable numeric summary used by dashboard statistic helpers."""

    minimum: float | None
    mean: float | None
    maximum: float | None
    latest: float | None


class RunStatistics(TypedDict):
    """Real-run operational, reliability, completeness, and provenance statistics."""

    run_id: str
    run_type: str
    status: str
    period_name: str | None
    is_fake: bool
    energyplus_version: str | None
    model_path: str | None
    weather_path: str | None
    started_at: str | None
    completed_at: str | None
    progress_percent: float | None
    telemetry_steps: int
    zone_rows: int
    distinct_zones: int
    observation_count: int
    proposed_action_count: int
    applied_action_count: int
    action_application_percent: float | None
    decision_count: int
    incomplete_decision_count: int
    decision_completion_percent: float | None
    tool_call_count: int
    successful_tool_call_count: int
    failed_tool_call_count: int
    control_affecting_tool_call_count: int
    timeout_count: int
    fallback_count: int
    safety_clamp_count: int
    invalid_action_count: int | None
    average_decision_latency_ms: float | None
    p95_decision_latency_ms: float | None
    average_tool_latency_ms: float | None
    p95_tool_latency_ms: float | None
    telemetry_start: str | None
    telemetry_end: str | None
    latest_simulation_timestamp: str | None
    latest_facility_demand_kw: float | None
    latest_cumulative_electricity_kwh: float | None
    latest_occupancy_count: float | None
    latest_operative_temperature_mean_c: float | None
    latest_operative_temperature_min_c: float | None
    latest_operative_temperature_max_c: float | None
    heating_setpoint: NumericSummary
    cooling_setpoint: NumericSummary
    applied_heating_setpoint: NumericSummary
    applied_cooling_setpoint: NumericSummary
    applied_hold_minutes: NumericSummary
    action_model_counts: dict[str, int]
    latest_action_model: str | None
    latest_action_reason_code: str | None
    latest_action_fallback_status: str | None
    zone_row_completeness_percent: float | None
    telemetry_field_coverage_percent: dict[str, float]
    comfort_field_coverage_percent: dict[str, float]
    verified_metric_count: int
    verified_metric_sources: tuple[str, ...]
    final_metrics_verified: bool
    energy_cross_check_passed: bool | None
    finalization_verified_for_comparison: bool | None
    data_origin: str | None
    preparation_fingerprint: str | None
    weather_sha256: str | None
    input_model_sha256: str | None


class ComparisonMetricStatistics(TypedDict):
    """One verified baseline-to-controlled metric comparison."""

    baseline: float | None
    controlled: float | None
    absolute_delta: float | None
    percent_delta: float | None
    units: str | None
    lower_is_better: bool


class ComparisonStatistics(TypedDict):
    """Stable compatibility and metric-delta result for two real runs."""

    baseline_run_id: str
    controlled_run_id: str
    compatible: bool
    message: str
    preparation_fingerprint_match: bool | None
    weather_sha256_match: bool | None
    energy_cross_checks_passed: bool | None
    metrics: dict[str, ComparisonMetricStatistics]


COMFORT_DISTRIBUTION_COLUMNS = (
    "zone_name",
    "samples",
    "occupied_samples",
    "operative_temperature_mean_c",
    "operative_temperature_min_c",
    "operative_temperature_p05_c",
    "operative_temperature_p50_c",
    "operative_temperature_p95_c",
    "operative_temperature_max_c",
    "occupied_operative_temperature_mean_c",
    "occupied_operative_temperature_min_c",
    "occupied_operative_temperature_max_c",
    "occupied_pmv_mean",
    "occupied_pmv_p05",
    "occupied_pmv_p50",
    "occupied_pmv_p95",
    "occupied_pmv_max_abs",
    "occupied_pmv_compliance_percent",
    "occupied_mean_ppd_percent",
    "relative_humidity_mean_percent",
    "relative_humidity_p95_percent",
    "co2_mean_ppm",
    "co2_max_ppm",
    "operative_temperature_coverage_percent",
    "pmv_coverage_percent",
    "co2_coverage_percent",
)


_COMPARISON_METRICS: dict[str, bool] = {
    "facility_electricity_kwh": True,
    "hvac_electricity_kwh": True,
    "peak_electrical_demand_kw": True,
    "cost": True,
    "operational_carbon_kg": True,
    "occupied_temperature_violation_percent": True,
    "occupied_temperature_violation_degree_hours": True,
    "pmv_compliance_percent": False,
    "mean_ppd_percent": True,
    "llm_decision_count": True,
    "tool_call_count": True,
    "average_decision_latency_ms": True,
    "p95_decision_latency_ms": True,
    "timeout_count": True,
    "fallback_count": True,
    "invalid_action_count": True,
    "safety_clamp_count": True,
}


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
    """Return proposed actions, validation, and physical-application status."""

    _validate_limit(limit)
    frame = _read_frame(
        database_path,
        """
        SELECT p.timestamp, p.observation_id, p.action_generation, p.action_id,
               p.proposed_values_json, p.applied_values_json,
               p.validation_result_json, p.clamp_details_json, p.expiry, p.model,
               p.latency_ms, p.reason_code, p.explanation, p.fallback_status,
               p.cache_hit,
               CASE WHEN a.action_id IS NULL THEN 0 ELSE 1 END AS applied
        FROM proposed_actions AS p
        LEFT JOIN applied_actions AS a ON a.action_id = p.action_id
        WHERE p.run_id = ?
        ORDER BY p.observation_id DESC, p.action_generation DESC
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
    baseline_weather_sha = _first_text(baseline_metadata, "weather_sha256")
    controlled_weather_sha = _first_text(controlled_metadata, "weather_sha256")
    if baseline_weather_sha is None or controlled_weather_sha is None:
        return False, "Weather-content checksum is missing."
    if baseline_weather_sha != controlled_weather_sha:
        return False, "Weather-content checksum does not match."
    baseline_metrics = verified_metrics(database_path, baseline_run_id)
    controlled_metrics = verified_metrics(database_path, controlled_run_id)
    if "final_run_metrics" not in baseline_metrics:
        return False, "Baseline final metrics are not verified."
    if "final_run_metrics" not in controlled_metrics:
        return False, "Controlled final metrics are not verified."
    for label, metrics in (
        ("Baseline", baseline_metrics),
        ("Controlled", controlled_metrics),
    ):
        cross_check = _metric_payload(metrics, "energy_cross_check")
        if cross_check.get("passed") is not True:
            return False, f"{label} energy cross-check has not passed."
        finalization = _metric_payload(metrics, "finalization_verification")
        if finalization.get("verified_for_comparison") is not True:
            return False, f"{label} finalization is not verified for comparison."
    baseline_final = _metric_payload(baseline_metrics, "final_run_metrics")
    controlled_final = _metric_payload(controlled_metrics, "final_run_metrics")
    baseline_window = (
        baseline_final.get("simulation_start"),
        baseline_final.get("simulation_end"),
    )
    controlled_window = (
        controlled_final.get("simulation_start"),
        controlled_final.get("simulation_end"),
    )
    if any(value is None for value in (*baseline_window, *controlled_window)):
        return False, "Finalized simulation-window provenance is missing."
    if baseline_window != controlled_window:
        return False, "Finalized simulation windows do not match."
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


def run_statistics(database_path: Path, run_id: str) -> RunStatistics:
    """Summarize only persisted rows for one real run without extrapolation."""

    with readonly_connection(database_path) as connection:
        run = _require_real_run(connection, run_id)
        facility = connection.execute(
            """
            SELECT COUNT(*) AS telemetry_steps,
                   MIN(simulation_timestamp) AS telemetry_start,
                   MAX(simulation_timestamp) AS telemetry_end,
                   MIN(heating_setpoint_c) AS heating_min,
                   AVG(heating_setpoint_c) AS heating_mean,
                   MAX(heating_setpoint_c) AS heating_max,
                   MIN(cooling_setpoint_c) AS cooling_min,
                   AVG(cooling_setpoint_c) AS cooling_mean,
                   MAX(cooling_setpoint_c) AS cooling_max
            FROM telemetry
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        latest_facility = connection.execute(
            """
            SELECT simulation_timestamp, facility_demand_kw,
                   cumulative_electricity_kwh, heating_setpoint_c,
                   cooling_setpoint_c
            FROM telemetry
            WHERE run_id = ?
            ORDER BY simulation_timestamp DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        zone_counts = connection.execute(
            """
            SELECT COUNT(*) AS zone_rows,
                   COUNT(DISTINCT zone_name) AS distinct_zones
            FROM zone_telemetry
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        latest_zone = connection.execute(
            """
            SELECT SUM(occupant_count) AS occupancy_count,
                   AVG(operative_temperature_c) AS operative_mean,
                   MIN(operative_temperature_c) AS operative_min,
                   MAX(operative_temperature_c) AS operative_max
            FROM zone_telemetry
            WHERE run_id = ?
              AND simulation_timestamp = (
                  SELECT MAX(simulation_timestamp)
                  FROM zone_telemetry
                  WHERE run_id = ?
              )
            """,
            (run_id, run_id),
        ).fetchone()
        observation_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        decisions = connection.execute(
            """
            SELECT COUNT(*) AS decision_count,
                   SUM(CASE WHEN completed = 0 THEN 1 ELSE 0 END) AS incomplete_count,
                   SUM(timeout_count) AS timeout_count,
                   AVG(latency_ms) AS average_latency_ms
            FROM agent_decisions
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        decision_latencies = [
            float(row[0])
            for row in connection.execute(
                "SELECT latency_ms FROM agent_decisions WHERE run_id = ?",
                (run_id,),
            )
            if row[0] is not None
        ]
        tools = connection.execute(
            """
            SELECT COUNT(*) AS tool_count,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successful_count,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed_count,
                   SUM(CASE WHEN control_affecting = 1 THEN 1 ELSE 0 END)
                       AS control_affecting_count,
                   AVG(duration_ms) AS average_latency_ms
            FROM tool_calls
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        tool_latencies = [
            float(row[0])
            for row in connection.execute(
                "SELECT duration_ms FROM tool_calls WHERE run_id = ?",
                (run_id,),
            )
            if row[0] is not None
        ]
        action_counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM proposed_actions WHERE run_id = ?) AS proposed_count,
                (SELECT COUNT(*) FROM applied_actions WHERE run_id = ?) AS applied_count
            """,
            (run_id, run_id),
        ).fetchone()
        applied_rows = connection.execute(
            """
            SELECT applied_values_json, clamp_details_json, fallback_status, model,
                   reason_code
            FROM applied_actions
            WHERE run_id = ?
            ORDER BY action_generation
            """,
            (run_id,),
        ).fetchall()
        model_counts = {
            str(row["model"]): int(row["model_count"])
            for row in connection.execute(
                """
                SELECT model, COUNT(*) AS model_count
                FROM applied_actions
                WHERE run_id = ?
                GROUP BY model
                ORDER BY model
                """,
                (run_id,),
            )
        }
        verified_rows = connection.execute(
            """
            SELECT metric_name, value, value_json, units, source
            FROM metrics
            WHERE run_id = ? AND verified = 1
            ORDER BY metric_name
            """,
            (run_id,),
        ).fetchall()
        telemetry_coverage = _field_coverage(
            connection,
            table="telemetry",
            run_id=run_id,
            fields=(
                "outdoor_temperature_c",
                "facility_demand_kw",
                "timestep_electricity_kwh",
                "cumulative_electricity_kwh",
                "hvac_electricity_kwh",
                "heating_setpoint_c",
                "cooling_setpoint_c",
            ),
        )
        comfort_coverage = _field_coverage(
            connection,
            table="zone_telemetry",
            run_id=run_id,
            fields=(
                "operative_temperature_c",
                "relative_humidity_percent",
                "occupant_count",
                "pmv",
                "ppd_percent",
                "co2_ppm",
            ),
        )

    verified = {str(row["metric_name"]): dict(row) for row in verified_rows}
    metadata = _json_object(run.get("metadata_json"))
    final_metrics_verified = "final_run_metrics" in verified
    proposed_count = int(action_counts["proposed_count"])
    applied_count = int(action_counts["applied_count"])
    decision_count = int(decisions["decision_count"])
    incomplete_count = int(decisions["incomplete_count"] or 0)
    telemetry_steps = int(facility["telemetry_steps"])
    zone_rows = int(zone_counts["zone_rows"])
    distinct_zones = int(zone_counts["distinct_zones"])
    expected_zone_rows = telemetry_steps * distinct_zones
    parsed_actions = [
        value for row in applied_rows if (value := _json_object(row["applied_values_json"]))
    ]
    fallback_from_rows = sum(_active_fallback(row["fallback_status"]) for row in applied_rows)
    clamps_from_rows = sum(bool(_json_value(row["clamp_details_json"])) for row in applied_rows)
    latest_action = applied_rows[-1] if applied_rows else None
    energy_cross_check = _verified_payload(verified, "energy_cross_check")
    finalization = _verified_payload(verified, "finalization_verification")
    verified_sources = tuple(sorted({str(row["source"]) for row in verified_rows}))
    return RunStatistics(
        run_id=str(run["run_id"]),
        run_type=str(run["run_type"]),
        status=str(run["status"]),
        period_name=_optional_text(run.get("period_name")),
        is_fake=False,
        energyplus_version=_optional_text(run.get("energyplus_version")),
        model_path=_optional_text(run.get("model_path")),
        weather_path=_optional_text(run.get("weather_path")),
        started_at=_optional_text(run.get("started_at")),
        completed_at=_optional_text(run.get("completed_at")),
        progress_percent=_optional_float(run.get("progress_percent")),
        telemetry_steps=telemetry_steps,
        zone_rows=zone_rows,
        distinct_zones=distinct_zones,
        observation_count=observation_count,
        proposed_action_count=proposed_count,
        applied_action_count=applied_count,
        action_application_percent=_ratio_percent(applied_count, proposed_count),
        decision_count=decision_count,
        incomplete_decision_count=incomplete_count,
        decision_completion_percent=_ratio_percent(
            decision_count - incomplete_count,
            decision_count,
        ),
        tool_call_count=_verified_or_count(verified, "tool_call_count", int(tools["tool_count"])),
        successful_tool_call_count=int(tools["successful_count"] or 0),
        failed_tool_call_count=int(tools["failed_count"] or 0),
        control_affecting_tool_call_count=int(tools["control_affecting_count"] or 0),
        timeout_count=_verified_or_count(
            verified,
            "timeout_count",
            int(decisions["timeout_count"] or 0),
        ),
        fallback_count=_verified_or_count(
            verified,
            "fallback_count",
            fallback_from_rows,
        ),
        safety_clamp_count=_verified_or_count(
            verified,
            "safety_clamp_count",
            clamps_from_rows,
        ),
        invalid_action_count=_verified_optional_count(verified, "invalid_action_count"),
        average_decision_latency_ms=_verified_or_float(
            verified,
            "average_decision_latency_ms",
            _optional_float(decisions["average_latency_ms"]),
        ),
        p95_decision_latency_ms=_verified_or_float(
            verified,
            "p95_decision_latency_ms",
            _percentile(decision_latencies, 0.95),
        ),
        average_tool_latency_ms=_optional_float(tools["average_latency_ms"]),
        p95_tool_latency_ms=_percentile(tool_latencies, 0.95),
        telemetry_start=_optional_text(facility["telemetry_start"]),
        telemetry_end=_optional_text(facility["telemetry_end"]),
        latest_simulation_timestamp=(
            _optional_text(latest_facility["simulation_timestamp"])
            if latest_facility is not None
            else None
        ),
        latest_facility_demand_kw=(
            _optional_float(latest_facility["facility_demand_kw"])
            if latest_facility is not None
            else None
        ),
        latest_cumulative_electricity_kwh=(
            _optional_float(latest_facility["cumulative_electricity_kwh"])
            if latest_facility is not None
            else None
        ),
        latest_occupancy_count=_optional_float(latest_zone["occupancy_count"]),
        latest_operative_temperature_mean_c=_optional_float(latest_zone["operative_mean"]),
        latest_operative_temperature_min_c=_optional_float(latest_zone["operative_min"]),
        latest_operative_temperature_max_c=_optional_float(latest_zone["operative_max"]),
        heating_setpoint=NumericSummary(
            minimum=_optional_float(facility["heating_min"]),
            mean=_optional_float(facility["heating_mean"]),
            maximum=_optional_float(facility["heating_max"]),
            latest=(
                _optional_float(latest_facility["heating_setpoint_c"])
                if latest_facility is not None
                else None
            ),
        ),
        cooling_setpoint=NumericSummary(
            minimum=_optional_float(facility["cooling_min"]),
            mean=_optional_float(facility["cooling_mean"]),
            maximum=_optional_float(facility["cooling_max"]),
            latest=(
                _optional_float(latest_facility["cooling_setpoint_c"])
                if latest_facility is not None
                else None
            ),
        ),
        applied_heating_setpoint=_action_numeric_summary(
            parsed_actions,
            "heating_setpoint_c",
        ),
        applied_cooling_setpoint=_action_numeric_summary(
            parsed_actions,
            "cooling_setpoint_c",
        ),
        applied_hold_minutes=_action_numeric_summary(parsed_actions, "hold_minutes"),
        action_model_counts=model_counts,
        latest_action_model=(
            _optional_text(latest_action["model"]) if latest_action is not None else None
        ),
        latest_action_reason_code=(
            _optional_text(latest_action["reason_code"]) if latest_action is not None else None
        ),
        latest_action_fallback_status=(
            _optional_text(latest_action["fallback_status"]) if latest_action is not None else None
        ),
        zone_row_completeness_percent=_ratio_percent(zone_rows, expected_zone_rows),
        telemetry_field_coverage_percent=telemetry_coverage,
        comfort_field_coverage_percent=comfort_coverage,
        verified_metric_count=len(verified_rows),
        verified_metric_sources=verified_sources,
        final_metrics_verified=final_metrics_verified,
        energy_cross_check_passed=_optional_bool(energy_cross_check.get("passed")),
        finalization_verified_for_comparison=_optional_bool(
            finalization.get("verified_for_comparison")
        ),
        data_origin=_optional_text(metadata.get("data_origin")),
        preparation_fingerprint=_first_text(
            metadata,
            "preparation_fingerprint",
            "preparation_manifest_sha256",
        ),
        weather_sha256=_first_text(metadata, "weather_sha256"),
        input_model_sha256=_first_text(metadata, "input_model_sha256"),
    )


def comparison_statistics(
    database_path: Path,
    baseline_run_id: str,
    controlled_run_id: str,
) -> ComparisonStatistics:
    """Return verified deltas only when two completed real runs are compatible."""

    compatible, message = compare_status(database_path, baseline_run_id, controlled_run_id)
    baseline = get_run(database_path, baseline_run_id)
    controlled = get_run(database_path, controlled_run_id)
    baseline_metadata = _json_object(baseline.get("metadata_json") if baseline else None)
    controlled_metadata = _json_object(controlled.get("metadata_json") if controlled else None)
    fingerprint_match = _matching_metadata_value(
        baseline_metadata,
        controlled_metadata,
        "preparation_fingerprint",
        "preparation_manifest_sha256",
    )
    weather_match = _matching_metadata_value(
        baseline_metadata,
        controlled_metadata,
        "weather_sha256",
    )
    if not compatible:
        return ComparisonStatistics(
            baseline_run_id=baseline_run_id,
            controlled_run_id=controlled_run_id,
            compatible=False,
            message=message,
            preparation_fingerprint_match=fingerprint_match,
            weather_sha256_match=weather_match,
            energy_cross_checks_passed=None,
            metrics={},
        )

    baseline_metrics = verified_metrics(database_path, baseline_run_id)
    controlled_metrics = verified_metrics(database_path, controlled_run_id)
    metric_statistics: dict[str, ComparisonMetricStatistics] = {}
    for metric_name, lower_is_better in _COMPARISON_METRICS.items():
        baseline_value = _metric_number(baseline_metrics, metric_name)
        controlled_value = _metric_number(controlled_metrics, metric_name)
        absolute_delta = (
            controlled_value - baseline_value
            if baseline_value is not None and controlled_value is not None
            else None
        )
        percent_delta = (
            absolute_delta / baseline_value * 100
            if absolute_delta is not None and baseline_value is not None and baseline_value != 0
            else None
        )
        baseline_units = _metric_units(baseline_metrics, metric_name)
        controlled_units = _metric_units(controlled_metrics, metric_name)
        metric_statistics[metric_name] = ComparisonMetricStatistics(
            baseline=baseline_value,
            controlled=controlled_value,
            absolute_delta=absolute_delta,
            percent_delta=percent_delta,
            units=baseline_units if baseline_units == controlled_units else None,
            lower_is_better=lower_is_better,
        )
    baseline_crosscheck = _metric_payload(baseline_metrics, "energy_cross_check")
    controlled_crosscheck = _metric_payload(controlled_metrics, "energy_cross_check")
    crosscheck_values = (
        _optional_bool(baseline_crosscheck.get("passed")),
        _optional_bool(controlled_crosscheck.get("passed")),
    )
    crosschecks_passed = (
        all(value is True for value in crosscheck_values)
        if all(value is not None for value in crosscheck_values)
        else None
    )
    return ComparisonStatistics(
        baseline_run_id=baseline_run_id,
        controlled_run_id=controlled_run_id,
        compatible=True,
        message=message,
        preparation_fingerprint_match=fingerprint_match,
        weather_sha256_match=weather_match,
        energy_cross_checks_passed=crosschecks_passed,
        metrics=metric_statistics,
    )


def comfort_distribution(
    database_path: Path,
    run_id: str,
    *,
    pmv_limit: float = 0.7,
    simulation_end: str | datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Return per-zone distributions from persisted real zone telemetry.

    ``simulation_end`` bounds replay summaries to the visible simulated frame.
    """

    if pmv_limit <= 0:
        raise ValueError("pmv_limit must be positive")
    with readonly_connection(database_path) as connection:
        _require_real_run(connection, run_id)
    frame = zone_telemetry(database_path, run_id)
    if simulation_end is not None and not frame.empty:
        cutoff = pd.Timestamp(simulation_end)
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        frame = frame[frame["simulation_timestamp"] <= cutoff]
    if frame.empty:
        return pd.DataFrame(columns=COMFORT_DISTRIBUTION_COLUMNS)
    rows: list[dict[str, Any]] = []
    for zone_name, group in frame.groupby("zone_name", sort=True):
        occupied = group[group["occupant_count"].fillna(0) > 0]
        operative = group["operative_temperature_c"].dropna()
        occupied_operative = occupied["operative_temperature_c"].dropna()
        occupied_pmv = occupied["pmv"].dropna()
        occupied_ppd = occupied["ppd_percent"].dropna()
        humidity = group["relative_humidity_percent"].dropna()
        co2 = group["co2_ppm"].dropna()
        rows.append(
            {
                "zone_name": str(zone_name),
                "samples": len(group),
                "occupied_samples": len(occupied),
                "operative_temperature_mean_c": _series_mean(operative),
                "operative_temperature_min_c": _series_min(operative),
                "operative_temperature_p05_c": _series_quantile(operative, 0.05),
                "operative_temperature_p50_c": _series_quantile(operative, 0.50),
                "operative_temperature_p95_c": _series_quantile(operative, 0.95),
                "operative_temperature_max_c": _series_max(operative),
                "occupied_operative_temperature_mean_c": _series_mean(occupied_operative),
                "occupied_operative_temperature_min_c": _series_min(occupied_operative),
                "occupied_operative_temperature_max_c": _series_max(occupied_operative),
                "occupied_pmv_mean": _series_mean(occupied_pmv),
                "occupied_pmv_p05": _series_quantile(occupied_pmv, 0.05),
                "occupied_pmv_p50": _series_quantile(occupied_pmv, 0.50),
                "occupied_pmv_p95": _series_quantile(occupied_pmv, 0.95),
                "occupied_pmv_max_abs": _series_max(occupied_pmv.abs()),
                "occupied_pmv_compliance_percent": (
                    float((occupied_pmv.abs() <= pmv_limit).mean() * 100)
                    if not occupied_pmv.empty
                    else None
                ),
                "occupied_mean_ppd_percent": _series_mean(occupied_ppd),
                "relative_humidity_mean_percent": _series_mean(humidity),
                "relative_humidity_p95_percent": _series_quantile(humidity, 0.95),
                "co2_mean_ppm": _series_mean(co2),
                "co2_max_ppm": _series_max(co2),
                "operative_temperature_coverage_percent": _ratio_percent(
                    int(group["operative_temperature_c"].notna().sum()),
                    len(group),
                ),
                "pmv_coverage_percent": _ratio_percent(
                    int(group["pmv"].notna().sum()),
                    len(group),
                ),
                "co2_coverage_percent": _ratio_percent(
                    int(group["co2_ppm"].notna().sum()),
                    len(group),
                ),
            }
        )
    return pd.DataFrame(rows, columns=COMFORT_DISTRIBUTION_COLUMNS)


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


def _require_real_run(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise DashboardDataError(f"Run does not exist: {run_id}")
    run = dict(row)
    if bool(run["is_fake"]):
        raise DashboardDataError("Fake runs are excluded from production statistics.")
    return run


def _field_coverage(
    connection: sqlite3.Connection,
    *,
    table: str,
    run_id: str,
    fields: tuple[str, ...],
) -> dict[str, float]:
    allowed = {
        "telemetry": {
            "outdoor_temperature_c",
            "facility_demand_kw",
            "timestep_electricity_kwh",
            "cumulative_electricity_kwh",
            "hvac_electricity_kwh",
            "heating_setpoint_c",
            "cooling_setpoint_c",
        },
        "zone_telemetry": {
            "operative_temperature_c",
            "relative_humidity_percent",
            "occupant_count",
            "pmv",
            "ppd_percent",
            "co2_ppm",
        },
    }
    if table not in allowed or not set(fields).issubset(allowed[table]):
        raise ValueError("unsupported field-coverage query")
    expressions = ", ".join(
        f"SUM(CASE WHEN {field} IS NOT NULL THEN 1 ELSE 0 END) AS {field}" for field in fields
    )
    row = connection.execute(
        f"SELECT COUNT(*) AS total, {expressions} FROM {table} WHERE run_id = ?",  # noqa: S608
        (run_id,),
    ).fetchone()
    total = int(row["total"])
    return {field: float(row[field] or 0) / total * 100 if total else 0.0 for field in fields}


def _json_value(value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return None


def _active_fallback(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().casefold() not in {"", "none", "inactive", "false"}


def _verified_payload(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
) -> dict[str, Any]:
    row = metrics.get(metric_name)
    return _json_object(row.get("value_json") if row else None)


def _verified_or_count(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
    fallback: int,
) -> int:
    value = metrics.get(metric_name, {}).get("value")
    return int(value) if value is not None else fallback


def _verified_optional_count(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
) -> int | None:
    value = metrics.get(metric_name, {}).get("value")
    return int(value) if value is not None else None


def _verified_or_float(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
    fallback: float | None,
) -> float | None:
    value = metrics.get(metric_name, {}).get("value")
    return float(value) if value is not None else fallback


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _ratio_percent(numerator: int, denominator: int) -> float | None:
    return numerator / denominator * 100 if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    return float(pd.Series(values, dtype="float64").quantile(quantile))


def _action_numeric_summary(
    actions: list[dict[str, Any]],
    field: str,
) -> NumericSummary:
    values = [
        float(value)
        for action in actions
        if isinstance((value := action.get(field)), (int, float)) and not isinstance(value, bool)
    ]
    return NumericSummary(
        minimum=min(values) if values else None,
        mean=sum(values) / len(values) if values else None,
        maximum=max(values) if values else None,
        latest=values[-1] if values else None,
    )


def _metric_number(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
) -> float | None:
    value = metrics.get(metric_name, {}).get("value")
    return float(value) if value is not None else None


def _metric_units(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
) -> str | None:
    return _optional_text(metrics.get(metric_name, {}).get("units"))


def _metric_payload(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
) -> dict[str, Any]:
    row = metrics.get(metric_name)
    if not row:
        return {}
    structured = row.get("structured_value")
    return structured if isinstance(structured, dict) else {}


def _matching_metadata_value(
    baseline: dict[str, Any],
    controlled: dict[str, Any],
    *keys: str,
) -> bool | None:
    baseline_value = _first_text(baseline, *keys)
    controlled_value = _first_text(controlled, *keys)
    if baseline_value is None or controlled_value is None:
        return None
    return baseline_value == controlled_value


def _series_mean(series: pd.Series) -> float | None:
    return float(series.mean()) if not series.empty else None


def _series_min(series: pd.Series) -> float | None:
    return float(series.min()) if not series.empty else None


def _series_max(series: pd.Series) -> float | None:
    return float(series.max()) if not series.empty else None


def _series_quantile(series: pd.Series, quantile: float) -> float | None:
    return float(series.quantile(quantile)) if not series.empty else None
