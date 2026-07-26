"""Production MCP service backed by the SQLite WAL communication bus."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ecoloop.config import Settings, load_file_config, repository_root
from ecoloop.control.candidates import evaluate_candidates
from ecoloop.control.candidates import (
    generate_candidate_actions as generate_candidates,
)
from ecoloop.control.constraints import build_control_constraints
from ecoloop.control.fallback import (
    DeterministicFallbackController,
    reusable_last_safe_action,
)
from ecoloop.control.safety import SafetyContext, SafetyValidator
from ecoloop.db.store import SQLiteStore
from ecoloop.energyplus.discovery import discover_energyplus
from ecoloop.energyplus.epw_forecast import (
    EPWForecastError,
    forecast_context_from_epw,
)
from ecoloop.energyplus.handles import normalize_point_name, parse_available_api_data
from ecoloop.energyplus.logs import parse_error_file, severity_counts
from ecoloop.energyplus.model import write_action_schedule
from ecoloop.energyplus.replay import replay_action_sequence_from_store
from ecoloop.exceptions import RunStateError
from ecoloop.mcp.models import (
    OPERATIONAL_REASON_CODES,
    AuditEvent,
    CandidateActionInput,
    ControlActionInput,
)
from ecoloop.schemas import (
    BaselineReference,
    BuildingObservation,
    CandidateAction,
    ControlAction,
    ControlConstraints,
    ReasonCode,
    RunStatus,
    RunType,
    ToolCallTrace,
)


class SQLiteMCPService:
    """Implement all MCP operations without exposing storage or filesystem primitives."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        settings: Settings,
        validator: SafetyValidator | None = None,
        fallback: DeterministicFallbackController | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._validator = validator or SafetyValidator()
        self._fallback = fallback or DeterministicFallbackController()
        self._audit_lock = asyncio.Lock()
        self._audit_sequences: dict[str, int] = {}
        self._fallback_failures: dict[str, int] = {}

    async def get_current_building_state(self, run_id: str) -> dict[str, Any]:
        observation = await self._observation(run_id)
        return {
            "observation": _compact_observation(observation),
            "source": "sqlite_energyplus_telemetry",
        }

    async def get_recent_trends(self, run_id: str, window_steps: int) -> dict[str, Any]:
        observations = await asyncio.to_thread(
            self._store.get_recent_observations,
            run_id,
            limit=window_steps,
        )
        if not observations:
            raise ValueError(f"run has no observations: {run_id}")
        return {
            "run_id": run_id,
            "window_steps": window_steps,
            "sample_count": len(observations),
            "trends": _observation_trends(observations),
            "source": "sqlite_energyplus_telemetry",
        }

    async def get_constraints(self, run_id: str) -> dict[str, Any]:
        _, constraints = await self._observation_and_constraints(run_id)
        return constraints.model_dump(mode="json")

    async def get_weather_forecast(self, run_id: str, hours: int) -> dict[str, Any]:
        observation = await self._observation(run_id)
        run = await asyncio.to_thread(self._store.get_run, run_id)
        if run is None or run.weather_path is None:
            raise ValueError(f"run has no configured weather provenance: {run_id}")
        try:
            context = await asyncio.to_thread(
                forecast_context_from_epw,
                Path(run.weather_path),
                observation.simulation_timestamp,
                hours,
            )
        except EPWForecastError as exc:
            return {
                "run_id": run_id,
                "hours_requested": hours,
                "available": False,
                "simulation_timestamp": observation.simulation_timestamp.isoformat(),
                "features": None,
                "points": [],
                "source": "unavailable_configured_epw_context",
                "error": str(exc),
            }
        expected_weather_sha = run.metadata.get("weather_sha256")
        if (
            isinstance(expected_weather_sha, str)
            and context.file_metadata.sha256 != expected_weather_sha
        ):
            raise ValueError("configured EPW content no longer matches the run weather checksum")
        return {
            "run_id": run_id,
            "hours_requested": hours,
            "available": True,
            "simulation_timestamp": observation.simulation_timestamp.isoformat(),
            "features": {
                "temperature_mean_c": context.temperature_mean_c,
                "temperature_max_c": context.temperature_max_c,
                "solar_mean_w_m2": context.solar_mean_w_m2,
            },
            "points": [
                {
                    "offset_hours": point.offset_hours,
                    "simulation_timestamp": point.timestamp.isoformat(),
                    "drybulb_temperature_c": point.drybulb_temperature_c,
                    "global_horizontal_solar_w_m2": (point.global_horizontal_solar_w_m2),
                    "leap_day_fallback": point.leap_day_fallback,
                }
                for point in context.points
            ],
            "weather_sha256": context.file_metadata.sha256,
            "source": context.source,
        }

    async def get_grid_signal(self, run_id: str, hours: int) -> dict[str, Any]:
        observation = await self._observation(run_id)
        points = [
            {
                "simulation_timestamp": (
                    observation.simulation_timestamp + timedelta(hours=offset)
                ).isoformat(),
                "tariff_per_kwh": observation.tariff_per_kwh,
                "carbon_kg_per_kwh": observation.carbon_kg_per_kwh,
            }
            for offset in range(1, hours + 1)
        ]
        return {
            "run_id": run_id,
            "hours": hours,
            "points": points,
            "source": "configured_constant_grid_signal",
        }

    async def generate_candidate_actions(self, run_id: str) -> dict[str, Any]:
        observation, constraints = await self._observation_and_constraints(run_id)
        candidates = generate_candidates(
            observation,
            constraints,
            baseline_heating_setpoint_c=(
                observation.baseline_reference.heating_setpoint_c
                if observation.baseline_reference is not None
                else None
            ),
            baseline_cooling_setpoint_c=(
                observation.baseline_reference.cooling_setpoint_c
                if observation.baseline_reference is not None
                else None
            ),
            hold_minutes=min(60, constraints.maximum_hold_minutes),
        )
        scores = evaluate_candidates(observation, constraints, candidates)
        recommended = scores[0]
        return {
            "run_id": run_id,
            "observation_id": observation.observation_id,
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "scores": [item.model_dump(mode="json") for item in scores],
            "recommended_candidate": recommended.candidate.model_dump(mode="json"),
            "recommended_total_score": recommended.total_score,
            "application_context": {
                "observation_id": observation.observation_id,
                "action_generation": constraints.next_action_generation,
                "allowed_reason_codes": list(OPERATIONAL_REASON_CODES),
            },
            "scoring_method": "transparent_one_step_heuristic_not_mpc",
            "selection_guidance": (
                "Candidates are independently bounded and sorted by ascending total_score; "
                "prefer recommended_candidate unless a concrete safety condition requires "
                "request_safe_fallback."
            ),
        }

    async def evaluate_candidate_actions(
        self,
        run_id: str,
        candidates: list[CandidateActionInput],
    ) -> dict[str, Any]:
        if not candidates:
            raise ValueError("at least one candidate is required")
        observation, constraints = await self._observation_and_constraints(run_id)
        domain_candidates = [_domain_candidate(item) for item in candidates]
        scores = evaluate_candidates(observation, constraints, domain_candidates)
        recommended = scores[0]
        return {
            "run_id": run_id,
            "observation_id": observation.observation_id,
            "evaluated_candidates": [item.model_dump(mode="json") for item in scores],
            "recommended_candidate": recommended.candidate.model_dump(mode="json"),
            "recommended_total_score": recommended.total_score,
            "application_context": {
                "observation_id": observation.observation_id,
                "action_generation": constraints.next_action_generation,
                "allowed_reason_codes": list(OPERATIONAL_REASON_CODES),
            },
            "scoring_method": "transparent_one_step_heuristic_not_mpc",
            "selection_guidance": (
                "Candidates are independently bounded and sorted by ascending total_score; "
                "prefer recommended_candidate unless a concrete safety condition requires "
                "request_safe_fallback."
            ),
        }

    async def apply_control_action(
        self,
        run_id: str,
        observation_id: int,
        action: ControlActionInput,
    ) -> dict[str, Any]:
        await self._require_running_run(run_id)
        observation, constraints = await self._observation_and_constraints(run_id)
        now = datetime.now(UTC)
        proposal = _domain_control_action(
            run_id,
            observation_id,
            action,
            now=now,
            default_model=f"ollama:{self._settings.ollama_model}",
        )
        applied_history = await asyncio.to_thread(
            self._store.get_applied_actions,
            run_id,
            limit=10_000,
        )
        for prior_action, prior_validation in applied_history:
            if prior_action.action_id != proposal.action_id:
                continue
            if (
                prior_action.observation_id != proposal.observation_id
                or prior_action.action_generation != proposal.action_generation
                or prior_action.action != proposal.action
            ):
                raise ValueError("action identity was reused with different control values")
            return {
                "success": True,
                "status": "applied",
                "terminal": "applied",
                "run_id": run_id,
                "observation_id": observation_id,
                "action_id": proposal.action_id,
                "idempotent_duplicate": True,
                "proposed_action": prior_action.action.model_dump(mode="json"),
                "applied_action": (
                    prior_validation.applied_action.model_dump(mode="json")
                    if prior_validation.applied_action is not None
                    else None
                ),
                "validation": prior_validation.model_dump(mode="json"),
            }
        last_applied = applied_history[-1] if applied_history else None
        last_generation = last_applied[0].action_generation if last_applied else 0
        validation = self._validator.validate(
            proposal,
            SafetyContext(
                expected_run_id=run_id,
                latest_observation=observation,
                constraints=constraints,
                last_applied_generation=last_generation,
                now=now,
            ),
        )
        if not validation.accepted:
            await asyncio.to_thread(self._store.record_proposed_action, proposal, validation)
            return {
                "success": False,
                "status": "rejected",
                "terminal": None,
                "run_id": run_id,
                "observation_id": observation_id,
                "proposed_action": proposal.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
            }
        application = await asyncio.to_thread(
            self._store.apply_validated_action,
            proposal,
            validation,
            expected_run_id=run_id,
            timestamp=now,
        )
        success = application.applied or application.idempotent_duplicate
        if success:
            self._fallback_failures.pop(run_id, None)
        return {
            "success": success,
            "status": "applied" if success else "rejected",
            "terminal": "applied" if success else None,
            "run_id": run_id,
            "observation_id": observation_id,
            "action_id": proposal.action_id,
            "proposed_action": proposal.action.model_dump(mode="json"),
            "applied_action": (
                validation.applied_action.model_dump(mode="json")
                if validation.applied_action is not None
                else None
            ),
            "validation": validation.model_dump(mode="json"),
            "application": application.model_dump(mode="json"),
        }

    async def request_safe_fallback(
        self,
        run_id: str,
        observation_id: int,
    ) -> dict[str, Any]:
        await self._require_running_run(run_id)
        observation, constraints = await self._observation_and_constraints(run_id)
        if observation_id != observation.observation_id:
            raise ValueError(
                f"fallback observation_id {observation_id} is not current "
                f"({observation.observation_id})"
            )
        now = datetime.now(UTC)
        consecutive = self._fallback_failures.get(run_id, 0) + 1
        self._fallback_failures[run_id] = consecutive
        last_safe = await asyncio.to_thread(self._store.get_last_applied_action, run_id)
        proposal = reusable_last_safe_action(
            last_safe,
            observation=observation,
            constraints=constraints,
            timestamp=now,
            consecutive_failures=consecutive,
        )
        fallback_status = "last_known_safe"
        if proposal is None:
            proposal = self._fallback.create_action(
                observation,
                constraints,
                timestamp=now,
            )
            fallback_status = "deterministic_rule"
        last_generation = last_safe[0].action_generation if last_safe else 0
        validation = self._validator.validate(
            proposal,
            SafetyContext(
                expected_run_id=run_id,
                latest_observation=observation,
                constraints=constraints,
                last_applied_generation=last_generation,
                now=now,
            ),
        )
        if not validation.accepted:
            await asyncio.to_thread(self._store.record_proposed_action, proposal, validation)
            raise RuntimeError(
                "deterministic fallback was rejected: "
                + "; ".join(issue.message for issue in validation.issues)
            )
        application = await asyncio.to_thread(
            self._store.apply_validated_action,
            proposal,
            validation,
            expected_run_id=run_id,
            timestamp=now,
        )
        if not (application.applied or application.idempotent_duplicate):
            raise RuntimeError(f"deterministic fallback was not applied: {application.message}")
        return {
            "success": True,
            "status": "fallback",
            "terminal": "fallback",
            "run_id": run_id,
            "observation_id": observation_id,
            "action_id": proposal.action_id,
            "fallback_status": fallback_status,
            "applied_action": (
                validation.applied_action.model_dump(mode="json")
                if validation.applied_action is not None
                else None
            ),
            "validation": validation.model_dump(mode="json"),
        }

    async def get_last_energyplus_errors(self, run_id: str, limit: int) -> dict[str, Any]:
        errors = await asyncio.to_thread(self._store.get_recent_errors, run_id, limit=limit)
        return {
            "run_id": run_id,
            "errors": errors,
            "count": len(errors),
            "bounded": True,
        }

    async def inspect_idf(self, path: Path) -> dict[str, Any]:
        return await asyncio.to_thread(_inspect_model_file, path)

    async def validate_idf(self, path: Path, weather_path: Path) -> dict[str, Any]:
        installation = await asyncio.to_thread(discover_energyplus, self._settings)
        if installation is None or installation.executable is None:
            raise RuntimeError(
                "EnergyPlus 26.1.0 executable is unavailable; set ENERGYPLUS_HOME "
                "or install EnergyPlus 26.1.0"
            )
        if not installation.is_version_match:
            raise RuntimeError(
                f"EnergyPlus version mismatch: found {installation.version}; expected 26.1.0"
            )
        output = (
            self._settings.resolved_runs_dir() / "diagnostics" / f"validate-{uuid.uuid4().hex[:12]}"
        )
        await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
        log_path = output / "energyplus-stdout.log"
        with log_path.open("wb") as log_handle:
            process = await asyncio.create_subprocess_exec(
                str(installation.executable),
                "-w",
                str(weather_path),
                "-d",
                str(output),
                str(path),
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=300)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError(
                    f"EnergyPlus validation timed out after 300 seconds; outputs: {output}"
                ) from None
        error_path = output / "eplusout.err"
        messages = (
            await asyncio.to_thread(parse_error_file, error_path, maximum_messages=100)
            if error_path.is_file()
            else ()
        )
        counts = severity_counts(messages)
        return {
            "valid": exit_code == 0 and counts["fatal"] == 0 and counts["severe"] == 0,
            "exit_code": exit_code,
            "model_path": str(path),
            "weather_path": str(weather_path),
            "output_directory": str(output),
            "message_counts": counts,
            "severe_or_fatal": [
                {
                    "severity": item.severity.value,
                    "message": item.message,
                    "occurrences": item.occurrences,
                }
                for item in messages
                if item.severity.value in {"severe", "fatal"}
            ][:20],
        }

    async def inspect_available_energyplus_points(
        self,
        run_id: str,
        query: str,
    ) -> dict[str, Any]:
        catalogue = await asyncio.to_thread(self._find_api_catalogue, run_id)
        points = await asyncio.to_thread(_search_api_points, catalogue, query)
        return {
            "run_id": run_id,
            "query": query,
            "catalogue_path": str(catalogue),
            "matches": points,
            "count": len(points),
        }

    async def parse_energyplus_error_file(self, path: Path) -> dict[str, Any]:
        messages = await asyncio.to_thread(parse_error_file, path, maximum_messages=500)
        return {
            "path": str(path),
            "counts": severity_counts(messages),
            "messages": [
                {
                    "severity": item.severity.value,
                    "message": item.message,
                    "digest": item.digest,
                    "occurrences": item.occurrences,
                }
                for item in messages
            ],
            "bounded": True,
        }

    async def generate_replay_model(self, run_id: str) -> dict[str, Any]:
        run = await asyncio.to_thread(self._store.get_run, run_id)
        if run is None:
            raise ValueError(f"unknown run_id: {run_id}")
        if run.is_fake:
            raise ValueError("production replay generation refuses fake runs")
        sequence = await asyncio.to_thread(
            replay_action_sequence_from_store,
            run_id,
            self._store,
        )
        run_directory = self._settings.resolved_runs_dir() / run_id
        schedule_path = run_directory / "action_schedule.csv"
        await asyncio.to_thread(write_action_schedule, sequence.actions, schedule_path)
        prepared_replay = await asyncio.to_thread(
            lambda: (
                Path(run.model_path).resolve()
                if run.model_path is not None
                else (repository_root() / "models/generated/agent_replay.idf").resolve()
            )
        )
        if not await asyncio.to_thread(prepared_replay.is_file):
            raise RuntimeError(
                "the source run's immutable control model is unavailable; "
                "run prepare-model and execute the controlled case again"
            )
        source_manifest = prepared_replay.parent / "preparation-manifest.json"
        if not await asyncio.to_thread(source_manifest.is_file):
            raise RuntimeError(
                "the source run's immutable preparation manifest is unavailable; "
                "a replay cannot preserve PMV/PPD handle discovery"
            )
        replay_path = run_directory / "agent_replay.idf"
        replay_manifest = run_directory / "preparation-manifest.json"
        await asyncio.to_thread(replay_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, prepared_replay, replay_path)
        await asyncio.to_thread(shutil.copy2, source_manifest, replay_manifest)
        return {
            "generated": True,
            "run_id": run_id,
            "action_count": len(sequence.actions),
            "action_schedule": str(schedule_path),
            "replay_model": str(replay_path),
            "preparation_manifest": str(replay_manifest),
            "method": "runtime_schedule_replay_from_physical_acknowledgements",
            "timing_source": sequence.timing_source,
        }

    async def audit_tool_call(self, event: AuditEvent) -> None:
        if event.run_id is None:
            return
        async with self._audit_lock:
            await asyncio.to_thread(self._record_audit, event)

    async def _observation(self, run_id: str) -> BuildingObservation:
        observation = await asyncio.to_thread(self._store.get_current_observation, run_id)
        if observation is None:
            raise ValueError(f"run has no current observation: {run_id}")
        updates: dict[str, Any] = {}
        if observation.baseline_reference is None:
            reference = await asyncio.to_thread(
                self._aligned_baseline_reference,
                run_id,
                observation,
            )
            if reference is not None:
                updates["baseline_reference"] = reference
        recent = await asyncio.to_thread(
            self._store.get_recent_observations,
            run_id,
            limit=8,
        )
        updates["recent_trends"] = _observation_trends(recent)
        physical = await asyncio.to_thread(
            self._store.get_physical_actuator_applications,
            run_id,
            limit=10_000,
        )
        if physical:
            latest = physical[-1]
            updates["previous_action"] = CandidateAction(
                candidate_id=f"physical-{latest.action_generation}",
                heating_setpoint_c=latest.heating_setpoint_c,
                cooling_setpoint_c=latest.cooling_setpoint_c,
                hold_minutes=latest.hold_minutes,
            )
        if (
            observation.forecast_temperature_mean_c is None
            and observation.forecast_temperature_max_c is None
        ):
            run = await asyncio.to_thread(self._store.get_run, run_id)
            if run is not None and run.weather_path is not None:
                try:
                    forecast = await asyncio.to_thread(
                        forecast_context_from_epw,
                        Path(run.weather_path),
                        observation.simulation_timestamp,
                        6,
                    )
                except EPWForecastError:
                    pass
                else:
                    expected_sha = run.metadata.get("weather_sha256")
                    if not isinstance(expected_sha, str) or (
                        forecast.file_metadata.sha256 == expected_sha
                    ):
                        updates.update(
                            {
                                "forecast_temperature_mean_c": (forecast.temperature_mean_c),
                                "forecast_temperature_max_c": (forecast.temperature_max_c),
                                "forecast_solar_mean_w_m2": forecast.solar_mean_w_m2,
                            }
                        )
        payload = observation.model_dump(mode="python")
        payload.update(updates)
        return BuildingObservation.model_validate(payload)

    def _aligned_baseline_reference(
        self,
        run_id: str,
        observation: BuildingObservation,
    ) -> BaselineReference | None:
        """Return a verified parent-baseline point at the exact simulation time."""

        run = self._store.get_run(run_id)
        if run is None or run.parent_run_id is None:
            return None
        parent = self._store.get_run(run.parent_run_id)
        if (
            parent is None
            or parent.run_type is not RunType.BASELINE
            or parent.status is not RunStatus.COMPLETED
            or parent.is_fake != run.is_fake
            or parent.period_name != run.period_name
            or parent.weather_path != run.weather_path
        ):
            return None
        run_fingerprint = run.metadata.get(
            "preparation_fingerprint",
            run.metadata.get("preparation_manifest_sha256"),
        )
        parent_fingerprint = parent.metadata.get(
            "preparation_fingerprint",
            parent.metadata.get("preparation_manifest_sha256"),
        )
        if (
            not isinstance(run_fingerprint, str)
            or not isinstance(parent_fingerprint, str)
            or run_fingerprint != parent_fingerprint
            or run.metadata.get("weather_sha256") is None
            or run.metadata.get("weather_sha256") != parent.metadata.get("weather_sha256")
        ):
            return None
        if not run.is_fake:
            parent_metrics = self._store.get_metrics(parent.run_id, verified_only=True)
            finalization = parent_metrics.get("finalization_verification", {}).get("value")
            final_metrics = parent_metrics.get("final_run_metrics")
            cross_check = parent_metrics.get("energy_cross_check", {}).get("value")
            if (
                not isinstance(finalization, dict)
                or finalization.get("verified_for_comparison") is not True
                or final_metrics is None
                or not isinstance(cross_check, dict)
                or cross_check.get("passed") is not True
            ):
                return None
        candidates = self._store.get_recent_observations(parent.run_id, limit=10_000)
        matched = next(
            (
                item
                for item in candidates
                if item.simulation_timestamp == observation.simulation_timestamp
            ),
            None,
        )
        if matched is None:
            return None
        return BaselineReference(
            run_id=parent.run_id,
            simulation_timestamp=matched.simulation_timestamp,
            heating_setpoint_c=matched.heating_setpoint_c,
            cooling_setpoint_c=matched.cooling_setpoint_c,
            facility_demand_kw=matched.facility_demand_kw,
            cumulative_electricity_kwh=matched.cumulative_electricity_kwh,
        )

    async def _observation_and_constraints(
        self,
        run_id: str,
    ) -> tuple[BuildingObservation, ControlConstraints]:
        observation = await self._observation(run_id)
        last = await asyncio.to_thread(self._store.get_last_applied_action, run_id)
        last_applied_generation = last[0].action_generation if last else 0
        last_proposed_generation = await asyncio.to_thread(
            self._last_proposed_generation,
            run_id,
        )
        next_generation = max(last_applied_generation, last_proposed_generation) + 1
        constraints = build_control_constraints(
            run_id=run_id,
            timestamp=datetime.now(UTC),
            occupied=observation.occupied,
            capabilities=observation.actuator_capabilities,
            next_action_generation=next_generation,
            config=load_file_config().control,
        )
        return observation, constraints

    async def _require_running_run(self, run_id: str) -> None:
        run = await asyncio.to_thread(self._store.get_run, run_id)
        if run is None:
            raise RunStateError(f"unknown run_id: {run_id}")
        if run.status is not RunStatus.RUNNING:
            raise RunStateError(
                f"control tools require a running simulation; run {run_id} is {run.status.value}"
            )

    def _last_proposed_generation(self, run_id: str) -> int:
        connection = sqlite3.connect(self._store.path)
        try:
            row = connection.execute(
                """
                SELECT MAX(action_generation)
                FROM proposed_actions
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def _record_audit(self, event: AuditEvent) -> None:
        run_id = event.run_id
        if run_id is None or self._store.get_run(run_id) is None:
            return
        sequence = self._audit_sequences.get(run_id)
        if sequence is None:
            recent = self._store.get_recent_tool_calls(run_id, limit=1)
            sequence = recent[-1].sequence if recent else 0
        sequence += 1
        self._audit_sequences[run_id] = sequence
        observation_id = event.arguments.get("observation_id")
        if not isinstance(observation_id, int) and event.result is not None:
            result_observation_id = event.result.get("observation_id")
            nested_observation = event.result.get("observation")
            if isinstance(result_observation_id, int):
                observation_id = result_observation_id
            elif isinstance(nested_observation, dict) and isinstance(
                nested_observation.get("observation_id"),
                int,
            ):
                observation_id = nested_observation["observation_id"]
        trace = ToolCallTrace(
            call_id=f"mcp-{uuid.uuid4().hex}",
            run_id=run_id,
            timestamp=event.started_at,
            observation_id=observation_id if isinstance(observation_id, int) else None,
            sequence=sequence,
            tool_name=event.tool_name,
            arguments=event.arguments,
            result=event.result,
            success=event.success,
            error=event.error,
            duration_ms=event.latency_ms,
            control_affecting=event.control_affecting,
        )
        self._store.record_tool_call(trace)

    def _find_api_catalogue(self, run_id: str) -> Path:
        run_directory = (self._settings.resolved_runs_dir() / run_id).resolve()
        if not run_directory.is_relative_to(self._settings.resolved_runs_dir()):
            raise ValueError("run_id resolves outside the configured run directory")
        expected = (
            run_directory / "api_points.csv",
            run_directory / "energyplus" / "api_points.csv",
            run_directory / "output" / "api_points.csv",
        )
        for path in expected:
            if path.is_file():
                return path
        raise ValueError(
            f"api_points.csv is unavailable for run {run_id}; run handle discovery first"
        )


def build_sqlite_service(settings: Settings) -> SQLiteMCPService:
    """Construct the production service from typed settings."""

    return SQLiteMCPService(
        store=SQLiteStore(settings.resolved_database_path()),
        settings=settings,
    )


def _domain_candidate(candidate: CandidateActionInput) -> CandidateAction:
    payload = {
        "candidate_id": candidate.candidate_id,
        "heating_setpoint_c": candidate.heating_setpoint_c,
        "cooling_setpoint_c": candidate.cooling_setpoint_c,
        "hold_minutes": candidate.hold_minutes,
        "ventilation_multiplier": candidate.ventilation_multiplier,
        "lighting_fraction": candidate.lighting_fraction,
        "supply_air_temperature_c": candidate.supply_air_temperature_c,
        "shading_state": candidate.shading_state,
    }
    candidate_id = payload.pop("candidate_id", None) or _candidate_identifier(payload)
    return CandidateAction(candidate_id=candidate_id, **payload)


def _domain_control_action(
    run_id: str,
    observation_id: int,
    action: ControlActionInput,
    *,
    now: datetime,
    default_model: str,
) -> ControlAction:
    candidate = _domain_candidate(action)
    reason_name = action.reason_code
    try:
        reason = ReasonCode[reason_name]
    except KeyError as exc:  # pragma: no cover - guarded by the MCP input enum
        raise ValueError(f"unsupported reason_code: {reason_name}") from exc
    identity = json.dumps(
        {
            "run_id": run_id,
            "observation_id": observation_id,
            "generation": action.action_generation,
            "candidate": candidate.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    action_id = f"action-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
    return ControlAction(
        action_id=action_id,
        run_id=run_id,
        observation_id=observation_id,
        action_generation=action.action_generation,
        timestamp=now,
        expires_at=now + timedelta(minutes=action.hold_minutes),
        action=candidate,
        model=action.model or default_model,
        latency_ms=action.latency_ms or 0.0,
        reason_code=reason,
        explanation=action.explanation,
    )


def _candidate_identifier(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"mcp-{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"


def _observation_trends(observations: list[BuildingObservation]) -> dict[str, Any]:
    metrics = (
        "zone_temperature_mean_c",
        "operative_temperature_mean_c",
        "relative_humidity_mean_percent",
        "pmv_mean",
        "co2_mean_ppm",
        "outdoor_temperature_c",
        "facility_demand_kw",
    )
    trends: dict[str, Any] = {}
    for metric in metrics:
        samples = [
            (item.simulation_timestamp, getattr(item, metric))
            for item in observations
            if getattr(item, metric) is not None
        ]
        if not samples:
            continue
        values = [float(value) for _, value in samples]
        duration_hours = (
            (samples[-1][0] - samples[0][0]).total_seconds() / 3600.0 if len(samples) > 1 else 0.0
        )
        slope = (values[-1] - values[0]) / duration_hours if duration_hours > 0 else 0.0
        trends[metric] = {
            "current": values[-1],
            "mean": sum(values) / len(values),
            "minimum": min(values),
            "maximum": max(values),
            "slope_per_hour": slope,
            "sample_count": len(values),
        }
    return trends


def _compact_observation(observation: BuildingObservation) -> dict[str, Any]:
    payload = observation.model_dump(mode="json", exclude={"zones"})
    payload["zone_count"] = len(observation.zones)
    payload["zones"] = [
        {
            "zone_name": zone.zone_name,
            "mean_air_temperature_c": zone.mean_air_temperature_c,
            "operative_temperature_c": zone.operative_temperature_c,
            "relative_humidity_percent": zone.relative_humidity_percent,
            "occupant_count": zone.occupant_count,
            "pmv": zone.pmv,
            "ppd_percent": zone.ppd_percent,
            "co2_ppm": zone.co2_ppm,
            "heating_setpoint_c": zone.heating_setpoint_c,
            "cooling_setpoint_c": zone.cooling_setpoint_c,
        }
        for zone in observation.zones[:20]
    ]
    if len(observation.zones) > 20:
        payload["zones_truncated"] = len(observation.zones) - 20
    return payload


def _inspect_model_file(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if path.suffix.casefold() == ".epjson":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("epJSON root must be an object")
        epjson_counts = {
            str(object_type): len(instances)
            for object_type, instances in payload.items()
            if isinstance(instances, dict)
        }
        version = payload.get("Version")
        return {
            "path": str(path),
            "format": "epjson",
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "object_type_counts": epjson_counts,
            "version_object": version if isinstance(version, dict) else None,
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    idf_counts: dict[str, int] = {}
    idf_version: str | None = None
    for object_text in _idf_objects(text):
        fields = [item.strip() for item in object_text.split(",")]
        if not fields or not fields[0]:
            continue
        object_type = fields[0]
        idf_counts[object_type] = idf_counts.get(object_type, 0) + 1
        if object_type.casefold() == "version" and len(fields) > 1:
            idf_version = fields[1].strip()
    return {
        "path": str(path),
        "format": "idf",
        "sha256": digest,
        "size_bytes": path.stat().st_size,
        "object_type_counts": idf_counts,
        "version": idf_version,
    }


def _idf_objects(text: str) -> Iterable[str]:
    cleaned_lines = [line.split("!", 1)[0] for line in text.splitlines()]
    buffer = ""
    for line in cleaned_lines:
        buffer += " " + line
        while ";" in buffer:
            current, buffer = buffer.split(";", 1)
            if current.strip():
                yield current.strip()


def _search_api_points(path: Path, query: str) -> list[dict[str, Any]]:
    points = parse_available_api_data(path.read_text(encoding="utf-8", errors="replace"))
    normalized_query = normalize_point_name(query)
    ranked: list[tuple[float, Any]] = []
    for point in points:
        normalized_label = normalize_point_name(point.label)
        containment = 1.0 if normalized_query in normalized_label else 0.0
        similarity = SequenceMatcher(None, normalized_query, normalized_label).ratio()
        score = containment * 2.0 + similarity
        if containment or similarity >= 0.25:
            ranked.append((score, point))
    ranked.sort(key=lambda item: (-item[0], item[1].label.casefold()))
    return [
        {
            "category": point.category,
            "name": point.name,
            "key": point.key,
            "control_type": point.control_type,
            "units": point.units,
            "similarity": round(score, 4),
        }
        for score, point in ranked[:25]
    ]
