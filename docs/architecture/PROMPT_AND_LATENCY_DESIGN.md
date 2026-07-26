# Prompt and Latency Design

The canonical detailed prompt discussion is
[`../prompt-engineering.md`](../prompt-engineering.md). This document records the
runtime acceptance contract.

## Supervisory prompt contract

The system prompt identifies the model as a supervisory strategy selector, not
the safety authority. It requires the following MCP-backed progression:

1. `get_current_building_state`
2. `get_constraints`
3. `generate_candidate_actions` or `evaluate_candidate_actions`
4. `apply_control_action` or `request_safe_fallback`

The model receives compact JSON containing the current observation, selected
zone summaries, capabilities, aligned baseline reference when compatible,
recent bounded trends, previous approved action, and configured grid signals.
It never receives full simulation output, arbitrary IDF text, or unbounded logs.

The terminal response stores a short operational explanation and reason code.
EcoLoop neither asks for nor records hidden chain-of-thought.

## Tool selection and validation

The official MCP client discovers tools dynamically and converts their JSON
schemas into Ollama tool definitions. Unknown tools and invalid arguments are
rejected by protocol/schema boundaries. If the first model attempt skips a
required tool, the host gives one concise corrective reprompt. A second failure
activates deterministic fallback.

Tool descriptions explicitly state that candidates are bounded and that scoring
is a transparent one-step heuristic, not model-predictive control.

## Context compression

- zone details are capped and projected to control-relevant fields;
- historical windows are bounded by step count;
- trends carry current, mean, min, max, slope and sample count;
- EnergyPlus errors are classified, deduplicated by hash and line-limited;
- raw logs and file contents are never inserted as instructions;
- semantically similar quantized state may use a cached decision;
- prompt payload is serialized as compact JSON under the configured budget.

Log/IDF content is treated as quoted untrusted data. Text found there cannot
change the system policy or grant tools.

## Timing and failure recovery

Inference runs outside the EnergyPlus child. The coordinator applies a
configurable `LLM_TIMEOUT_SECONDS`, at most one retry, local Ollama keep-alive,
and per-run circuit breaker. It records request start/end, total latency, tool
latency, timeout count, cache hit and fallback status.

The accelerated EnergyPlus demo uses a bounded exact-observation handshake so
the simulation cannot race many hours ahead of local inference. On timeout,
EcoLoop may reuse the last-known-safe action for one interval and then switches
to deterministic fallback. Repeated failures disable model control for the run.
The EnergyPlus callback never waits without a deadline.

This latency design is intentionally different from a field deployment. A
BACnet/MQTT building operates in wall-clock time, so the durable latest-state
queue can remain entirely nonblocking while the supervisory worker responds
within the next control interval.
