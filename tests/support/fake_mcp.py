"""Explicitly fake MCP service and plant for hermetic integration tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ecoloop.mcp.models import AuditEvent, CandidateActionInput, ControlActionInput


class FakeMCPService:
    """Small deterministic fake plant; never used by a production command."""

    def __init__(self, *, audit_path: Path | None = None) -> None:
        self.state: dict[str, Any] = {
            "run_id": "fake-run",
            "observation_id": 1,
            "simulation_timestamp": "2026-07-26T12:00:00+00:00",
            "occupied": True,
            "occupancy_count": 12.0,
            "zone_temperature_mean_c": 28.0,
            "operative_temperature_mean_c": 28.0,
            "outdoor_temperature_c": 34.0,
            "pmv_mean": 0.9,
            "facility_demand_kw": 48.0,
            "heating_setpoint_c": 20.0,
            "cooling_setpoint_c": 26.0,
            "action_generation": 0,
            "actuator_capabilities": {
                "heating_setpoint": True,
                "cooling_setpoint": True,
            },
        }
        self.audits: list[AuditEvent] = []
        self.applied: list[dict[str, Any]] = []
        self._audit_path = audit_path.resolve() if audit_path is not None else None

    async def get_current_building_state(self, run_id: str) -> dict[str, Any]:
        self._run(run_id)
        return {"observation": dict(self.state), "source": "explicit_test_fake"}

    async def get_recent_trends(self, run_id: str, window_steps: int) -> dict[str, Any]:
        self._run(run_id)
        return {
            "window_steps": window_steps,
            "zone_temperature": {
                "current": self.state["zone_temperature_mean_c"],
                "slope_per_hour": -0.2,
            },
            "source": "explicit_test_fake",
        }

    async def get_constraints(self, run_id: str) -> dict[str, Any]:
        self._run(run_id)
        return {
            "run_id": run_id,
            "occupied": True,
            "heating_min_c": 19.0,
            "heating_max_c": 22.0,
            "cooling_min_c": 23.0,
            "cooling_max_c": 26.0,
            "minimum_deadband_c": 2.0,
            "maximum_change_c": 1.0,
            "maximum_hold_minutes": 120,
            "next_action_generation": int(self.state["action_generation"]) + 1,
            "capabilities": {
                "heating_setpoint": True,
                "cooling_setpoint": True,
            },
            "source": "explicit_test_fake",
        }

    async def get_weather_forecast(self, run_id: str, hours: int) -> dict[str, Any]:
        self._run(run_id)
        return {
            "hours": hours,
            "points": [{"offset_hours": 1, "drybulb_temperature_c": 34.0}],
            "source": "explicit_test_fake",
        }

    async def get_grid_signal(self, run_id: str, hours: int) -> dict[str, Any]:
        self._run(run_id)
        return {
            "hours": hours,
            "points": [{"offset_hours": 1, "tariff_per_kwh": 0.12, "carbon_kg_per_kwh": 0.7}],
            "source": "explicit_test_fake",
        }

    async def generate_candidate_actions(self, run_id: str) -> dict[str, Any]:
        self._run(run_id)
        candidates = [
            {
                "heating_setpoint_c": 20.0,
                "cooling_setpoint_c": 25.0,
                "hold_minutes": 60,
            },
            {
                "heating_setpoint_c": 20.0,
                "cooling_setpoint_c": 26.0,
                "hold_minutes": 60,
            },
        ]
        return {
            "candidates": candidates,
            "scores": [
                {
                    "candidate": candidates[0],
                    "total_score": 1.0,
                    "components": {"comfort": 1.0},
                },
                {
                    "candidate": candidates[1],
                    "total_score": 2.0,
                    "components": {"comfort": 2.0},
                },
            ],
            "recommended_candidate": candidates[0],
            "recommended_total_score": 1.0,
            "application_context": {
                "observation_id": int(self.state["observation_id"]),
                "action_generation": int(self.state["action_generation"]) + 1,
                "allowed_reason_codes": [
                    "ENERGY_OPTIMIZATION",
                    "COMFORT_PROTECTION",
                ],
            },
            "source": "explicit_test_fake",
        }

    async def evaluate_candidate_actions(
        self,
        run_id: str,
        candidates: list[CandidateActionInput],
    ) -> dict[str, Any]:
        self._run(run_id)
        return {
            "evaluated_candidates": [
                {
                    "candidate": candidate.model_dump(mode="json"),
                    "total_score": float(index + 1),
                    "components": {"comfort": float(index + 1)},
                }
                for index, candidate in enumerate(candidates)
            ],
            "source": "explicit_test_fake",
        }

    async def apply_control_action(
        self,
        run_id: str,
        observation_id: int,
        action: ControlActionInput,
    ) -> dict[str, Any]:
        self._run(run_id)
        if observation_id != self.state["observation_id"]:
            raise ValueError("stale observation")
        expected_generation = int(self.state["action_generation"]) + 1
        if action.action_generation != expected_generation:
            raise ValueError("non-monotonic action generation")
        applied = action.model_dump(mode="json")
        self.applied.append(applied)
        self.state["heating_setpoint_c"] = action.heating_setpoint_c
        self.state["cooling_setpoint_c"] = action.cooling_setpoint_c
        self.state["action_generation"] = action.action_generation
        # Deterministic fake first-order response, prominently test-only.
        old_temperature = float(self.state["zone_temperature_mean_c"])
        cooling_effect = max(0.0, old_temperature - action.cooling_setpoint_c) * 0.35
        self.state["zone_temperature_mean_c"] = old_temperature - cooling_effect
        self.state["operative_temperature_mean_c"] = old_temperature - cooling_effect * 0.8
        self.state["observation_id"] = observation_id + 1
        return {
            "success": True,
            "terminal": "applied",
            "status": "applied",
            "run_id": run_id,
            "observation_id": observation_id,
            "proposed_action": applied,
            "applied_action": applied,
            "validation": {"accepted": True, "clamps": []},
            "source": "explicit_test_fake",
        }

    async def request_safe_fallback(
        self,
        run_id: str,
        observation_id: int,
    ) -> dict[str, Any]:
        self._run(run_id)
        if observation_id != self.state["observation_id"]:
            raise ValueError("stale observation")
        return {
            "success": True,
            "terminal": "fallback",
            "status": "fallback",
            "run_id": run_id,
            "observation_id": observation_id,
            "fallback_status": "deterministic_rule",
            "source": "explicit_test_fake",
        }

    async def get_last_energyplus_errors(self, run_id: str, limit: int) -> dict[str, Any]:
        self._run(run_id)
        return {"errors": [], "limit": limit, "source": "explicit_test_fake"}

    async def inspect_idf(self, path: Path) -> dict[str, Any]:
        stat = await asyncio.to_thread(path.stat)
        return {
            "path": str(path),
            "size_bytes": stat.st_size,
            "object_types": ["Version"],
            "source": "explicit_test_fake",
        }

    async def validate_idf(self, path: Path, weather_path: Path) -> dict[str, Any]:
        return {
            "valid": True,
            "path": str(path),
            "weather_path": str(weather_path),
            "source": "explicit_test_fake",
        }

    async def inspect_available_energyplus_points(
        self,
        run_id: str,
        query: str,
    ) -> dict[str, Any]:
        self._run(run_id)
        return {
            "query": query,
            "matches": [{"name": "Zone Mean Air Temperature", "key": "ZONE ONE"}],
            "source": "explicit_test_fake",
        }

    async def parse_energyplus_error_file(self, path: Path) -> dict[str, Any]:
        return {
            "path": str(path),
            "counts": {"warning": 0, "severe": 0, "fatal": 0},
            "source": "explicit_test_fake",
        }

    async def generate_replay_model(self, run_id: str) -> dict[str, Any]:
        self._run(run_id)
        return {
            "generated": True,
            "run_id": run_id,
            "source": "explicit_test_fake",
        }

    async def audit_tool_call(self, event: AuditEvent) -> None:
        self.audits.append(event)
        if self._audit_path is not None:
            payload = json.dumps(event.model_dump(mode="json"), sort_keys=True)
            await asyncio.to_thread(_append_jsonl, self._audit_path, payload)

    @staticmethod
    def _run(run_id: str) -> None:
        if run_id != "fake-run":
            raise ValueError("unknown fake run")


def _append_jsonl(path: Path, payload: str) -> None:
    """Append one audit record to the test harness's preconfigured file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.write("\n")
