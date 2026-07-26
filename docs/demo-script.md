# Three-minute Demo Script

The primary demo is live. `DEMO_REPLAY` is a clearly labelled visualization of a
previously completed real run and never replaces the real control integration
test.

## Preparation

```powershell
uv sync --extra dev
uv run python -m ecoloop doctor
uv run python -m ecoloop prepare-model
uv run python -m ecoloop run baseline --period demo
uv run python -m ecoloop demo
```

Before recording, verify that `runs/current_run.txt` contains the controlled run
ID and that the dashboard says either `LIVE` or `REAL RUN REPLAY`.

## Shot list and narration

### 0:00-0:20 - Problem and thesis

Screen: README title and architecture diagram.

Narration:

“Building schedules cannot react to occupancy, comfort, demand, and weather at
the same time. EcoLoop adds a local supervisory loop without giving a language
model direct, unchecked control.”

Caption-only alternative:

`Fixed schedules miss changing conditions. EcoLoop adds local adaptive control
with deterministic guardrails.`

### 0:20-0:45 - Environment truth check

Screen: `python -m ecoloop doctor`.

Narration:

“The doctor verifies the exact EnergyPlus version, PyEnergyPlus, dynamic
library, model, weather, Ollama API, selected model, and writable artifact
directories. Missing dependencies block the real demo instead of silently
switching to fake data.”

Caption:

`Every external dependency is discovered and version-checked.`

### 0:45-1:15 - Closed-loop architecture

Screen: architecture document, then terminal showing MCP server and simulation
start.

Narration:

“EnergyPlus writes one observation per zone timestep to SQLite. A real MCP stdio
client discovers narrow tools. Ollama examines compact state, constraints, and
evaluated candidates. The independent validator stores proposed and applied
values before the next callback actuates thermostat schedules.”

Caption:

`EnergyPlus -> SQLite -> MCP -> local Ollama -> validator -> actuator -> physical response`

### 1:15-1:55 - Live operations

Screen: dashboard Live Operations.

Narration:

“The dashboard reads the run database: simulation time, progress, zone
temperatures, occupancy, comfort, demand, current setpoints, proposed versus
applied action, tool calls, latency, and fallback status. Display delay changes
only wall-clock presentation speed.”

Caption:

`Real telemetry and control evidence, not a fabricated animation.`

### 1:55-2:25 - Guardrails and trace

Screen: Agent Decisions, then Reliability and Errors.

Narration:

“Every choice is bounded to evaluated candidates. Here the trace shows required
tools, score components, proposed values, safety clamps, applied action, reason
code, and latency. Timeouts use one last-safe interval, then deterministic
fallback, and repeated failures open a circuit breaker.”

Caption:

`The model proposes. Deterministic software validates and applies.`

### 2:25-2:50 - Baseline comparison

Screen: Baseline vs Agent.

Narration:

“Both cases use the same model, weather, dates, timestep, and internal loads.
Totals come from EnergyPlus outputs and are cross-checked against telemetry.
Electricity, peak, comfort, cost, carbon, and other fuels are reported
independently.”

Caption:

`Matched runs + official output totals + telemetry cross-check.`

### 2:50-3:00 - Close

Screen: repository layout and submission artifacts.

Narration:

“The same evidence exports to replay schedules, reports, checksums, and the
submission package. The path to real buildings is a commissioned BACnet or MQTT
adapter behind the same capability and safety boundary.”

Caption:

`Simulation proof today; commissioned protocol adapters next.`

## Failure-recovery recording alternative

If the live model is too slow:

1. Do not use `--fake`.
2. Stop cleanly and show the timeout/fallback record.
3. Run `python -m ecoloop replay REAL_COMPLETED_RUN_ID`.
4. Show the `REAL RUN REPLAY` label and original run ID.
5. Explain that replay uses previously recorded real telemetry/actions and makes
   no model call.

If no real completed run exists, do not record a results demo. Record only the
doctor blocker and implementation tour.
