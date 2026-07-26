"""Verified run finalization and baseline-versus-controlled comparison.

Official EnergyPlus output supplies final energy and demand totals. Runtime
telemetry is used only as a required cross-check and for occupied comfort. A
failed cross-check is persisted honestly as unverified and cannot be loaded for
comparison.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ecoloop import ENERGYPLUS_VERSION
from ecoloop.config import Settings, load_file_config
from ecoloop.db.store import SQLiteStore
from ecoloop.energyplus.results import EnergyPlusResults, parse_results
from ecoloop.exceptions import RunStateError
from ecoloop.metrics import (
    ComfortMetrics,
    ComfortSample,
    calculate_comfort_metrics,
    calculate_latency_metrics,
    compare_completed_runs,
    energy_cost,
    energy_cross_check,
    operational_carbon,
)
from ecoloop.schemas import (
    ComparisonMetrics,
    FinalRunMetrics,
    RunRecord,
    RunStatus,
)
from ecoloop.time_utils import parse_iso_datetime, utc_now

_FINAL_METRIC_NAME = "final_run_metrics"
_FINALIZATION_SOURCE = "official_energyplus_plus_sqlite_audit_v1"
_COMPARISON_SOURCE = "verified_completed_run_comparison_v1"


class EvaluationError(RunStateError):
    """Raised when final metrics or a comparison cannot be verified."""


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    """Final metrics plus their publication gate and exported files."""

    metrics: FinalRunMetrics
    verified_for_comparison: bool
    verification_reasons: tuple[str, ...]
    metrics_json_path: Path
    metrics_csv_path: Path


@dataclass(frozen=True, slots=True)
class ComparisonArtifacts:
    """Verified comparison and its machine-readable files."""

    comparison: ComparisonMetrics
    json_path: Path
    csv_path: Path


@dataclass(frozen=True, slots=True)
class _TelemetrySummary:
    simulation_start: datetime
    simulation_end: datetime
    electricity_kwh: float
    row_count: int


@dataclass(frozen=True, slots=True)
class _AuditSummary:
    decision_count: int
    tool_call_count: int
    average_latency_ms: float | None
    p95_latency_ms: float | None
    timeout_count: int
    fallback_count: int
    invalid_action_count: int
    safety_clamp_count: int


@dataclass(frozen=True, slots=True)
class _Provenance:
    preparation_fingerprint: str | None
    weather_sha256: str | None


def finalize_run(
    store: SQLiteStore,
    run_id: str,
    official_results: EnergyPlusResults,
    *,
    timestamp: datetime | None = None,
    tariff_per_kwh: float | None = None,
    carbon_kg_per_kwh: float | None = None,
    telemetry_tolerance_percent: float | None = None,
    occupied_operative_min_c: float | None = None,
    occupied_operative_max_c: float | None = None,
    absolute_pmv_max: float | None = None,
    zone_timestep_minutes: int | None = None,
    output_directory: Path | None = None,
    allow_fake: bool = False,
) -> FinalizationResult:
    """Build and persist final metrics from a completed run and official results.

    ``allow_fake`` exists only for explicit test fixtures. Fake results are
    always persisted with ``verified=False`` and remain ineligible for
    comparison.
    """

    run = _require_completed_run(store, run_id)
    if run.is_fake and not allow_fake:
        raise EvaluationError("production finalization refuses an explicitly fake run")
    _validate_official_results(run, official_results)
    timestep_minutes = (
        Settings().zone_timestep_minutes if zone_timestep_minutes is None else zone_timestep_minutes
    )
    if timestep_minutes <= 0 or 60 % timestep_minutes:
        raise ValueError("zone_timestep_minutes must be a positive divisor of 60")

    config = load_file_config()
    tariff = config.evaluation.tariff_per_kwh if tariff_per_kwh is None else tariff_per_kwh
    carbon_factor = (
        config.evaluation.carbon_kg_per_kwh if carbon_kg_per_kwh is None else carbon_kg_per_kwh
    )
    tolerance = (
        config.evaluation.telemetry_energy_tolerance_percent
        if telemetry_tolerance_percent is None
        else telemetry_tolerance_percent
    )
    occupied_limits = config.control.occupied
    operative_min = (
        occupied_limits.operative_min_c
        if occupied_operative_min_c is None
        else occupied_operative_min_c
    )
    operative_max = (
        occupied_limits.operative_max_c
        if occupied_operative_max_c is None
        else occupied_operative_max_c
    )
    pmv_limit = occupied_limits.absolute_pmv_max if absolute_pmv_max is None else absolute_pmv_max
    if operative_min is None or operative_max is None or pmv_limit is None:
        raise EvaluationError("occupied comfort limits are incomplete")

    with _readonly_connection(store.path) as connection:
        telemetry = _telemetry_summary(
            connection,
            run_id,
            zone_timestep_minutes=timestep_minutes,
        )
        comfort, air_temperature_fallback_count = _comfort_summary(
            connection,
            run_id,
            operative_min_c=float(operative_min),
            operative_max_c=float(operative_max),
            absolute_pmv_max=float(pmv_limit),
            zone_timestep_minutes=timestep_minutes,
        )
        audit = _audit_summary(connection, run_id)

    official_energy = _required_non_negative(
        official_results.facility_electricity_kwh,
        "official facility electricity",
    )
    official_peak = _required_non_negative(
        official_results.peak_electrical_demand_kw,
        "official peak electrical demand",
    )
    cross_check = energy_cross_check(
        official_energy,
        telemetry.electricity_kwh,
        tolerance,
    )
    now = timestamp or utc_now()
    artifact_paths = _official_artifact_paths(official_results)
    metrics = FinalRunMetrics(
        run_id=run.run_id,
        timestamp=now,
        run_type=run.run_type,
        status=run.status,
        is_fake=run.is_fake,
        simulation_start=telemetry.simulation_start,
        simulation_end=telemetry.simulation_end,
        facility_electricity_kwh=official_energy,
        hvac_electricity_kwh=official_results.hvac_electricity_kwh,
        other_fuels_kwh=official_results.other_fuels_kwh,
        peak_electrical_demand_kw=official_peak,
        cost=energy_cost(official_energy, tariff),
        operational_carbon_kg=operational_carbon(official_energy, carbon_factor),
        occupied_temperature_violation_percent=comfort.temperature_violation_percent,
        occupied_temperature_violation_degree_hours=(comfort.temperature_violation_degree_hours),
        pmv_compliance_percent=comfort.pmv_compliance_percent,
        mean_ppd_percent=comfort.mean_ppd_percent,
        maximum_occupied_co2_ppm=comfort.maximum_occupied_co2_ppm,
        llm_decision_count=audit.decision_count,
        tool_call_count=audit.tool_call_count,
        average_decision_latency_ms=audit.average_latency_ms,
        p95_decision_latency_ms=audit.p95_latency_ms,
        timeout_count=audit.timeout_count,
        fallback_count=audit.fallback_count,
        invalid_action_count=audit.invalid_action_count,
        safety_clamp_count=audit.safety_clamp_count,
        warning_count=official_results.warning_count,
        severe_count=official_results.severe_count,
        fatal_count=official_results.fatal_count,
        energy_cross_check=cross_check,
        source_artifacts=tuple(str(path) for path in artifact_paths),
    )

    reasons: list[str] = []
    if run.is_fake:
        reasons.append("run is explicitly fake")
    if not cross_check.passed:
        reasons.append(
            "telemetry energy differs from official EnergyPlus output by "
            f"{cross_check.difference_percent:.3f}% "
            f"(tolerance {cross_check.tolerance_percent:.3f}%)"
        )
    verified = not reasons
    provenance = _run_provenance(run)
    _persist_final_metrics(
        store,
        metrics,
        verified=verified,
        verification_reasons=tuple(reasons),
        provenance=provenance,
        air_temperature_fallback_count=air_temperature_fallback_count,
        timestamp=now,
    )
    _record_official_artifacts(store, run_id, artifact_paths, timestamp=now)
    destination = (output_directory or official_results.run_directory).resolve()
    json_path, csv_path = _write_final_metric_files(metrics, destination)
    store.record_artifact(
        run_id,
        "final_metrics_json",
        json_path,
        sha256=_sha256_file(json_path),
        size_bytes=json_path.stat().st_size,
        timestamp=now,
    )
    store.record_artifact(
        run_id,
        "final_metrics_csv",
        csv_path,
        sha256=_sha256_file(csv_path),
        size_bytes=csv_path.stat().st_size,
        timestamp=now,
    )
    return FinalizationResult(
        metrics=metrics,
        verified_for_comparison=verified,
        verification_reasons=tuple(reasons),
        metrics_json_path=json_path,
        metrics_csv_path=csv_path,
    )


def finalize_run_directory(
    store: SQLiteStore,
    run_id: str,
    run_directory: Path,
    **kwargs: Any,
) -> FinalizationResult:
    """Parse official output in *run_directory* and finalize the matching run."""

    official = parse_results(run_directory.expanduser().resolve())
    return finalize_run(store, run_id, official, **kwargs)


def load_verified_final_metrics(
    store: SQLiteStore,
    run_id: str,
) -> FinalRunMetrics:
    """Load the canonical final metric only when its publication gate passed."""

    run = _require_completed_run(store, run_id)
    if run.is_fake:
        raise EvaluationError("fake runs cannot be loaded as verified final metrics")
    rows = store.get_metrics(run_id)
    item = rows.get(_FINAL_METRIC_NAME)
    if item is None:
        raise EvaluationError(f"run has no canonical final metrics: {run_id}")
    if not bool(item["verified"]):
        raise EvaluationError(f"run final metrics did not pass verification: {run_id}")
    value = item["value"]
    if not isinstance(value, Mapping):
        raise EvaluationError(f"canonical final metrics are malformed: {run_id}")
    try:
        metrics = FinalRunMetrics.model_validate(dict(value))
    except ValueError as exc:
        raise EvaluationError(f"canonical final metrics are invalid: {run_id}") from exc
    if metrics.run_id != run_id:
        raise EvaluationError("canonical final metrics contain the wrong run_id")
    if not metrics.energy_cross_check.passed:
        raise EvaluationError("canonical final metrics failed the energy cross-check")
    return metrics


def compare_and_write(
    store: SQLiteStore,
    baseline_run_id: str,
    controlled_run_id: str,
    *,
    output_directory: Path | None = None,
    timestamp: datetime | None = None,
) -> ComparisonArtifacts:
    """Verify compatibility, calculate comparison metrics, and write JSON/CSV."""

    baseline_run, controlled_run = _require_compatible_runs(
        store,
        baseline_run_id,
        controlled_run_id,
    )
    baseline = load_verified_final_metrics(store, baseline_run_id)
    controlled = load_verified_final_metrics(store, controlled_run_id)
    now = timestamp or utc_now()
    try:
        comparison = compare_completed_runs(baseline, controlled, timestamp=now)
    except ValueError as exc:
        raise EvaluationError(f"run comparison is not valid: {exc}") from exc
    destination = (
        (output_directory or store.path.parent / controlled_run_id / "export")
        .expanduser()
        .resolve()
    )
    artifacts = write_comparison(comparison, destination)
    store.upsert_metric(
        controlled_run_id,
        "comparison",
        value_json=comparison.model_dump(mode="json"),
        source=_COMPARISON_SOURCE,
        verified=True,
        timestamp=now,
    )
    for artifact_type, path in (
        ("comparison_json", artifacts.json_path),
        ("comparison_csv", artifacts.csv_path),
    ):
        store.record_artifact(
            controlled_run_id,
            artifact_type,
            path,
            sha256=_sha256_file(path),
            size_bytes=path.stat().st_size,
            metadata={
                "baseline_run_id": baseline_run.run_id,
                "controlled_run_id": controlled_run.run_id,
            },
            timestamp=now,
        )
    return artifacts


def write_comparison(
    comparison: ComparisonMetrics,
    output_directory: Path,
) -> ComparisonArtifacts:
    """Write a verified comparison as canonical JSON and one-row CSV."""

    destination = output_directory.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "comparison.json"
    csv_path = destination / "comparison.csv"
    payload = comparison.model_dump(mode="json")
    _write_json(json_path, payload)
    _write_single_row_csv(csv_path, payload)
    return ComparisonArtifacts(
        comparison=comparison,
        json_path=json_path,
        csv_path=csv_path,
    )


def _require_completed_run(store: SQLiteStore, run_id: str) -> RunRecord:
    run = store.get_run(run_id)
    if run is None:
        raise EvaluationError(f"unknown run_id: {run_id}")
    if run.status is not RunStatus.COMPLETED:
        raise EvaluationError(f"run is not completed: {run_id} ({run.status.value})")
    return run


def _validate_official_results(
    run: RunRecord,
    results: EnergyPlusResults,
) -> None:
    if not results.completed:
        raise EvaluationError(
            "official EnergyPlus results are incomplete: "
            + (results.failure_reason or "unknown failure")
        )
    if results.fatal_count:
        raise EvaluationError("official EnergyPlus output contains fatal errors")
    if results.source_file is None or not results.source_file.is_file():
        raise EvaluationError("official EnergyPlus result source file is missing")
    run_directory = results.run_directory.expanduser().resolve()
    source_file = results.source_file.expanduser().resolve()
    if source_file.parent != run_directory:
        raise EvaluationError(
            "official EnergyPlus result source is outside the run output directory"
        )
    configured_output = run.metadata.get("output_directory")
    if isinstance(configured_output, str) and (
        Path(configured_output).expanduser().resolve() != run_directory
    ):
        raise EvaluationError("official EnergyPlus results do not match the run output directory")
    end_file = run_directory / "eplusout.end"
    if (
        not end_file.is_file()
        or "completed successfully"
        not in end_file.read_text(
            encoding="utf-8",
            errors="replace",
        ).casefold()
    ):
        raise EvaluationError("official EnergyPlus completion marker is missing")
    if run.energyplus_version != ENERGYPLUS_VERSION:
        raise EvaluationError(
            f"run EnergyPlus version is {run.energyplus_version!r}; required {ENERGYPLUS_VERSION}"
        )


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise EvaluationError(f"run database does not exist: {resolved}")
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()


def _telemetry_summary(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    zone_timestep_minutes: int,
) -> _TelemetrySummary:
    row = connection.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            COUNT(timestep_electricity_kwh) AS energy_count,
            SUM(timestep_electricity_kwh) AS electricity_kwh,
            MIN(simulation_timestamp) AS simulation_start,
            MAX(simulation_timestamp) AS simulation_end
        FROM telemetry
        WHERE run_id = ?
          AND LOWER(environment) NOT LIKE '%design day%'
          AND LOWER(environment) NOT LIKE '%sizing%'
          AND LOWER(environment) NOT LIKE '%warmup%'
        """,
        (run_id,),
    ).fetchone()
    if row is None or int(row["row_count"]) < 2:
        raise EvaluationError("finalization requires at least two facility telemetry rows")
    if int(row["energy_count"]) != int(row["row_count"]):
        raise EvaluationError("facility telemetry has missing timestep electricity values")
    first_interval_end = parse_iso_datetime(str(row["simulation_start"]))
    start = first_interval_end - timedelta(minutes=zone_timestep_minutes)
    end = parse_iso_datetime(str(row["simulation_end"]))
    if end <= start:
        raise EvaluationError("facility telemetry has no positive simulation duration")
    value = float(row["electricity_kwh"])
    if not math.isfinite(value) or value < 0:
        raise EvaluationError("telemetry electricity total is not finite and non-negative")
    return _TelemetrySummary(
        simulation_start=start,
        simulation_end=end,
        electricity_kwh=value,
        row_count=int(row["row_count"]),
    )


def _comfort_summary(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    operative_min_c: float,
    operative_max_c: float,
    absolute_pmv_max: float,
    zone_timestep_minutes: int,
) -> tuple[ComfortMetrics, int]:
    rows = connection.execute(
        """
        SELECT
            simulation_timestamp, zone_name, mean_air_temperature_c,
            operative_temperature_c, occupant_count, pmv, ppd_percent, co2_ppm
        FROM zone_telemetry
        WHERE run_id = ?
          AND LOWER(environment) NOT LIKE '%design day%'
          AND LOWER(environment) NOT LIKE '%sizing%'
          AND LOWER(environment) NOT LIKE '%warmup%'
        ORDER BY simulation_timestamp, zone_name
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        raise EvaluationError("run has no zone telemetry for comfort evaluation")
    facility_timestamps = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT simulation_timestamp
            FROM telemetry
            WHERE run_id = ?
              AND LOWER(environment) NOT LIKE '%design day%'
              AND LOWER(environment) NOT LIKE '%sizing%'
              AND LOWER(environment) NOT LIKE '%warmup%'
            """,
            (run_id,),
        ).fetchall()
    }
    zone_timestamps = {str(row["simulation_timestamp"]) for row in rows}
    if zone_timestamps != facility_timestamps:
        raise EvaluationError(
            "zone and facility telemetry do not cover identical evaluation timesteps"
        )
    expected_zones = {str(row["zone_name"]) for row in rows}
    zones_by_timestep: dict[str, set[str]] = {}
    for row in rows:
        zones_by_timestep.setdefault(str(row["simulation_timestamp"]), set()).add(
            str(row["zone_name"])
        )
    incomplete = [
        timestamp for timestamp, zones in zones_by_timestep.items() if zones != expected_zones
    ]
    if incomplete:
        raise EvaluationError(
            "zone telemetry is incomplete at one or more timesteps; "
            f"first missing batch: {incomplete[0]}"
        )
    duration_hours = zone_timestep_minutes / 60.0
    samples: list[ComfortSample] = []
    air_temperature_fallback_count = 0
    for row in rows:
        if row["occupant_count"] is None:
            raise EvaluationError("zone telemetry is missing the occupancy signal")
        occupied = float(row["occupant_count"]) > 0
        operative = row["operative_temperature_c"]
        if operative is None:
            operative = row["mean_air_temperature_c"]
            if occupied:
                air_temperature_fallback_count += 1
        samples.append(
            ComfortSample(
                occupied=occupied,
                operative_temperature_c=float(operative),
                duration_hours=duration_hours,
                pmv=float(row["pmv"]) if row["pmv"] is not None else None,
                ppd_percent=(float(row["ppd_percent"]) if row["ppd_percent"] is not None else None),
                co2_ppm=(float(row["co2_ppm"]) if row["co2_ppm"] is not None else None),
            )
        )
    try:
        comfort = calculate_comfort_metrics(
            samples,
            operative_min_c=operative_min_c,
            operative_max_c=operative_max_c,
            absolute_pmv_max=absolute_pmv_max,
        )
    except ValueError as exc:
        raise EvaluationError(f"occupied comfort metrics are unavailable: {exc}") from exc
    return comfort, air_temperature_fallback_count


def _audit_summary(
    connection: sqlite3.Connection,
    run_id: str,
) -> _AuditSummary:
    decision_rows = connection.execute(
        """
        SELECT latency_ms, fallback_status
        FROM agent_decisions
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchall()
    latencies = [float(row["latency_ms"]) for row in decision_rows]
    latency = calculate_latency_metrics(latencies)
    timeout_count = sum(
        1
        for row in decision_rows
        if row["fallback_status"] is not None
        and "timeout" in str(row["fallback_status"]).casefold()
    )
    explicit_timeout = connection.execute(
        """
        SELECT value
        FROM metrics
        WHERE run_id = ? AND metric_name = 'timeout_count' AND value IS NOT NULL
        """,
        (run_id,),
    ).fetchone()
    if explicit_timeout is not None:
        value = float(explicit_timeout["value"])
        if not value.is_integer() or value < 0:
            raise EvaluationError("persisted timeout_count is not a non-negative integer")
        timeout_count = int(value)

    tool_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    )
    fallback_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM applied_actions
            WHERE run_id = ? AND fallback_status IS NOT NULL AND fallback_status != ''
            """,
            (run_id,),
        ).fetchone()[0]
    )
    proposed_rows = connection.execute(
        """
        SELECT validation_result_json
        FROM proposed_actions
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchall()
    invalid_count = 0
    for row in proposed_rows:
        raw = row["validation_result_json"]
        if raw is None:
            continue
        parsed = _parse_json_object(str(raw), "proposed action validation")
        if parsed.get("accepted") is False:
            invalid_count += 1
    clamp_rows = connection.execute(
        "SELECT clamp_details_json FROM applied_actions WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    clamp_count = 0
    for row in clamp_rows:
        try:
            value = json.loads(str(row["clamp_details_json"]))
        except json.JSONDecodeError as exc:
            raise EvaluationError("applied action clamp audit is malformed") from exc
        if not isinstance(value, list):
            raise EvaluationError("applied action clamp audit is not a JSON list")
        clamp_count += len(value)
    return _AuditSummary(
        decision_count=len(decision_rows),
        tool_call_count=tool_count,
        average_latency_ms=latency.average_ms,
        p95_latency_ms=latency.p95_ms,
        timeout_count=timeout_count,
        fallback_count=fallback_count,
        invalid_action_count=invalid_count,
        safety_clamp_count=clamp_count,
    )


def _persist_final_metrics(
    store: SQLiteStore,
    metrics: FinalRunMetrics,
    *,
    verified: bool,
    verification_reasons: tuple[str, ...],
    provenance: _Provenance,
    air_temperature_fallback_count: int,
    timestamp: datetime,
) -> None:
    source = _FINALIZATION_SOURCE
    numeric: dict[str, tuple[float, str | None]] = {
        "facility_electricity_kwh": (metrics.facility_electricity_kwh, "kWh"),
        "peak_electrical_demand_kw": (metrics.peak_electrical_demand_kw, "kW"),
        "cost": (metrics.cost, "configured_currency"),
        "operational_carbon_kg": (metrics.operational_carbon_kg, "kgCO2e"),
        "occupied_temperature_violation_percent": (
            metrics.occupied_temperature_violation_percent,
            "%",
        ),
        "occupied_temperature_violation_degree_hours": (
            metrics.occupied_temperature_violation_degree_hours,
            "degree-hour",
        ),
        "llm_decision_count": (float(metrics.llm_decision_count), "count"),
        "tool_call_count": (float(metrics.tool_call_count), "count"),
        "timeout_count": (float(metrics.timeout_count), "count"),
        "fallback_count": (float(metrics.fallback_count), "count"),
        "invalid_action_count": (float(metrics.invalid_action_count), "count"),
        "safety_clamp_count": (float(metrics.safety_clamp_count), "count"),
        "warning_count": (float(metrics.warning_count), "count"),
        "severe_count": (float(metrics.severe_count), "count"),
        "fatal_count": (float(metrics.fatal_count), "count"),
        "comfort_air_temperature_fallback_count": (
            float(air_temperature_fallback_count),
            "zone-timesteps",
        ),
    }
    optional_numeric = {
        "hvac_electricity_kwh": (metrics.hvac_electricity_kwh, "kWh"),
        "pmv_compliance_percent": (metrics.pmv_compliance_percent, "%"),
        "mean_ppd_percent": (metrics.mean_ppd_percent, "%"),
        "maximum_occupied_co2_ppm": (metrics.maximum_occupied_co2_ppm, "ppm"),
        "average_decision_latency_ms": (
            metrics.average_decision_latency_ms,
            "ms",
        ),
        "p95_decision_latency_ms": (metrics.p95_decision_latency_ms, "ms"),
    }
    for name, (value, units) in (*numeric.items(), *optional_numeric.items()):
        if value is None:
            continue
        store.upsert_metric(
            metrics.run_id,
            name,
            value=float(value),
            units=units,
            source=source,
            verified=verified,
            timestamp=timestamp,
        )
    structured: dict[str, Mapping[str, Any] | Sequence[Any]] = {
        "other_fuels_kwh": metrics.other_fuels_kwh,
        "energy_cross_check": metrics.energy_cross_check.model_dump(mode="json"),
        "finalization_verification": {
            "verified_for_comparison": verified,
            "reasons": list(verification_reasons),
            "source": source,
            "timeout_count_semantics": (
                "Explicit persisted timeout_count when available; otherwise "
                "decision outcomes marked with a timeout fallback."
            ),
        },
        _FINAL_METRIC_NAME: metrics.model_dump(mode="json"),
    }
    if provenance.preparation_fingerprint is not None:
        structured["preparation_fingerprint"] = {"sha256": provenance.preparation_fingerprint}
    if provenance.weather_sha256 is not None:
        structured["weather_sha256"] = {"sha256": provenance.weather_sha256}
    for name, structured_value in structured.items():
        store.upsert_metric(
            metrics.run_id,
            name,
            value_json=structured_value,
            source=source,
            verified=verified,
            timestamp=timestamp,
        )


def _require_compatible_runs(
    store: SQLiteStore,
    baseline_run_id: str,
    controlled_run_id: str,
) -> tuple[RunRecord, RunRecord]:
    baseline = _require_completed_run(store, baseline_run_id)
    controlled = _require_completed_run(store, controlled_run_id)
    if baseline.is_fake or controlled.is_fake:
        raise EvaluationError("comparison refuses fake runs")
    if baseline.energyplus_version != ENERGYPLUS_VERSION:
        raise EvaluationError("baseline EnergyPlus version is missing or unsupported")
    if controlled.energyplus_version != baseline.energyplus_version:
        raise EvaluationError("EnergyPlus versions do not match")
    if not baseline.period_name or not controlled.period_name:
        raise EvaluationError("run period provenance is missing")
    if baseline.period_name != controlled.period_name:
        raise EvaluationError("run periods do not match")

    baseline_provenance = _finalized_provenance(store, baseline)
    controlled_provenance = _finalized_provenance(store, controlled)
    if baseline_provenance.weather_sha256 is None or controlled_provenance.weather_sha256 is None:
        raise EvaluationError("weather provenance is missing")
    if baseline_provenance.weather_sha256 != controlled_provenance.weather_sha256:
        raise EvaluationError("weather files do not match")
    if (
        baseline_provenance.preparation_fingerprint is None
        or controlled_provenance.preparation_fingerprint is None
    ):
        raise EvaluationError("model preparation fingerprint is missing")
    if baseline_provenance.preparation_fingerprint != controlled_provenance.preparation_fingerprint:
        raise EvaluationError("model preparation fingerprints do not match")
    return baseline, controlled


def _finalized_provenance(store: SQLiteStore, run: RunRecord) -> _Provenance:
    """Load finalization-time hashes and reject later provenance mutation."""

    current = _run_provenance(run)
    metrics = store.get_metrics(run.run_id)
    stored_preparation = _stored_digest(metrics, "preparation_fingerprint")
    stored_weather = _stored_digest(metrics, "weather_sha256")
    if stored_preparation is not None and current.preparation_fingerprint != stored_preparation:
        raise EvaluationError("model preparation fingerprints do not match finalized provenance")
    if stored_weather is not None and current.weather_sha256 != stored_weather:
        raise EvaluationError("weather files do not match finalized provenance")
    return _Provenance(
        preparation_fingerprint=stored_preparation or current.preparation_fingerprint,
        weather_sha256=stored_weather or current.weather_sha256,
    )


def _stored_digest(
    metrics: Mapping[str, Mapping[str, Any]],
    name: str,
) -> str | None:
    item = metrics.get(name)
    if item is None:
        return None
    value = item.get("value")
    if not isinstance(value, Mapping):
        return None
    digest = value.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return None
    try:
        bytes.fromhex(digest)
    except ValueError:
        return None
    return digest.casefold()


def _run_provenance(run: RunRecord) -> _Provenance:
    preparation = _metadata_digest(
        run.metadata,
        "preparation_fingerprint",
        "model_preparation_fingerprint",
    )
    if preparation is None and run.model_path:
        manifest = Path(run.model_path).resolve().parent / "preparation-manifest.json"
        if manifest.is_file():
            preparation = _sha256_file(manifest)
    weather = _metadata_digest(run.metadata, "weather_sha256", "weather_fingerprint")
    if weather is None and run.weather_path:
        weather_path = Path(run.weather_path).resolve()
        if weather_path.is_file():
            weather = _sha256_file(weather_path)
    return _Provenance(
        preparation_fingerprint=preparation,
        weather_sha256=weather,
    )


def _metadata_digest(metadata: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = metadata.get(name)
        if isinstance(value, str) and len(value) == 64:
            try:
                bytes.fromhex(value)
            except ValueError:
                continue
            return value.casefold()
    return None


def _official_artifact_paths(results: EnergyPlusResults) -> tuple[Path, ...]:
    candidates = [
        results.source_file,
        results.run_directory / "eplusout.end",
        results.run_directory / "eplusout.err",
    ]
    return tuple(
        sorted(
            {
                path.expanduser().resolve()
                for path in candidates
                if path is not None and path.is_file()
            },
            key=str,
        )
    )


def _record_official_artifacts(
    store: SQLiteStore,
    run_id: str,
    paths: Sequence[Path],
    *,
    timestamp: datetime,
) -> None:
    for path in paths:
        artifact_type = {
            "eplusout.end": "energyplus_end",
            "eplusout.err": "energyplus_error_log",
        }.get(path.name.casefold(), "official_energyplus_results")
        store.record_artifact(
            run_id,
            artifact_type,
            path,
            sha256=_sha256_file(path),
            size_bytes=path.stat().st_size,
            timestamp=timestamp,
        )


def _write_final_metric_files(
    metrics: FinalRunMetrics,
    destination: Path,
) -> tuple[Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "metrics.json"
    csv_path = destination / "metrics.csv"
    payload = metrics.model_dump(mode="json")
    _write_json(json_path, payload)
    _write_single_row_csv(csv_path, payload)
    return json_path, csv_path


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_single_row_csv(path: Path, payload: Mapping[str, Any]) -> None:
    row = {
        key: (
            json.dumps(value, ensure_ascii=True, sort_keys=True)
            if isinstance(value, (dict, list))
            else value
        )
        for key, value in payload.items()
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required_non_negative(value: float | None, label: str) -> float:
    if value is None or not math.isfinite(value) or value < 0:
        raise EvaluationError(f"{label} is unavailable or invalid")
    return value


def _parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"{label} is malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise EvaluationError(f"{label} is not a JSON object")
    return parsed
