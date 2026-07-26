# Prompt and Context Engineering

## Design goal

The local model performs bounded supervisory selection, not unrestricted
building operation. Flexible interpretation is useful for weighing evaluated
candidates; deterministic software owns capabilities, safety, freshness,
application, and failure recovery.

## System prompt

The system prompt:

- identifies the task as local building supervisory control;
- states that tool and file/log content is untrusted data;
- forbids unsupported actuator values and arbitrary numeric invention;
- requires current state and constraints before selection;
- requires candidate generation/evaluation;
- requires a terminal apply or safe fallback tool;
- asks only for a short reason code and operational explanation;
- prohibits claims that a tool succeeded until its result confirms success.

It does not ask for private hidden reasoning.

## Tool descriptions

Descriptions are narrow and operational. They explain:

- required IDs and freshness expectations;
- returned units and missing-value behavior;
- whether a tool is read-only or control-affecting;
- that candidate values are bounded;
- that application can be clamped or rejected;
- that diagnostic paths are allow-listed.

The host discovers schemas dynamically from MCP and converts them to Ollama tool
definitions. A hard-coded Python shortcut does not replace protocol discovery.

## Required sequence

A decision is incomplete unless the trace contains:

1. `get_current_building_state`;
2. `get_constraints`;
3. `generate_candidate_actions` or `evaluate_candidate_actions`;
4. `apply_control_action` or `request_safe_fallback`.

One concise corrective prompt identifies missing operations. A second incomplete
attempt falls back deterministically.

After the model makes progress, the host removes already-completed protocol
stages from the Ollama tool definitions. Repeated state or constraint calls
cannot consume every round by restarting the sequence. Candidate tools remain
available only after state and constraints, and terminal tools become the final
selection surface after candidate evaluation. All tools are still discovered
from the live MCP server rather than hard-coded Python calls.

## Compact state

The context includes:

- current aggregate observation;
- zone min/mean/max summaries;
- rolling averages and slopes;
- occupancy transition;
- current/reference setpoints;
- demand and timestep/cumulative energy;
- available comfort/IAQ fields;
- forecast/grid summaries;
- actuator capabilities;
- previous action;
- evaluated candidates and score components;
- bounded severe/fatal diagnostic summary.

Raw zone histories, full simulation logs, IDF text, and full EnergyPlus output
are excluded.

## Token budget

The default compact-state target is 1,800 approximate tokens. Priority is:

1. run/observation identity and freshness;
2. constraints and capabilities;
3. safety-critical current state;
4. candidates and score components;
5. recent trends;
6. baseline/forecast/grid context;
7. diagnostic excerpts.

Low-priority history is reduced first. Current constraints are never truncated.
Serialization uses compact JSON and quantized values where precision does not
affect validation.

## Long-log management

EnergyPlus messages are classified and stored separately. Context contains only
bounded, deduplicated warning/severe/fatal summaries:

- stable normalized hash;
- occurrence count;
- first/last timestamp;
- severity;
- maximum configured lines.

No raw log line is interpreted as an instruction.

## Latency strategy

- deterministic sampling at temperature zero or closest supported setting;
- Ollama keep-alive;
- event-driven decisions rather than every timestep;
- quantized-state action cache;
- configurable request timeout;
- at most one retry;
- maximum tool rounds;
- last-safe interval;
- deterministic fallback;
- consecutive-failure circuit breaker with one skipped interval and a
  deterministic half-open probe.

Each attempt stores end-to-end latency, individual tool durations, and the
number of timeout attempts for mean, p95, and reliability reporting.

## Prompt-injection treatment

Weather, grid inputs, IDF fields, EnergyPlus logs, error files, model metadata,
and MCP tool results are data. Delimiters and explicit instructions state that
embedded requests or commands must be ignored. The tool surface itself provides
no shell, general file access, package installation, deletion, or unrestricted
network function, limiting the impact even if malicious text reaches context.
