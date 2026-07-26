"""Generate deterministic Runtime-API replay artifacts from applied actions."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

from ecoloop.config import Settings
from ecoloop.energyplus.model import ReplayAction, write_action_schedule
from ecoloop.exceptions import EnergyPlusIntegrationError
from ecoloop.schemas import (
    ControlAction,
    PhysicalActuatorApplication,
    RunRecord,
    RunType,
    ValidationResult,
)


class ReplayStore(Protocol):
    """Store methods required for replay generation."""

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def get_applied_actions(
        self,
        run_id: str,
        *,
        limit: int = 10_000,
    ) -> list[tuple[ControlAction, ValidationResult]]: ...

    def get_physical_actuator_applications(
        self,
        run_id: str,
        *,
        limit: int = 10_000,
    ) -> list[PhysicalActuatorApplication]: ...

    def get_verified_legacy_physical_actuator_applications(
        self,
        run_id: str,
        *,
        limit: int = 10_000,
    ) -> list[PhysicalActuatorApplication]: ...

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
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReplayArtifacts:
    """Run-specific schedule and actuator-capable replay model."""

    run_id: str
    action_schedule: Path
    replay_model: Path
    action_count: int
    timing_source: str


@dataclass(frozen=True, slots=True)
class ReplayActionSequence:
    """Replay actions with explicit physical-timing provenance."""

    actions: tuple[ReplayAction, ...]
    timing_source: str


def replay_actions_from_store(
    run_id: str,
    store: ReplayStore,
) -> tuple[ReplayAction, ...]:
    """Return only actions with an exact post-write callback timestamp."""

    return replay_action_sequence_from_store(run_id, store).actions


def replay_action_sequence_from_store(
    run_id: str,
    store: ReplayStore,
) -> ReplayActionSequence:
    """Build a schedule from structured or strictly verified legacy evidence."""

    pairs = store.get_applied_actions(run_id, limit=100_000)
    physical = store.get_physical_actuator_applications(run_id, limit=100_000)
    timing_source = "structured_runtime_callback"
    if not physical and pairs:
        physical = store.get_verified_legacy_physical_actuator_applications(
            run_id,
            limit=100_000,
        )
        timing_source = "legacy_verified_runtime_message"
    if not physical:
        raise EnergyPlusIntegrationError(
            f"Run {run_id} has no exact physical actuator callback acknowledgements; "
            "observation timestamps are not a safe replay substitute."
        )

    generations = [item.action_generation for item in physical]
    if generations != sorted(set(generations)):
        raise EnergyPlusIntegrationError(
            f"Run {run_id} has duplicate or non-monotonic physical action generations."
        )
    simulation_times = [item.simulation_timestamp for item in physical]
    if any(later <= earlier for earlier, later in pairwise(simulation_times)):
        raise EnergyPlusIntegrationError(
            f"Run {run_id} has non-increasing physical callback timestamps."
        )

    if pairs:
        accepted = [
            (action, validation)
            for action, validation in pairs
            if validation.accepted and validation.applied_action is not None
        ]
        if len(accepted) != len(physical):
            raise EnergyPlusIntegrationError(
                f"Run {run_id} has {len(accepted)} queued actions but "
                f"{len(physical)} physical callback acknowledgements."
            )
        by_generation = {item.action_generation: item for item in physical}
        for action, validation in accepted:
            application = by_generation.get(action.action_generation)
            applied = validation.applied_action
            if application is None or applied is None:
                raise EnergyPlusIntegrationError(
                    f"Run {run_id} is missing physical generation {action.action_generation}."
                )
            if (
                application.observation_id != action.observation_id
                or (application.action_id is not None and application.action_id != action.action_id)
                or abs(float(application.heating_setpoint_c) - float(applied.heating_setpoint_c))
                > 0.00051
                or abs(float(application.cooling_setpoint_c) - float(applied.cooling_setpoint_c))
                > 0.00051
                or application.hold_minutes != applied.hold_minutes
            ):
                raise EnergyPlusIntegrationError(
                    f"Run {run_id} physical generation {action.action_generation} "
                    "does not match its queued validated action."
                )
    else:
        run = store.get_run(run_id)
        if run is None or run.run_type is not RunType.FIXED_OVERRIDE:
            raise EnergyPlusIntegrationError(
                f"Run {run_id} has physical writes without a supported action source."
            )

    actions = []
    for application in physical:
        actions.append(
            ReplayAction(
                simulation_timestamp=application.simulation_timestamp.replace(
                    tzinfo=None
                ).isoformat(),
                observation_id=application.observation_id,
                action_generation=application.action_generation,
                heating_setpoint_c=float(application.heating_setpoint_c),
                cooling_setpoint_c=float(application.cooling_setpoint_c),
                hold_minutes=application.hold_minutes,
            )
        )
    return ReplayActionSequence(actions=tuple(actions), timing_source=timing_source)


def generate_replay(
    run_id: str,
    store: ReplayStore,
    settings: Settings | None = None,
    *,
    output_directory: Path | None = None,
) -> ReplayArtifacts:
    """Create a replay bundle that never invokes the model host."""

    run = store.get_run(run_id)
    if run is None:
        raise EnergyPlusIntegrationError(f"Unknown run_id: {run_id}")
    resolved_settings = settings or Settings()
    destination = (
        output_directory.expanduser().resolve()
        if output_directory is not None
        else (resolved_settings.resolved_runs_dir() / run_id / "replay").resolve()
    )
    destination.mkdir(parents=True, exist_ok=True)
    if run.model_path is None:
        raise EnergyPlusIntegrationError(
            f"Run {run_id} has no immutable model snapshot and cannot be replayed."
        )
    source_model = Path(run.model_path).expanduser().resolve()
    if not source_model.is_file():
        raise EnergyPlusIntegrationError(
            f"Run {run_id} immutable model snapshot is missing: {source_model}."
        )
    replay_model = destination / "agent_replay.idf"
    shutil.copy2(source_model, replay_model)
    sequence = replay_action_sequence_from_store(run_id, store)
    schedule = write_action_schedule(sequence.actions, destination / "action_schedule.csv")
    store.record_artifact(run_id, "replay_model", replay_model)
    store.record_artifact(run_id, "action_schedule", schedule)
    return ReplayArtifacts(
        run_id=run_id,
        action_schedule=schedule,
        replay_model=replay_model,
        action_count=len(sequence.actions),
        timing_source=sequence.timing_source,
    )
