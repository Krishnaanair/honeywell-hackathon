"""Bounded local-model tool loop with mandatory MCP sequencing."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ecoloop.agent.audit import DecisionSink, NullDecisionSink
from ecoloop.agent.client import MCPClientError, MCPClientPort, tools_for_ollama
from ecoloop.agent.models import AgentDecision, HostToolTrace, ModelResponse, ToolRequest
from ecoloop.agent.ollama_host import ModelBackend
from ecoloop.agent.prompt import (
    SYSTEM_PROMPT,
    build_compact_context,
    candidate_selection_prompt,
    corrective_prompt,
)
from ecoloop.agent.reliability import ActionCache, FailureCircuitBreaker
from ecoloop.mcp.models import CandidateActionInput

REQUIRED_TOOL_NAMES = frozenset(
    {
        "get_current_building_state",
        "get_constraints",
        "generate_candidate_actions",
        "evaluate_candidate_actions",
        "apply_control_action",
        "request_safe_fallback",
    }
)
_CANDIDATE_FIELDS = (
    "heating_setpoint_c",
    "cooling_setpoint_c",
    "hold_minutes",
    "ventilation_multiplier",
    "lighting_fraction",
    "supply_air_temperature_c",
    "shading_state",
)
_CONTROL_ACTION_FIELDS = (
    "candidate_id",
    *_CANDIDATE_FIELDS,
    "action_generation",
    "reason_code",
    "explanation",
    "model",
    "latency_ms",
)


class AgentProtocolError(RuntimeError):
    """Raised when a terminal safe MCP action cannot be completed."""


@dataclass(frozen=True, slots=True)
class AgentHostConfig:
    """Reliability and prompt limits for one local controller host."""

    timeout_seconds: float = 30.0
    maximum_tool_rounds: int = 8
    maximum_consecutive_failures: int = 3
    state_token_budget: int = 1800
    retry_once: bool = True
    enable_action_cache: bool = True

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.maximum_tool_rounds < 1:
            raise ValueError("maximum_tool_rounds must be positive")
        if self.maximum_consecutive_failures < 1:
            raise ValueError("maximum_consecutive_failures must be positive")
        if self.state_token_budget < 256:
            raise ValueError("state_token_budget must be at least 256")


@dataclass(slots=True)
class _SequenceState:
    state_seen: bool = False
    constraints_seen: bool = False
    candidates_seen: bool = False
    observation_id: int | None = None
    current_state: dict[str, Any] = field(default_factory=dict)
    candidate_fingerprints: set[str] = field(default_factory=set)
    recommended_candidate: dict[str, Any] | None = None
    next_action_generation: int | None = None

    def missing(self) -> list[str]:
        required: list[str] = []
        if not self.state_seen:
            required.append("get_current_building_state")
        if not self.constraints_seen:
            required.append("get_constraints")
        if not self.candidates_seen:
            required.append("generate_candidate_actions or evaluate_candidate_actions")
        required.append("apply_control_action or request_safe_fallback")
        return required


class AgentHost:
    """Run a local Ollama tool loop exclusively through an MCP client."""

    def __init__(
        self,
        *,
        mcp_client: MCPClientPort,
        model: ModelBackend,
        config: AgentHostConfig | None = None,
        circuit_breaker: FailureCircuitBreaker | None = None,
        action_cache: ActionCache | None = None,
        decision_sink: DecisionSink | None = None,
    ) -> None:
        self._mcp = mcp_client
        self._model = model
        self._config = config or AgentHostConfig()
        self._breaker = circuit_breaker or FailureCircuitBreaker(
            self._config.maximum_consecutive_failures
        )
        self._cache = action_cache or ActionCache()
        self._decision_sink = decision_sink or NullDecisionSink()

    @property
    def circuit_breaker(self) -> FailureCircuitBreaker:
        """Expose breaker state for orchestration and reliability metrics."""

        return self._breaker

    async def decide(
        self,
        run_id: str,
        *,
        state_hint: dict[str, Any] | None = None,
    ) -> AgentDecision:
        """Complete one validated apply/fallback decision through MCP."""

        started_clock = time.perf_counter()
        trace: list[HostToolTrace] = []
        tools = await self._mcp.discover_tools()
        names = {tool.name for tool in tools}
        missing_tools = sorted(REQUIRED_TOOL_NAMES - names)
        if missing_tools:
            raise AgentProtocolError(
                "MCP server is missing required tools: " + ", ".join(missing_tools)
            )

        if self._breaker.is_open(run_id):
            return await self._fallback_decision(
                run_id,
                trace,
                started_clock=started_clock,
                sequence=_SequenceState(),
                rounds=0,
                corrective_used=False,
                timeout_count=0,
                fallback_status="circuit_breaker_open",
            )

        if self._config.enable_action_cache and state_hint:
            cached_action = self._cache.get(run_id, state_hint)
            if cached_action is not None:
                cached = await self._try_cached_action(
                    run_id,
                    state_hint,
                    cached_action,
                    trace,
                    started_clock,
                )
                if cached is not None:
                    self._breaker.record_success(run_id)
                    return cached

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_compact_context(
                    run_id,
                    state_hint,
                    token_budget=self._config.state_token_budget,
                ),
            },
        ]
        ollama_tools = tools_for_ollama(tools)
        sequence = _SequenceState()
        corrective_used = False
        candidate_selection_reprompt_used = False
        candidate_guidance_sent = False
        timeout_count = 0
        retry_used = False
        loop = asyncio.get_running_loop()
        decision_deadline = loop.time() + self._config.timeout_seconds * (
            2 if self._config.retry_once else 1
        )

        for round_number in range(1, self._config.maximum_tool_rounds + 1):
            remaining_seconds = decision_deadline - loop.time()
            if remaining_seconds <= 0:
                self._breaker.record_failure(run_id)
                return await self._fallback_decision(
                    run_id,
                    trace,
                    started_clock=started_clock,
                    sequence=sequence,
                    rounds=round_number,
                    corrective_used=corrective_used,
                    timeout_count=timeout_count,
                    fallback_status="decision_timeout",
                )
            try:
                response = await asyncio.wait_for(
                    self._model.complete(messages, ollama_tools),
                    timeout=min(self._config.timeout_seconds, remaining_seconds),
                )
            except TimeoutError:
                timeout_count += 1
                if self._config.retry_once and not retry_used:
                    retry_used = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The previous local inference timed out. Retry once, use the "
                                "required MCP sequence, and finish with a terminal safe tool."
                            ),
                        }
                    )
                    continue
                self._breaker.record_failure(run_id)
                return await self._fallback_decision(
                    run_id,
                    trace,
                    started_clock=started_clock,
                    sequence=sequence,
                    rounds=round_number,
                    corrective_used=corrective_used,
                    timeout_count=timeout_count,
                    fallback_status="model_timeout",
                )
            except (RuntimeError, ValueError, OSError) as exc:
                self._breaker.record_failure(run_id)
                return await self._fallback_decision(
                    run_id,
                    trace,
                    started_clock=started_clock,
                    sequence=sequence,
                    rounds=round_number,
                    corrective_used=corrective_used,
                    timeout_count=timeout_count,
                    fallback_status=f"model_error:{_bounded_error(exc)}",
                )

            response = response.model_copy(
                update={
                    "tool_calls": [
                        _normalize_model_request(request, run_id) for request in response.tool_calls
                    ]
                }
            )
            messages.append(_assistant_message(response))
            if (
                not response.tool_calls
                and sequence.recommended_candidate is not None
                and not candidate_selection_reprompt_used
            ):
                candidate_selection_reprompt_used = True
                messages.append(
                    {
                        "role": "user",
                        "content": candidate_selection_prompt(
                            run_id,
                            observation_id=_required_observation_id(sequence),
                            action_generation=(
                                sequence.next_action_generation
                                or _next_generation(sequence.current_state)
                            ),
                            candidate=sequence.recommended_candidate,
                        ),
                    }
                )
                continue
            violation = _first_violation(response.tool_calls, run_id, sequence, names)
            if violation is not None or not response.tool_calls:
                if corrective_used:
                    self._breaker.record_failure(run_id)
                    return await self._fallback_decision(
                        run_id,
                        trace,
                        started_clock=started_clock,
                        sequence=sequence,
                        rounds=round_number,
                        corrective_used=True,
                        timeout_count=timeout_count,
                        fallback_status="tool_sequence_rejected",
                    )
                corrective_used = True
                messages.append(
                    {
                        "role": "user",
                        "content": corrective_prompt(run_id, sequence.missing()),
                    }
                )
                continue

            terminal: tuple[str, dict[str, Any], ToolRequest] | None = None
            candidate_selection_deferred = False
            for request in response.tool_calls:
                current_violation = _tool_violation(request, run_id, sequence, names)
                if current_violation is not None:
                    violation = current_violation
                    break
                request = _with_authoritative_action_metadata(
                    request,
                    model=self._model.model_name,
                    latency_ms=max(0.0, (time.perf_counter() - started_clock) * 1000.0),
                )
                if (
                    request.name == "request_safe_fallback"
                    and sequence.recommended_candidate is not None
                    and not candidate_selection_reprompt_used
                ):
                    candidate_selection_reprompt_used = True
                    candidate_selection_deferred = True
                    messages.append(
                        {
                            "role": "user",
                            "content": candidate_selection_prompt(
                                run_id,
                                observation_id=_required_observation_id(sequence),
                                action_generation=(
                                    sequence.next_action_generation
                                    or _next_generation(sequence.current_state)
                                ),
                                candidate=sequence.recommended_candidate,
                            ),
                        }
                    )
                    break
                try:
                    result = await self._call_and_trace(request, trace)
                except MCPClientError:
                    if request.name == "apply_control_action":
                        self._breaker.record_failure(run_id)
                        return await self._fallback_decision(
                            run_id,
                            trace,
                            started_clock=started_clock,
                            sequence=sequence,
                            rounds=round_number,
                            corrective_used=corrective_used,
                            timeout_count=timeout_count,
                            fallback_status="invalid_action",
                        )
                    violation = f"MCP call failed: {request.name}"
                    break
                _advance_sequence(sequence, request, result)
                messages.append(_tool_result_message(request.name, result))
                if (
                    request.name in {"generate_candidate_actions", "evaluate_candidate_actions"}
                    and sequence.recommended_candidate is not None
                    and not candidate_guidance_sent
                ):
                    candidate_guidance_sent = True
                    messages.append(
                        {
                            "role": "user",
                            "content": candidate_selection_prompt(
                                run_id,
                                observation_id=_required_observation_id(sequence),
                                action_generation=(
                                    sequence.next_action_generation
                                    or _next_generation(sequence.current_state)
                                ),
                                candidate=sequence.recommended_candidate,
                            ),
                        }
                    )
                if request.name in {"apply_control_action", "request_safe_fallback"}:
                    if _terminal_success(request.name, result):
                        terminal = (request.name, result, request)
                        break
                    if request.name == "apply_control_action":
                        self._breaker.record_failure(run_id)
                        return await self._fallback_decision(
                            run_id,
                            trace,
                            started_clock=started_clock,
                            sequence=sequence,
                            rounds=round_number,
                            corrective_used=corrective_used,
                            timeout_count=timeout_count,
                            fallback_status="invalid_action",
                        )
                    violation = f"terminal tool did not succeed: {request.name}"
                    break

            if candidate_selection_deferred:
                continue

            if terminal is not None:
                tool_name, result, terminal_request = terminal
                if tool_name == "apply_control_action":
                    self._breaker.record_success(run_id)
                    self._remember_action(run_id, sequence, terminal_request)
                else:
                    self._breaker.record_failure(run_id)
                decision = _make_decision(
                    run_id=run_id,
                    observation_id=_required_observation_id(sequence),
                    status="applied" if tool_name == "apply_control_action" else "fallback",
                    result=result,
                    model=response.model or self._model.model_name,
                    started_clock=started_clock,
                    rounds=round_number,
                    trace=trace,
                    corrective_used=corrective_used,
                    timeout_count=timeout_count,
                    fallback_status=(
                        str(result.get("fallback_status", "model_requested"))
                        if tool_name == "request_safe_fallback"
                        else None
                    ),
                )
                await self._decision_sink.record(
                    decision,
                    state_summary=sequence.current_state,
                )
                return decision

            if violation is not None:
                if corrective_used:
                    self._breaker.record_failure(run_id)
                    return await self._fallback_decision(
                        run_id,
                        trace,
                        started_clock=started_clock,
                        sequence=sequence,
                        rounds=round_number,
                        corrective_used=True,
                        timeout_count=timeout_count,
                        fallback_status="tool_sequence_rejected",
                    )
                corrective_used = True
                messages.append(
                    {
                        "role": "user",
                        "content": corrective_prompt(run_id, sequence.missing()),
                    }
                )

        self._breaker.record_failure(run_id)
        return await self._fallback_decision(
            run_id,
            trace,
            started_clock=started_clock,
            sequence=sequence,
            rounds=self._config.maximum_tool_rounds,
            corrective_used=corrective_used,
            timeout_count=timeout_count,
            fallback_status="maximum_tool_rounds",
        )

    async def _try_cached_action(
        self,
        run_id: str,
        state_hint: dict[str, Any],
        cached_action: dict[str, Any],
        trace: list[HostToolTrace],
        started_clock: float,
    ) -> AgentDecision | None:
        sequence = _SequenceState()
        state_request = ToolRequest(
            name="get_current_building_state",
            arguments={"run_id": run_id},
        )
        state_result = await self._call_and_trace(state_request, trace)
        _advance_sequence(sequence, state_request, state_result)
        actual_state = sequence.current_state
        if self._cache.get(run_id, actual_state) is None:
            return None

        constraints_request = ToolRequest(name="get_constraints", arguments={"run_id": run_id})
        constraints_result = await self._call_and_trace(constraints_request, trace)
        _advance_sequence(sequence, constraints_request, constraints_result)

        candidate = {
            key: value
            for key, value in cached_action.items()
            if key in _CANDIDATE_FIELDS and value is not None
        }
        evaluate_request = ToolRequest(
            name="evaluate_candidate_actions",
            arguments={"run_id": run_id, "candidates": [candidate]},
        )
        evaluated = await self._call_and_trace(evaluate_request, trace)
        _advance_sequence(sequence, evaluate_request, evaluated)
        sequence.candidate_fingerprints.add(_candidate_fingerprint(candidate))

        generation = _next_generation(actual_state)
        action = {
            **candidate,
            "action_generation": generation,
            "reason_code": "SCHEDULE_TRACKING",
            "explanation": "Reused a recently validated candidate for an equivalent state.",
            "model": self._model.model_name,
            "latency_ms": max(0.0, (time.perf_counter() - started_clock) * 1000.0),
        }
        apply_request = ToolRequest(
            name="apply_control_action",
            arguments={
                "run_id": run_id,
                "observation_id": _required_observation_id(sequence),
                "action": action,
            },
        )
        try:
            applied = await self._call_and_trace(apply_request, trace)
        except MCPClientError:
            return None
        if not _terminal_success("apply_control_action", applied):
            return None
        decision = _make_decision(
            run_id=run_id,
            observation_id=_required_observation_id(sequence),
            status="applied",
            result=applied,
            model=self._model.model_name,
            started_clock=started_clock,
            rounds=0,
            trace=trace,
            corrective_used=False,
            timeout_count=0,
            fallback_status=None,
            cache_hit=True,
        )
        await self._decision_sink.record(
            decision,
            state_summary=sequence.current_state,
        )
        return decision

    async def _fallback_decision(
        self,
        run_id: str,
        trace: list[HostToolTrace],
        *,
        started_clock: float,
        sequence: _SequenceState,
        rounds: int,
        corrective_used: bool,
        timeout_count: int,
        fallback_status: str,
    ) -> AgentDecision:
        if not sequence.state_seen:
            state_request = ToolRequest(
                name="get_current_building_state",
                arguments={"run_id": run_id},
            )
            try:
                state_result = await self._call_and_trace(state_request, trace)
            except MCPClientError as exc:
                raise AgentProtocolError("safe fallback could not read current state") from exc
            _advance_sequence(sequence, state_request, state_result)
        if not sequence.constraints_seen:
            constraints_request = ToolRequest(
                name="get_constraints",
                arguments={"run_id": run_id},
            )
            try:
                constraints_result = await self._call_and_trace(constraints_request, trace)
            except MCPClientError as exc:
                raise AgentProtocolError("safe fallback could not read constraints") from exc
            _advance_sequence(sequence, constraints_request, constraints_result)

        fallback_request = ToolRequest(
            name="request_safe_fallback",
            arguments={
                "run_id": run_id,
                "observation_id": _required_observation_id(sequence),
            },
        )
        try:
            result = await self._call_and_trace(fallback_request, trace)
        except MCPClientError as exc:
            raise AgentProtocolError("deterministic fallback tool failed") from exc
        if not _terminal_success("request_safe_fallback", result):
            raise AgentProtocolError("deterministic fallback was not applied")
        decision = _make_decision(
            run_id=run_id,
            observation_id=_required_observation_id(sequence),
            status="fallback",
            result=result,
            model=self._model.model_name,
            started_clock=started_clock,
            rounds=rounds,
            trace=trace,
            corrective_used=corrective_used,
            timeout_count=timeout_count,
            fallback_status=str(result.get("fallback_status", fallback_status)),
        )
        await self._decision_sink.record(
            decision,
            state_summary=sequence.current_state,
        )
        return decision

    async def _call_and_trace(
        self,
        request: ToolRequest,
        trace: list[HostToolTrace],
    ) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        started_clock = time.perf_counter()
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = await self._mcp.call_tool(request.name, request.arguments)
            return result
        except (MCPClientError, ValueError, TypeError, RuntimeError, OSError) as exc:
            error = _bounded_error(exc)
            raise MCPClientError(error) from exc
        finally:
            trace.append(
                HostToolTrace(
                    sequence=len(trace) + 1,
                    tool_name=request.name,
                    arguments=request.arguments,
                    result=result,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    latency_ms=max(0.0, (time.perf_counter() - started_clock) * 1000.0),
                    success=error is None,
                    error=error,
                )
            )

    def _remember_action(
        self,
        run_id: str,
        sequence: _SequenceState,
        request: ToolRequest,
    ) -> None:
        action = request.arguments.get("action")
        if not isinstance(action, dict) or not sequence.current_state:
            return
        candidate = {
            key: value
            for key, value in action.items()
            if key in _CANDIDATE_FIELDS and value is not None
        }
        self._cache.put(run_id, sequence.current_state, candidate)


def _first_violation(
    calls: list[ToolRequest],
    run_id: str,
    sequence: _SequenceState,
    discovered: set[str],
) -> str | None:
    shadow = _SequenceState(
        state_seen=sequence.state_seen,
        constraints_seen=sequence.constraints_seen,
        candidates_seen=sequence.candidates_seen,
        observation_id=sequence.observation_id,
        current_state=sequence.current_state,
        candidate_fingerprints=set(sequence.candidate_fingerprints),
        recommended_candidate=sequence.recommended_candidate,
        next_action_generation=sequence.next_action_generation,
    )
    for call in calls:
        violation = _tool_violation(call, run_id, shadow, discovered, preflight=True)
        if violation is not None:
            return violation
        if call.name == "get_current_building_state":
            shadow.state_seen = True
        elif call.name == "get_constraints":
            shadow.constraints_seen = True
        elif call.name in {"generate_candidate_actions", "evaluate_candidate_actions"}:
            shadow.candidates_seen = True
    return None


def _normalize_model_request(request: ToolRequest, run_id: str) -> ToolRequest:
    """Normalize a bounded flat action emitted by some local tool-call models."""

    arguments = dict(request.arguments)
    if request.name in REQUIRED_TOOL_NAMES and "run_id" not in arguments:
        arguments["run_id"] = run_id
    if request.name != "apply_control_action":
        return request.model_copy(update={"arguments": arguments})

    nested = arguments.get("action")
    action = dict(nested) if isinstance(nested, dict) else {}
    for key in _CONTROL_ACTION_FIELDS:
        if key in arguments and key not in action:
            action[key] = arguments[key]
    normalized: dict[str, Any] = {
        "run_id": arguments.get("run_id", run_id),
        "observation_id": arguments.get("observation_id"),
        "action": action,
    }
    return request.model_copy(update={"arguments": normalized})


def _tool_violation(
    request: ToolRequest,
    run_id: str,
    sequence: _SequenceState,
    discovered: set[str],
    *,
    preflight: bool = False,
) -> str | None:
    if request.name not in discovered:
        return f"undiscovered tool: {request.name}"
    supplied_run = request.arguments.get("run_id")
    if supplied_run is not None and supplied_run != run_id:
        return "wrong run_id"
    if request.name == "get_current_building_state":
        if supplied_run != run_id:
            return "get_current_building_state requires the current run_id"
        return None
    if request.name == "get_constraints":
        if not sequence.state_seen:
            return "get_constraints must follow get_current_building_state"
        return None
    if request.name in {"generate_candidate_actions", "evaluate_candidate_actions"}:
        if not sequence.state_seen or not sequence.constraints_seen:
            return "candidate tools require current state and constraints"
        return None
    if request.name in {"apply_control_action", "request_safe_fallback"}:
        if not (sequence.state_seen and sequence.constraints_seen and sequence.candidates_seen):
            return "terminal tools require state, constraints, and candidates"
        observation_id = request.arguments.get("observation_id")
        if not preflight and observation_id != sequence.observation_id:
            return "terminal tool observation_id is stale or mismatched"
        if request.name == "apply_control_action" and not preflight:
            action = request.arguments.get("action")
            if not isinstance(action, dict):
                return "apply_control_action requires an action object"
            if sequence.candidate_fingerprints and (
                _candidate_fingerprint(action) not in sequence.candidate_fingerprints
            ):
                return "selected action was not among generated or evaluated candidates"
        return None
    if not sequence.state_seen:
        return "diagnostic and context tools require current state first"
    return None


def _advance_sequence(
    sequence: _SequenceState,
    request: ToolRequest,
    result: dict[str, Any],
) -> None:
    if request.name == "get_current_building_state":
        sequence.state_seen = True
        sequence.current_state = _state_object(result)
        sequence.observation_id = _extract_observation_id(result)
    elif request.name == "get_constraints":
        sequence.constraints_seen = True
        generation = result.get("next_action_generation")
        if isinstance(generation, int) and generation >= 1:
            sequence.next_action_generation = generation
    elif request.name in {"generate_candidate_actions", "evaluate_candidate_actions"}:
        sequence.candidates_seen = True
        sequence.candidate_fingerprints.update(_extract_candidate_fingerprints(request, result))
        sequence.recommended_candidate = _extract_recommended_candidate(result)


def _extract_observation_id(result: dict[str, Any]) -> int:
    values: list[Any] = [result.get("observation_id")]
    for key in ("observation", "state", "current_state"):
        nested = result.get(key)
        if isinstance(nested, dict):
            values.append(nested.get("observation_id"))
    for value in values:
        if isinstance(value, int) and value >= 1:
            return value
    raise MCPClientError("current building state did not include a valid observation_id")


def _state_object(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("observation", "state", "current_state"):
        nested = result.get(key)
        if isinstance(nested, dict):
            return dict(nested)
    return dict(result)


def _extract_candidate_fingerprints(
    request: ToolRequest,
    result: dict[str, Any],
) -> set[str]:
    candidates: list[dict[str, Any]] = []
    supplied = request.arguments.get("candidates")
    if isinstance(supplied, list):
        candidates.extend(item for item in supplied if isinstance(item, dict))
    for key in ("candidates", "evaluated_candidates", "scores"):
        values = result.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict):
                continue
            candidate = value.get("candidate")
            candidates.append(candidate if isinstance(candidate, dict) else value)
    return {_candidate_fingerprint(candidate) for candidate in candidates}


def _extract_recommended_candidate(result: dict[str, Any]) -> dict[str, Any] | None:
    explicit = result.get("recommended_candidate")
    if isinstance(explicit, dict):
        return dict(explicit)
    for key in ("scores", "evaluated_candidates"):
        values = result.get(key)
        if not isinstance(values, list) or not values:
            continue
        first = values[0]
        if not isinstance(first, dict):
            continue
        candidate = first.get("candidate")
        if isinstance(candidate, dict):
            return dict(candidate)
    candidates = result.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return dict(candidates[0])
    return None


def _candidate_fingerprint(action: dict[str, Any]) -> str:
    core = {key: action.get(key) for key in _CANDIDATE_FIELDS if action.get(key) is not None}
    try:
        candidate = CandidateActionInput.model_validate(core)
    except ValueError:
        return "__invalid_candidate__"
    canonical = candidate.model_dump(
        mode="json",
        exclude={"candidate_id"},
        exclude_none=True,
    )
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _terminal_success(name: str, result: dict[str, Any]) -> bool:
    if result.get("success") is not True:
        return False
    terminal = result.get("terminal")
    if terminal is None:
        status = str(result.get("status", "")).casefold()
        return status in (
            {"applied", "success"}
            if name == "apply_control_action"
            else {"fallback", "fallback_applied", "success"}
        )
    return str(terminal) == ("applied" if name == "apply_control_action" else "fallback")


def _required_observation_id(sequence: _SequenceState) -> int:
    if sequence.observation_id is None:
        raise AgentProtocolError("no current observation_id is available")
    return sequence.observation_id


def _next_generation(state: dict[str, Any]) -> int:
    values: list[Any] = [state.get("action_generation"), state.get("latest_action_generation")]
    previous = state.get("previous_action")
    if isinstance(previous, dict):
        values.append(previous.get("action_generation"))
    generations = [value for value in values if isinstance(value, int) and value >= 0]
    return max(generations, default=0) + 1


def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.content[:500],
    }
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                }
            }
            for call in response.tool_calls
        ]
    return message


def _with_authoritative_action_metadata(
    request: ToolRequest,
    *,
    model: str,
    latency_ms: float,
) -> ToolRequest:
    if request.name != "apply_control_action":
        return request
    action = request.arguments.get("action")
    if not isinstance(action, dict):
        return request
    arguments = dict(request.arguments)
    arguments["action"] = {
        **action,
        "model": model,
        "latency_ms": latency_ms,
    }
    return request.model_copy(update={"arguments": arguments})


def _tool_result_message(name: str, result: dict[str, Any]) -> dict[str, Any]:
    model_result = (
        _compact_candidate_result(result)
        if name in {"generate_candidate_actions", "evaluate_candidate_actions"}
        else result
    )
    encoded = json.dumps(
        model_result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if len(encoded) > 8000:
        encoded = json.dumps(
            {
                "truncated": True,
                "summary": encoded[:7800],
            },
            separators=(",", ":"),
        )
    return {"role": "tool", "tool_name": name, "content": encoded}


def _compact_candidate_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep a small, complete selection surface for the next model turn."""

    compact: dict[str, Any] = {
        key: (
            _compact_candidate(result[key])
            if key == "recommended_candidate" and isinstance(result[key], dict)
            else result[key]
        )
        for key in (
            "run_id",
            "observation_id",
            "recommended_candidate",
            "recommended_total_score",
            "application_context",
            "scoring_method",
            "selection_guidance",
            "source",
        )
        if key in result
    }
    compact["next_required_tool"] = "apply_control_action or request_safe_fallback"
    for key in ("scores", "evaluated_candidates"):
        values = result.get(key)
        if isinstance(values, list):
            compact[key] = [
                _compact_candidate_score(value) for value in values[:3] if isinstance(value, dict)
            ]
            compact[f"{key}_count"] = len(values)
    candidates = result.get("candidates")
    if (
        isinstance(candidates, list)
        and "scores" not in compact
        and "evaluated_candidates" not in compact
    ):
        compact["candidates"] = [
            _compact_candidate(value) for value in candidates[:3] if isinstance(value, dict)
        ]
        compact["candidate_count"] = len(candidates)
    compact["result_compacted_for_model"] = True
    return compact


def _compact_candidate_score(value: dict[str, Any]) -> dict[str, Any]:
    candidate = value.get("candidate")
    summary: dict[str, Any] = {}
    if isinstance(candidate, dict):
        summary["candidate"] = _compact_candidate(candidate)
    for key in (
        "total_score",
        "components",
        "predicted_demand_kw",
        "predicted_operative_temperature_c",
    ):
        if key in value:
            summary[key] = value[key]
    return summary


def _compact_candidate(value: dict[str, Any]) -> dict[str, Any]:
    allowed = ("candidate_id", *_CANDIDATE_FIELDS)
    return {key: value[key] for key in allowed if value.get(key) is not None}


def _make_decision(
    *,
    run_id: str,
    observation_id: int,
    status: str,
    result: dict[str, Any],
    model: str,
    started_clock: float,
    rounds: int,
    trace: list[HostToolTrace],
    corrective_used: bool,
    timeout_count: int,
    fallback_status: str | None,
    cache_hit: bool = False,
) -> AgentDecision:
    return AgentDecision(
        run_id=run_id,
        observation_id=observation_id,
        status=status,
        result=result,
        model=model,
        latency_ms=max(0.0, (time.perf_counter() - started_clock) * 1000.0),
        tool_rounds=rounds,
        trace=trace,
        corrective_reprompt_used=corrective_used,
        timeout_count=timeout_count,
        fallback_status=fallback_status,
        cache_hit=cache_hit,
    )


def _bounded_error(exc: BaseException) -> str:
    return (" ".join(str(exc).split()) or exc.__class__.__name__)[:500]
