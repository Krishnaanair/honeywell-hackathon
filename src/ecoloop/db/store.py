"""SQLite WAL persistence for simulation telemetry and the control audit trail."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ecoloop.db.migrate import apply_migrations
from ecoloop.exceptions import RunStateError
from ecoloop.schemas import (
    ActionApplicationResult,
    BuildingObservation,
    BuildingTelemetry,
    CandidateScore,
    ControlAction,
    MessageSeverity,
    ObservationInput,
    RunRecord,
    RunStatus,
    RunType,
    ToolCallTrace,
    ValidationCode,
    ValidationResult,
    ZoneTelemetry,
)
from ecoloop.time_utils import isoformat_utc, parse_iso_datetime, utc_now

Clock = Callable[[], datetime]


class StoreError(RuntimeError):
    """Base class for durable-bus failures."""


class DataConflictError(StoreError):
    """Raised when an idempotency key is reused with different data."""


def _json_dumps(value: BaseModel | Mapping[str, Any] | Sequence[Any] | None) -> str:
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    else:
        payload = value
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    """Serialize domain models nested inside lists and mappings."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_load_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise StoreError("expected a JSON object in the database")
    return parsed


def _enum_value(value: RunType | RunStatus | MessageSeverity | ValidationCode | str) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


class SQLiteStore:
    """Small explicit repository around a versioned SQLite WAL database.

    A new connection is used per operation so simulation and agent processes can
    communicate without sharing Python state. Atomic action application uses
    ``BEGIN IMMEDIATE`` to serialize generation checks and inserts.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock = utc_now,
        busy_timeout_ms: int = 5_000,
        initialize: bool = True,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = path.expanduser().resolve()
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        if initialize:
            self.initialize()

    def initialize(self) -> int:
        """Create the parent directory, enable WAL, and apply all migrations."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                raise StoreError(f"failed to enable SQLite WAL mode for {self.path}")
            connection.execute("PRAGMA synchronous = NORMAL")
            return apply_migrations(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        try:
            yield connection
        finally:
            connection.close()

    def create_run(
        self,
        run_id: str,
        run_type: RunType,
        *,
        is_fake: bool = False,
        energyplus_version: str | None = None,
        model_path: Path | None = None,
        weather_path: Path | None = None,
        period_name: str | None = None,
        parent_run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> RunRecord:
        """Create a pending run; an existing run ID is never overwritten."""

        now = timestamp or self._clock()
        record = RunRecord(
            run_id=run_id,
            timestamp=now,
            updated_at=now,
            run_type=run_type,
            status=RunStatus.PENDING,
            is_fake=is_fake,
            energyplus_version=energyplus_version,
            model_path=str(model_path.resolve()) if model_path else None,
            weather_path=str(weather_path.resolve()) if weather_path else None,
            period_name=period_name,
            parent_run_id=parent_run_id,
            metadata=dict(metadata or {}),
        )
        values = record.model_dump(mode="json")
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, timestamp, updated_at, run_type, status, is_fake,
                        energyplus_version, model_path, weather_path, period_name,
                        parent_run_id, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.run_id,
                        isoformat_utc(record.timestamp),
                        isoformat_utc(record.updated_at),
                        record.run_type.value,
                        record.status.value,
                        int(record.is_fake),
                        record.energyplus_version,
                        record.model_path,
                        record.weather_path,
                        record.period_name,
                        record.parent_run_id,
                        _json_dumps(record.metadata),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DataConflictError(f"run_id already exists or is invalid: {run_id}") from exc
        return RunRecord.model_validate(values)

    def get_run(self, run_id: str) -> RunRecord | None:
        """Return one run record or ``None``."""

        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        run_type: RunType | None = None,
        include_fake: bool = True,
        limit: int = 100,
    ) -> list[RunRecord]:
        """List recent runs with explicit status/type/fake filtering."""

        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be in 1..10000")
        clauses: list[str] = []
        parameters: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status.value)
        if run_type is not None:
            clauses.append("run_type = ?")
            parameters.append(run_type.value)
        if not include_fake:
            clauses.append("is_fake = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs{where} ORDER BY timestamp DESC LIMIT ?",  # noqa: S608
                parameters,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error_summary: str | None = None,
        timestamp: datetime | None = None,
    ) -> RunRecord:
        """Transition a run through the permitted lifecycle."""

        now = timestamp or self._clock()
        allowed: dict[RunStatus, frozenset[RunStatus]] = {
            RunStatus.PENDING: frozenset(
                {RunStatus.RUNNING, RunStatus.BLOCKED, RunStatus.CANCELLED, RunStatus.FAILED}
            ),
            RunStatus.RUNNING: frozenset(
                {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
            ),
            RunStatus.COMPLETED: frozenset(),
            RunStatus.FAILED: frozenset(),
            RunStatus.BLOCKED: frozenset(),
            RunStatus.CANCELLED: frozenset(),
        }
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RunStateError(f"unknown run_id: {run_id}")
            current = RunStatus(str(row["status"]))
            if status == current:
                connection.commit()
                existing = self.get_run(run_id)
                if existing is None:  # pragma: no cover - guarded by transaction
                    raise RunStateError(f"run disappeared: {run_id}")
                return existing
            if status not in allowed[current]:
                connection.rollback()
                raise RunStateError(f"invalid run transition: {current.value} -> {status.value}")
            if status is RunStatus.COMPLETED:
                fatal_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM errors WHERE run_id = ? AND severity = 'fatal'",
                        (run_id,),
                    ).fetchone()[0]
                )
                if fatal_count:
                    connection.rollback()
                    raise RunStateError("cannot complete a run containing fatal errors")
            started_at = isoformat_utc(now) if status is RunStatus.RUNNING else None
            completed_at = (
                isoformat_utc(now)
                if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
                else None
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?,
                    updated_at = ?,
                    started_at = COALESCE(started_at, ?),
                    completed_at = COALESCE(?, completed_at),
                    error_summary = COALESCE(?, error_summary),
                    progress_percent = CASE
                        WHEN ? = 'completed' THEN 100.0
                        ELSE progress_percent
                    END
                WHERE run_id = ?
                """,
                (
                    status.value,
                    isoformat_utc(now),
                    started_at,
                    completed_at,
                    error_summary,
                    status.value,
                    run_id,
                ),
            )
            connection.commit()
        updated = self.get_run(run_id)
        if updated is None:  # pragma: no cover - guarded by transaction
            raise RunStateError(f"run disappeared: {run_id}")
        return updated

    def update_progress(
        self, run_id: str, progress_percent: float, *, timestamp: datetime | None = None
    ) -> None:
        """Persist EnergyPlus progress with bounded monotonic semantics."""

        if not 0 <= progress_percent <= 100:
            raise ValueError("progress_percent must be in 0..100")
        now = timestamp or self._clock()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET progress_percent = MAX(COALESCE(progress_percent, 0.0), ?), updated_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (progress_percent, isoformat_utc(now), run_id),
            )
        if cursor.rowcount != 1:
            raise RunStateError(f"run is unknown or not running: {run_id}")

    def record_telemetry(self, telemetry: BuildingTelemetry) -> bool:
        """Insert facility telemetry, returning ``False`` for an exact duplicate."""

        values = (
            telemetry.run_id,
            isoformat_utc(telemetry.timestamp),
            isoformat_utc(telemetry.simulation_timestamp),
            telemetry.timestep_key,
            telemetry.environment,
            telemetry.outdoor_temperature_c,
            telemetry.facility_demand_kw,
            telemetry.timestep_electricity_kwh,
            telemetry.cumulative_electricity_kwh,
            telemetry.hvac_electricity_kwh,
            telemetry.heating_setpoint_c,
            telemetry.cooling_setpoint_c,
        )
        with self._connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO telemetry (
                        run_id, timestamp, simulation_timestamp, timestep_key, environment,
                        outdoor_temperature_c, facility_demand_kw,
                        timestep_electricity_kwh, cumulative_electricity_kwh,
                        hvac_electricity_kwh, heating_setpoint_c, cooling_setpoint_c
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, timestep_key) DO NOTHING
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"cannot record telemetry for {telemetry.run_id}") from exc
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                """
                SELECT
                    run_id, timestamp, simulation_timestamp, timestep_key, environment,
                    outdoor_temperature_c, facility_demand_kw,
                    timestep_electricity_kwh, cumulative_electricity_kwh,
                    hvac_electricity_kwh, heating_setpoint_c, cooling_setpoint_c
                FROM telemetry
                WHERE run_id = ? AND timestep_key = ?
                """,
                (telemetry.run_id, telemetry.timestep_key),
            ).fetchone()
        if row is None or tuple(row) != values:
            raise DataConflictError(
                "facility timestep identity was reused with different telemetry"
            )
        return False

    def record_zone_telemetry(self, telemetry: ZoneTelemetry) -> bool:
        """Insert one zone row, suppressing duplicate callback iterations."""

        values = (
            telemetry.run_id,
            isoformat_utc(telemetry.timestamp),
            isoformat_utc(telemetry.simulation_timestamp),
            telemetry.timestep_key,
            telemetry.environment,
            telemetry.zone_name,
            telemetry.mean_air_temperature_c,
            telemetry.operative_temperature_c,
            telemetry.relative_humidity_percent,
            telemetry.occupant_count,
            telemetry.pmv,
            telemetry.ppd_percent,
            telemetry.co2_ppm,
            telemetry.heating_setpoint_c,
            telemetry.cooling_setpoint_c,
        )
        with self._connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO zone_telemetry (
                        run_id, timestamp, simulation_timestamp, timestep_key, environment,
                        zone_name, mean_air_temperature_c, operative_temperature_c,
                        relative_humidity_percent, occupant_count, pmv, ppd_percent,
                        co2_ppm, heating_setpoint_c, cooling_setpoint_c
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, timestep_key, zone_name) DO NOTHING
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(
                    f"cannot record zone telemetry for {telemetry.run_id}/{telemetry.zone_name}"
                ) from exc
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                """
                SELECT
                    run_id, timestamp, simulation_timestamp, timestep_key, environment,
                    zone_name, mean_air_temperature_c, operative_temperature_c,
                    relative_humidity_percent, occupant_count, pmv, ppd_percent,
                    co2_ppm, heating_setpoint_c, cooling_setpoint_c
                FROM zone_telemetry
                WHERE run_id = ? AND timestep_key = ? AND zone_name = ?
                """,
                (telemetry.run_id, telemetry.timestep_key, telemetry.zone_name),
            ).fetchone()
        if row is None or tuple(row) != values:
            raise DataConflictError("zone timestep identity was reused with different telemetry")
        return False

    def get_zone_telemetry(
        self,
        run_id: str,
        timestep_key: str,
    ) -> tuple[ZoneTelemetry, ...]:
        """Return the real zone rows aligned to one deterministic timestep."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    run_id, timestamp, simulation_timestamp, timestep_key, environment,
                    zone_name, mean_air_temperature_c, operative_temperature_c,
                    relative_humidity_percent, occupant_count, pmv, ppd_percent,
                    co2_ppm, heating_setpoint_c, cooling_setpoint_c
                FROM zone_telemetry
                WHERE run_id = ? AND timestep_key = ?
                ORDER BY zone_name COLLATE NOCASE
                """,
                (run_id, timestep_key),
            ).fetchall()
        return tuple(
            ZoneTelemetry(
                run_id=str(row["run_id"]),
                timestamp=parse_iso_datetime(str(row["timestamp"])),
                simulation_timestamp=parse_iso_datetime(str(row["simulation_timestamp"])),
                timestep_key=str(row["timestep_key"]),
                environment=str(row["environment"]),
                zone_name=str(row["zone_name"]),
                mean_air_temperature_c=float(row["mean_air_temperature_c"]),
                operative_temperature_c=_optional_float(row["operative_temperature_c"]),
                relative_humidity_percent=_optional_float(row["relative_humidity_percent"]),
                occupant_count=_optional_float(row["occupant_count"]),
                pmv=_optional_float(row["pmv"]),
                ppd_percent=_optional_float(row["ppd_percent"]),
                co2_ppm=_optional_float(row["co2_ppm"]),
                heating_setpoint_c=_optional_float(row["heating_setpoint_c"]),
                cooling_setpoint_c=_optional_float(row["cooling_setpoint_c"]),
            )
            for row in rows
        )

    def record_observation(self, observation: ObservationInput) -> BuildingObservation:
        """Insert or idempotently return an observation with a global monotonic ID."""

        payload = _json_dumps(observation)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO observations (
                        run_id, timestamp, simulation_timestamp, timestep_key,
                        environment, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, timestep_key) DO NOTHING
                    """,
                    (
                        observation.run_id,
                        isoformat_utc(observation.timestamp),
                        isoformat_utc(observation.simulation_timestamp),
                        observation.timestep_key,
                        observation.environment,
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StoreError(f"cannot record observation for run {observation.run_id}") from exc
            if cursor.rowcount == 1:
                if cursor.lastrowid is None:  # pragma: no cover - SQLite invariant
                    connection.rollback()
                    raise StoreError("SQLite did not return an observation ID")
                observation_id = int(cursor.lastrowid)
                connection.commit()
                return BuildingObservation(
                    observation_id=observation_id,
                    **observation.model_dump(),
                )
            row = connection.execute(
                """
                SELECT observation_id, payload_json
                FROM observations
                WHERE run_id = ? AND timestep_key = ?
                """,
                (observation.run_id, observation.timestep_key),
            ).fetchone()
            connection.commit()
        if row is None:  # pragma: no cover - protected by unique lookup
            raise StoreError("observation conflict could not be resolved")
        if str(row["payload_json"]) != payload:
            raise DataConflictError("timestep identity was reused with different observation data")
        return BuildingObservation(
            observation_id=int(row["observation_id"]),
            **_json_load_object(str(row["payload_json"])),
        )

    def get_current_observation(self, run_id: str) -> BuildingObservation | None:
        """Return the highest monotonic observation for a run."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT observation_id, payload_json
                FROM observations
                WHERE run_id = ?
                ORDER BY observation_id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return self._observation_from_row(row) if row is not None else None

    def get_observation(self, run_id: str, observation_id: int) -> BuildingObservation | None:
        """Return a specific observation only when it belongs to *run_id*."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT observation_id, payload_json
                FROM observations
                WHERE run_id = ? AND observation_id = ?
                """,
                (run_id, observation_id),
            ).fetchone()
        return self._observation_from_row(row) if row is not None else None

    def get_recent_observations(self, run_id: str, *, limit: int = 16) -> list[BuildingObservation]:
        """Return recent observations in chronological order."""

        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be in 1..10000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT observation_id, payload_json
                FROM observations
                WHERE run_id = ?
                ORDER BY observation_id DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._observation_from_row(row) for row in reversed(rows)]

    def record_proposed_action(
        self,
        action: ControlAction,
        validation: ValidationResult | None = None,
    ) -> bool:
        """Audit a proposal, returning ``False`` only for an identical retry."""

        self._validate_action_result_pair(action, validation)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (action.run_id,),
            ).fetchone()
            if run_row is None:
                connection.rollback()
                raise RunStateError(f"unknown run_id: {action.run_id}")
            if str(run_row["status"]) != RunStatus.RUNNING.value:
                connection.rollback()
                raise RunStateError(
                    "control proposals can only be recorded for a running simulation"
                )
            inserted = self._insert_proposed(connection, action, validation)
            connection.commit()
        return inserted

    def apply_validated_action(
        self,
        action: ControlAction,
        validation: ValidationResult,
        *,
        expected_run_id: str,
        timestamp: datetime | None = None,
    ) -> ActionApplicationResult:
        """Atomically apply only a fresh, accepted, monotonically generated action."""

        now = timestamp or self._clock()
        self._validate_action_result_pair(action, validation)
        if not validation.accepted or validation.applied_action is None:
            rejection_code = (
                validation.issues[0].code if validation.issues else ValidationCode.INVALID_DEADBAND
            )
            rejection_message = (
                validation.issues[0].message
                if validation.issues
                else "safety validation did not accept the action"
            )
            return self._application_rejection(
                action,
                now,
                rejection_code,
                rejection_message,
            )
        if action.run_id != expected_run_id:
            return self._application_rejection(
                action,
                now,
                ValidationCode.WRONG_RUN,
                "action run does not match the active simulation run",
            )
        expiry = validation.applied_expires_at or action.expires_at
        if expiry <= now:
            return self._application_rejection(
                action,
                now,
                ValidationCode.EXPIRED_ACTION,
                "action expired before it could be applied",
            )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT action_id FROM applied_actions WHERE action_id = ?",
                (action.action_id,),
            ).fetchone()
            if duplicate is not None:
                connection.commit()
                return ActionApplicationResult(
                    run_id=action.run_id,
                    observation_id=action.observation_id,
                    action_id=action.action_id,
                    timestamp=now,
                    applied=False,
                    idempotent_duplicate=True,
                    rejection_code=ValidationCode.DUPLICATE_ACTION,
                    message="action was already applied; actuator write must be skipped",
                )
            run_row = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (expected_run_id,),
            ).fetchone()
            if run_row is None:
                connection.rollback()
                return self._application_rejection(
                    action,
                    now,
                    ValidationCode.WRONG_RUN,
                    "active simulation run does not exist",
                )
            if str(run_row["status"]) != RunStatus.RUNNING.value:
                connection.rollback()
                return self._application_rejection(
                    action,
                    now,
                    ValidationCode.RUN_NOT_ACTIVE,
                    "validated actions can only be applied to a running simulation",
                )
            observation_row = connection.execute(
                """
                SELECT observation_id
                FROM observations
                WHERE run_id = ?
                ORDER BY observation_id DESC
                LIMIT 1
                """,
                (expected_run_id,),
            ).fetchone()
            if observation_row is None or int(observation_row["observation_id"]) != (
                action.observation_id
            ):
                connection.rollback()
                return self._application_rejection(
                    action,
                    now,
                    ValidationCode.STALE_OBSERVATION,
                    "action does not target the latest observation",
                )
            generation_row = connection.execute(
                """
                SELECT MAX(action_generation) AS generation
                FROM applied_actions
                WHERE run_id = ?
                """,
                (expected_run_id,),
            ).fetchone()
            prior_generation = (
                int(generation_row["generation"])
                if generation_row is not None and generation_row["generation"] is not None
                else 0
            )
            if action.action_generation <= prior_generation:
                connection.rollback()
                return self._application_rejection(
                    action,
                    now,
                    ValidationCode.NON_MONOTONIC_GENERATION,
                    "action generation is not newer than the last applied generation",
                )
            try:
                self._insert_proposed(connection, action, validation)
                connection.execute(
                    """
                    INSERT INTO applied_actions (
                        action_id, run_id, timestamp, observation_id, action_generation,
                        proposed_values_json, applied_values_json, validation_result_json,
                        clamp_details_json, expiry, model, latency_ms, reason_code,
                        explanation, fallback_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action.action_id,
                        action.run_id,
                        isoformat_utc(now),
                        action.observation_id,
                        action.action_generation,
                        _json_dumps(action.action),
                        _json_dumps(validation.applied_action),
                        _json_dumps(validation),
                        _json_dumps(validation.clamps),
                        isoformat_utc(expiry),
                        action.model,
                        action.latency_ms,
                        action.reason_code.value,
                        action.explanation,
                        validation.fallback_status,
                    ),
                )
                connection.execute(
                    """
                    UPDATE proposed_actions
                    SET applied_values_json = ?, validation_result_json = ?,
                        clamp_details_json = ?, fallback_status = ?
                    WHERE action_id = ?
                    """,
                    (
                        _json_dumps(validation.applied_action),
                        _json_dumps(validation),
                        _json_dumps(validation.clamps),
                        validation.fallback_status,
                        action.action_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DataConflictError(
                    f"action id or generation conflicts with existing audit data: "
                    f"{action.action_id}"
                ) from exc
            connection.commit()
        return ActionApplicationResult(
            run_id=action.run_id,
            observation_id=action.observation_id,
            action_id=action.action_id,
            timestamp=now,
            applied=True,
            message="validated action committed for actuator application",
        )

    def get_last_applied_action(self, run_id: str) -> tuple[ControlAction, ValidationResult] | None:
        """Return the latest proposed/applied pair for fallback and dashboard use."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    p.action_id, p.run_id, p.timestamp AS proposed_timestamp,
                    p.observation_id, p.action_generation, p.proposed_values_json,
                    p.expiry AS proposed_expiry, p.model, p.latency_ms, p.reason_code,
                    p.explanation, p.cache_hit, a.validation_result_json
                FROM applied_actions AS a
                JOIN proposed_actions AS p ON p.action_id = a.action_id
                WHERE a.run_id = ?
                ORDER BY a.action_generation DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return self._action_pair_from_row(row)

    def get_applied_actions(
        self, run_id: str, *, limit: int = 10_000
    ) -> list[tuple[ControlAction, ValidationResult]]:
        """Return validated actions in generation order for replay/export."""

        if not 1 <= limit <= 100_000:
            raise ValueError("limit must be in 1..100000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.action_id, p.run_id, p.timestamp AS proposed_timestamp,
                    p.observation_id, p.action_generation, p.proposed_values_json,
                    p.expiry AS proposed_expiry, p.model, p.latency_ms, p.reason_code,
                    p.explanation, p.cache_hit, a.validation_result_json
                FROM applied_actions AS a
                JOIN proposed_actions AS p ON p.action_id = a.action_id
                WHERE a.run_id = ?
                ORDER BY a.action_generation ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._action_pair_from_row(row) for row in rows]

    @staticmethod
    def _action_pair_from_row(
        row: sqlite3.Row,
    ) -> tuple[ControlAction, ValidationResult]:
        action = ControlAction.model_validate(
            {
                "action_id": row["action_id"],
                "run_id": row["run_id"],
                "timestamp": row["proposed_timestamp"],
                "observation_id": row["observation_id"],
                "action_generation": row["action_generation"],
                "action": json.loads(str(row["proposed_values_json"])),
                "expires_at": row["proposed_expiry"],
                "model": row["model"],
                "latency_ms": row["latency_ms"],
                "reason_code": row["reason_code"],
                "explanation": row["explanation"],
                "fallback": bool(
                    _json_load_object(str(row["validation_result_json"])).get("fallback_status")
                ),
                "cache_hit": bool(row["cache_hit"]),
            }
        )
        validation = ValidationResult.model_validate_json(str(row["validation_result_json"]))
        return action, validation

    def record_tool_call(self, trace: ToolCallTrace) -> bool:
        """Persist a complete, bounded MCP call trace."""

        values = (
            trace.call_id,
            trace.run_id,
            isoformat_utc(trace.timestamp),
            trace.observation_id,
            trace.sequence,
            trace.tool_name,
            _json_dumps(trace.arguments),
            _json_dumps(trace.result) if trace.result is not None else None,
            int(trace.success),
            trace.error,
            trace.duration_ms,
            int(trace.control_affecting),
        )
        with self._connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO tool_calls (
                        call_id, run_id, timestamp, observation_id, sequence, tool_name,
                        arguments_json, result_json, success, error, duration_ms,
                        control_affecting
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(call_id) DO NOTHING
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"cannot record tool call {trace.call_id}") from exc
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                """
                SELECT
                    call_id, run_id, timestamp, observation_id, sequence, tool_name,
                    arguments_json, result_json, success, error, duration_ms,
                    control_affecting
                FROM tool_calls
                WHERE call_id = ?
                """,
                (trace.call_id,),
            ).fetchone()
        if row is None or tuple(row) != values:
            raise DataConflictError(
                f"tool call ID was reused with different audit data: {trace.call_id}"
            )
        return False

    def get_recent_tool_calls(self, run_id: str, *, limit: int = 50) -> list[ToolCallTrace]:
        """Return recent tool calls in chronological order."""

        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be in 1..1000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tool_calls
                WHERE run_id = ?
                ORDER BY timestamp DESC, sequence DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        traces = [
            ToolCallTrace(
                call_id=str(row["call_id"]),
                run_id=str(row["run_id"]),
                timestamp=parse_iso_datetime(str(row["timestamp"])),
                observation_id=(
                    int(row["observation_id"]) if row["observation_id"] is not None else None
                ),
                sequence=int(row["sequence"]),
                tool_name=str(row["tool_name"]),
                arguments=_json_load_object(str(row["arguments_json"])),
                result=(
                    _json_load_object(str(row["result_json"]))
                    if row["result_json"] is not None
                    else None
                ),
                success=bool(row["success"]),
                error=str(row["error"]) if row["error"] is not None else None,
                duration_ms=float(row["duration_ms"]),
                control_affecting=bool(row["control_affecting"]),
            )
            for row in rows
        ]
        return list(reversed(traces))

    def record_agent_decision(
        self,
        *,
        decision_id: str,
        run_id: str,
        observation_id: int,
        model: str,
        latency_ms: float,
        completed: bool,
        candidate_scores: Sequence[CandidateScore] = (),
        state_summary: Mapping[str, Any] | None = None,
        action_generation: int | None = None,
        reason_code: str | None = None,
        explanation: str | None = None,
        fallback_status: str | None = None,
        timeout_count: int = 0,
        timestamp: datetime | None = None,
    ) -> None:
        """Audit one complete supervisory decision attempt."""

        if latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if timeout_count < 0:
            raise ValueError("timeout_count must be non-negative")
        now = timestamp or self._clock()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO agent_decisions (
                        decision_id, run_id, timestamp, observation_id,
                        action_generation, model, latency_ms, reason_code,
                        explanation, fallback_status, timeout_count,
                        candidate_scores_json, state_summary_json, completed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        run_id,
                        isoformat_utc(now),
                        observation_id,
                        action_generation,
                        model,
                        latency_ms,
                        reason_code,
                        explanation,
                        fallback_status,
                        timeout_count,
                        _json_dumps(candidate_scores),
                        _json_dumps(state_summary or {}),
                        int(completed),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DataConflictError(
                f"decision already exists or is invalid: {decision_id}"
            ) from exc

    def record_simulation_message(
        self,
        run_id: str,
        severity: MessageSeverity,
        message: str,
        *,
        timestamp: datetime | None = None,
    ) -> str:
        """Deduplicate a normalized EnergyPlus callback message by hash."""

        return self._record_deduplicated_message(
            table="simulation_messages",
            hash_column="message_hash",
            run_id=run_id,
            severity=severity,
            message=message,
            source=None,
            details=None,
            timestamp=timestamp,
        )

    def record_error(
        self,
        run_id: str,
        severity: MessageSeverity,
        source: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> str:
        """Deduplicate an error while incrementing occurrence count."""

        return self._record_deduplicated_message(
            table="errors",
            hash_column="error_hash",
            run_id=run_id,
            severity=severity,
            message=message,
            source=source,
            details=details,
            timestamp=timestamp,
        )

    def get_recent_errors(self, run_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return bounded severe/fatal diagnostics for MCP and dashboard use."""

        if not 1 <= limit <= 200:
            raise ValueError("limit must be in 1..200")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, severity, source, message, occurrence_count,
                       first_seen_at, last_seen_at, details_json
                FROM errors
                WHERE run_id = ?
                ORDER BY
                    CASE severity WHEN 'fatal' THEN 0 WHEN 'severe' THEN 1
                                  WHEN 'warning' THEN 2 ELSE 3 END,
                    last_seen_at DESC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [
            {
                "run_id": run_id,
                "timestamp": str(row["timestamp"]),
                "severity": str(row["severity"]),
                "source": str(row["source"]),
                "message": str(row["message"]),
                "occurrence_count": int(row["occurrence_count"]),
                "first_seen_at": str(row["first_seen_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                "details": _json_load_object(str(row["details_json"])),
            }
            for row in rows
        ]

    def upsert_metric(
        self,
        run_id: str,
        name: str,
        *,
        value: float | None = None,
        value_json: Mapping[str, Any] | Sequence[Any] | None = None,
        units: str | None = None,
        source: str,
        verified: bool,
        timestamp: datetime | None = None,
    ) -> None:
        """Store one named metric with explicit source and verification status."""

        if (value is None) == (value_json is None):
            raise ValueError("exactly one of value or value_json must be supplied")
        if value is not None and not math.isfinite(value):
            raise ValueError("metric value must be finite")
        now = timestamp or self._clock()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO metrics (
                    run_id, timestamp, metric_name, value, value_json,
                    units, source, verified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, metric_name) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    value = excluded.value,
                    value_json = excluded.value_json,
                    units = excluded.units,
                    source = excluded.source,
                    verified = excluded.verified
                """,
                (
                    run_id,
                    isoformat_utc(now),
                    name,
                    value,
                    _json_dumps(value_json) if value_json is not None else None,
                    units,
                    source,
                    int(verified),
                ),
            )

    def get_metrics(self, run_id: str, *, verified_only: bool = False) -> dict[str, dict[str, Any]]:
        """Return metrics keyed by name, optionally excluding unverified values."""

        predicate = " AND verified = 1" if verified_only else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM metrics WHERE run_id = ?{predicate}",  # noqa: S608
                (run_id,),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result[str(row["metric_name"])] = {
                "run_id": run_id,
                "timestamp": str(row["timestamp"]),
                "value": (
                    float(row["value"])
                    if row["value"] is not None
                    else json.loads(str(row["value_json"]))
                ),
                "units": str(row["units"]) if row["units"] is not None else None,
                "source": str(row["source"]),
                "verified": bool(row["verified"]),
            }
        return result

    def record_artifact(
        self,
        run_id: str,
        artifact_type: str,
        path: Path,
        *,
        sha256: str | None = None,
        size_bytes: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> bool:
        """Record a run artifact without reading arbitrary file content."""

        if size_bytes is not None and size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        now = timestamp or self._clock()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO run_artifacts (
                    run_id, timestamp, artifact_type, path, sha256,
                    size_bytes, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, artifact_type, path) DO NOTHING
                """,
                (
                    run_id,
                    isoformat_utc(now),
                    artifact_type,
                    str(path.resolve()),
                    sha256,
                    size_bytes,
                    _json_dumps(metadata or {}),
                ),
            )
        return cursor.rowcount == 1

    def _insert_proposed(
        self,
        connection: sqlite3.Connection,
        action: ControlAction,
        validation: ValidationResult | None,
    ) -> bool:
        applied = validation.applied_action if validation and validation.accepted else None
        existing = connection.execute(
            "SELECT * FROM proposed_actions WHERE action_id = ?",
            (action.action_id,),
        ).fetchone()
        expected_values = (
            action.run_id,
            isoformat_utc(action.timestamp),
            action.observation_id,
            action.action_generation,
            _json_dumps(action.action),
            isoformat_utc(action.expires_at),
            action.model,
            action.latency_ms,
            action.reason_code.value,
            action.explanation,
            int(action.cache_hit),
        )
        if existing is not None:
            existing_values = (
                str(existing["run_id"]),
                str(existing["timestamp"]),
                int(existing["observation_id"]),
                int(existing["action_generation"]),
                str(existing["proposed_values_json"]),
                str(existing["expiry"]),
                str(existing["model"]),
                float(existing["latency_ms"]),
                str(existing["reason_code"]),
                str(existing["explanation"]),
                int(existing["cache_hit"]),
            )
            if existing_values != expected_values:
                raise DataConflictError(
                    f"action_id was reused with different values: {action.action_id}"
                )
            if validation is not None:
                serialized_validation = _json_dumps(validation)
                prior_validation = existing["validation_result_json"]
                if prior_validation is not None and str(prior_validation) != serialized_validation:
                    raise DataConflictError(
                        f"action_id was reused with a different validation: {action.action_id}"
                    )
                if prior_validation is None:
                    connection.execute(
                        """
                        UPDATE proposed_actions
                        SET applied_values_json = ?, validation_result_json = ?,
                            clamp_details_json = ?, fallback_status = ?
                        WHERE action_id = ?
                        """,
                        (
                            _json_dumps(applied) if applied is not None else None,
                            serialized_validation,
                            _json_dumps(validation.clamps),
                            validation.fallback_status,
                            action.action_id,
                        ),
                    )
            return False
        try:
            connection.execute(
                """
                INSERT INTO proposed_actions (
                    action_id, run_id, timestamp, observation_id, action_generation,
                    proposed_values_json, applied_values_json, validation_result_json,
                    clamp_details_json, expiry, model, latency_ms, reason_code,
                    explanation, fallback_status, cache_hit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.action_id,
                    action.run_id,
                    isoformat_utc(action.timestamp),
                    action.observation_id,
                    action.action_generation,
                    _json_dumps(action.action),
                    _json_dumps(applied) if applied is not None else None,
                    _json_dumps(validation) if validation is not None else None,
                    _json_dumps(validation.clamps) if validation is not None else "[]",
                    isoformat_utc(action.expires_at),
                    action.model,
                    action.latency_ms,
                    action.reason_code.value,
                    action.explanation,
                    (
                        validation.fallback_status
                        if validation is not None
                        else ("proposed" if action.fallback else None)
                    ),
                    int(action.cache_hit),
                ),
            )
        except sqlite3.IntegrityError as exc:
            conflict = connection.execute(
                """
                SELECT action_id
                FROM proposed_actions
                WHERE run_id = ? AND observation_id = ? AND action_generation = ?
                """,
                (action.run_id, action.observation_id, action.action_generation),
            ).fetchone()
            if conflict is not None:
                raise DataConflictError(
                    "run/observation/generation was reused by another action: "
                    f"{conflict['action_id']}"
                ) from exc
            raise StoreError(f"cannot record proposed action {action.action_id}") from exc
        return True

    def _record_deduplicated_message(
        self,
        *,
        table: str,
        hash_column: str,
        run_id: str,
        severity: MessageSeverity,
        message: str,
        source: str | None,
        details: Mapping[str, Any] | None,
        timestamp: datetime | None,
    ) -> str:
        normalized = " ".join(message.split())
        if not normalized:
            raise ValueError("message must not be empty")
        now = timestamp or self._clock()
        digest = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()
        when = isoformat_utc(now)
        with self._connection() as connection:
            if table == "simulation_messages":
                connection.execute(
                    """
                    INSERT INTO simulation_messages (
                        run_id, timestamp, severity, message_hash, message,
                        occurrence_count, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(run_id, severity, message_hash) DO UPDATE SET
                        occurrence_count = occurrence_count + 1,
                        timestamp = excluded.timestamp,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        run_id,
                        when,
                        severity.value,
                        digest,
                        normalized,
                        when,
                        when,
                    ),
                )
            elif table == "errors":
                if source is None:
                    raise ValueError("error source is required")
                connection.execute(
                    """
                    INSERT INTO errors (
                        run_id, timestamp, severity, error_hash, source, message,
                        occurrence_count, first_seen_at, last_seen_at, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(run_id, severity, error_hash) DO UPDATE SET
                        occurrence_count = occurrence_count + 1,
                        timestamp = excluded.timestamp,
                        last_seen_at = excluded.last_seen_at,
                        details_json = excluded.details_json
                    """,
                    (
                        run_id,
                        when,
                        severity.value,
                        digest,
                        source,
                        normalized,
                        when,
                        when,
                        _json_dumps(details or {}),
                    ),
                )
            else:  # pragma: no cover - private invariant
                raise ValueError(f"unsupported message table: {table}/{hash_column}")
        return digest

    @staticmethod
    def _validate_action_result_pair(
        action: ControlAction,
        validation: ValidationResult | None,
    ) -> None:
        if validation is None:
            return
        if (
            action.run_id != validation.run_id
            or action.observation_id != validation.observation_id
            or action.action_generation != validation.action_generation
            or action.action != validation.proposed_action
        ):
            raise ValueError("validation result does not match the proposed action")

    @staticmethod
    def _application_rejection(
        action: ControlAction,
        timestamp: datetime,
        code: ValidationCode,
        message: str,
    ) -> ActionApplicationResult:
        return ActionApplicationResult(
            run_id=action.run_id,
            observation_id=action.observation_id,
            action_id=action.action_id,
            timestamp=timestamp,
            applied=False,
            rejection_code=code,
            message=message,
        )

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> BuildingObservation:
        return BuildingObservation(
            observation_id=int(row["observation_id"]),
            **_json_load_object(str(row["payload_json"])),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=str(row["run_id"]),
            timestamp=parse_iso_datetime(str(row["timestamp"])),
            updated_at=parse_iso_datetime(str(row["updated_at"])),
            run_type=RunType(str(row["run_type"])),
            status=RunStatus(str(row["status"])),
            is_fake=bool(row["is_fake"]),
            energyplus_version=(
                str(row["energyplus_version"]) if row["energyplus_version"] is not None else None
            ),
            model_path=str(row["model_path"]) if row["model_path"] is not None else None,
            weather_path=(str(row["weather_path"]) if row["weather_path"] is not None else None),
            period_name=(str(row["period_name"]) if row["period_name"] is not None else None),
            parent_run_id=(str(row["parent_run_id"]) if row["parent_run_id"] is not None else None),
            started_at=(
                parse_iso_datetime(str(row["started_at"]))
                if row["started_at"] is not None
                else None
            ),
            completed_at=(
                parse_iso_datetime(str(row["completed_at"]))
                if row["completed_at"] is not None
                else None
            ),
            progress_percent=(
                float(row["progress_percent"]) if row["progress_percent"] is not None else None
            ),
            error_summary=(str(row["error_summary"]) if row["error_summary"] is not None else None),
            metadata=_json_load_object(str(row["metadata_json"])),
        )
