"""Durable recording for completed local supervisory decisions."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol

from ecoloop.agent.models import AgentDecision
from ecoloop.db.store import SQLiteStore
from ecoloop.schemas import CandidateScore


class DecisionSink(Protocol):
    """Persistence boundary used after a terminal MCP action succeeds."""

    async def record(
        self,
        decision: AgentDecision,
        *,
        state_summary: dict[str, Any],
    ) -> None:
        """Persist a terminal decision without private model reasoning."""


class NullDecisionSink:
    """No-op sink for explicit tests and callers that persist elsewhere."""

    async def record(
        self,
        decision: AgentDecision,
        *,
        state_summary: dict[str, Any],
    ) -> None:
        del decision, state_summary


class SQLiteDecisionSink:
    """Write terminal host outcomes to the SQLite agent_decisions table."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def record(
        self,
        decision: AgentDecision,
        *,
        state_summary: dict[str, Any],
    ) -> None:
        """Extract operational metadata and persist it off the event loop."""

        candidate_scores = _candidate_scores(decision)
        action = _terminal_action_arguments(decision)
        await asyncio.to_thread(
            self._store.record_agent_decision,
            decision_id=f"decision-{uuid.uuid4().hex}",
            run_id=decision.run_id,
            observation_id=decision.observation_id,
            model=decision.model,
            latency_ms=decision.latency_ms,
            completed=True,
            candidate_scores=candidate_scores,
            state_summary=state_summary,
            action_generation=_integer_or_none(action.get("action_generation")),
            reason_code=_reason_code_or_none(action.get("reason_code")),
            explanation=_string_or_none(action.get("explanation")),
            fallback_status=decision.fallback_status,
        )


def _candidate_scores(decision: AgentDecision) -> tuple[CandidateScore, ...]:
    scores: list[CandidateScore] = []
    for trace in decision.trace:
        if trace.result is None:
            continue
        values = trace.result.get("scores")
        if not isinstance(values, list):
            values = trace.result.get("evaluated_candidates")
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            try:
                scores.append(CandidateScore.model_validate(value))
            except ValueError:
                continue
    return tuple(scores)


def _terminal_action_arguments(decision: AgentDecision) -> dict[str, Any]:
    for trace in reversed(decision.trace):
        if trace.tool_name != "apply_control_action":
            continue
        action = trace.arguments.get("action")
        if isinstance(action, dict):
            return action
    return {}


def _integer_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _reason_code_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return "schedule_tracking" if value == "CACHE_REUSE" else value.casefold()
