"""Generate deterministic Runtime-API replay artifacts from applied actions."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ecoloop.config import Settings, repository_root
from ecoloop.energyplus.model import ReplayAction, write_action_schedule
from ecoloop.exceptions import EnergyPlusIntegrationError
from ecoloop.schemas import BuildingObservation, ControlAction, RunRecord, ValidationResult


class ReplayStore(Protocol):
    """Store methods required for replay generation."""

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def get_observation(
        self,
        run_id: str,
        observation_id: int,
    ) -> BuildingObservation | None: ...

    def get_applied_actions(
        self,
        run_id: str,
        *,
        limit: int = 10_000,
    ) -> list[tuple[ControlAction, ValidationResult]]: ...

    def record_artifact(
        self,
        run_id: str,
        artifact_type: str,
        path: Path,
        **kwargs: object,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReplayArtifacts:
    """Run-specific schedule and actuator-capable replay model."""

    run_id: str
    action_schedule: Path
    replay_model: Path
    action_count: int


def replay_actions_from_store(
    run_id: str,
    store: ReplayStore,
) -> tuple[ReplayAction, ...]:
    """Align applied setpoints to the originating simulated observation clock."""

    pairs = store.get_applied_actions(run_id, limit=100_000)
    actions: list[ReplayAction] = []
    for action, validation in pairs:
        if not validation.accepted or validation.applied_action is None:
            continue
        observation = store.get_observation(run_id, action.observation_id)
        if observation is None:
            raise EnergyPlusIntegrationError(
                f"Applied action {action.action_id} references missing observation "
                f"{action.observation_id}."
            )
        applied = validation.applied_action
        actions.append(
            ReplayAction(
                simulation_timestamp=observation.simulation_timestamp.replace(
                    tzinfo=None
                ).isoformat(),
                observation_id=action.observation_id,
                action_generation=action.action_generation,
                heating_setpoint_c=float(applied.heating_setpoint_c),
                cooling_setpoint_c=float(applied.cooling_setpoint_c),
                hold_minutes=applied.hold_minutes,
            )
        )
    if not actions:
        raise EnergyPlusIntegrationError(f"Run {run_id} has no accepted applied actions to replay.")
    return tuple(actions)


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
    root = repository_root()
    destination = (
        output_directory.expanduser().resolve()
        if output_directory is not None
        else (resolved_settings.resolved_runs_dir() / run_id / "replay").resolve()
    )
    destination.mkdir(parents=True, exist_ok=True)
    source_model = root / "models" / "generated" / "agent_ready.idf"
    if not source_model.is_file():
        raise EnergyPlusIntegrationError(
            f"Agent-ready model is missing: {source_model}. Run prepare-model first."
        )
    replay_model = destination / "agent_replay.idf"
    shutil.copy2(source_model, replay_model)
    actions = replay_actions_from_store(run_id, store)
    schedule = write_action_schedule(actions, destination / "action_schedule.csv")
    store.record_artifact(run_id, "replay_model", replay_model)
    store.record_artifact(run_id, "action_schedule", schedule)
    return ReplayArtifacts(
        run_id=run_id,
        action_schedule=schedule,
        replay_model=replay_model,
        action_count=len(actions),
    )
