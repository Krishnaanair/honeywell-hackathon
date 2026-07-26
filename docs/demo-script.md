# Three-minute Demo Script

The primary demo is a live real run. For a stable recording, the dashboard can
replay a previously completed real run with a prominent `REAL RUN REPLAY` banner.
Neither path uses fabricated telemetry. A schedule-driven EnergyPlus replay is a
separate reproducibility test described below.

## Preparation

Run from the repository root:

```powershell
uv sync --extra dev --locked
uv run python -m ecoloop doctor
uv run python -m ecoloop prepare-model --period demo
uv run python -m ecoloop demo --display-delay-seconds 0.15
```

The demo command reruns the doctor, reuses or creates a verified real demo-period
baseline, starts the dashboard, launches the MCP-backed controlled process, and
prints:

- `Dashboard: http://127.0.0.1:8501`
- the baseline run ID;
- the controlled run ID;
- the MCP and Ollama connection summary.

Wait for those lines before recording. Open the URL, click **Refresh evidence**,
and select the exact controlled run ID printed by the terminal. Confirm that the
selected evidence strip shows the expected run type, period, ID, and status.
`runs/current_run.txt` must contain the same controlled ID.

The optional display delay changes wall-clock presentation speed only. It does
not change simulated timestamps, control cadence, or calculated energy.

## Stable real-run visualization

When a live model response would make the recording timing unpredictable, start
the dashboard against a completed real run:

```powershell
$env:ECOLOOP_DEMO_REPLAY = "1"
$env:ECOLOOP_DEMO_REPLAY_RUN_ID = "<COMPLETED_REAL_AGENT_RUN_ID>"
uv run python -m ecoloop dashboard
```

Confirm the `REAL RUN REPLAY` banner and source run ID remain visible. The
sidebar frame and speed controls change visualization only; no model call or new
simulation occurs.

## Three-minute shot list and narration

### 0:00-0:18 - Problem and control thesis

Screen transition: README title -> architecture diagram.

Narration:

"Fixed schedules cannot respond to occupancy, comfort, demand, and weather at
the same time. EcoLoop adds local adaptive supervision without giving the model
unchecked actuator authority."

Caption:

`Adaptive local control, bounded by deterministic safety.`

### 0:18-0:38 - Environment truth check

Screen transition: terminal showing the completed doctor table.

Narration:

"The doctor verifies EnergyPlus 26.1, PyEnergyPlus, the dynamic library, model,
weather, Ollama endpoint, selected model, and writable artifact directories.
Missing dependencies block the real demo; they never trigger silent fake data."

Caption:

`Every external dependency is discovered and version-checked.`

### 0:38-0:58 - Real process boundary

Screen transition: terminal lines showing dashboard URL, baseline and controlled
run IDs, MCP status, and Ollama model -> browser.

Narration:

"EnergyPlus records one observation per zone timestep. A real MCP stdio client
discovers narrow tools. The local model selects an evaluated candidate, and an
independent validator stores proposed and applied values before actuation."

Caption:

`EnergyPlus -> SQLite -> MCP -> local model -> validator -> actuator`

### 0:58-1:35 - Live Operations

Screen: **Live Operations**. Keep the selected evidence strip visible.

Show:

- simulation clock and progress;
- zone temperatures, occupancy, comfort, demand, and outdoor condition;
- current heating and cooling setpoints;
- latest proposed and applied action;
- fallback status, recent MCP calls, and decision latency.

Narration:

"This dashboard reads the SQLite evidence bus. The simulation clock, physical
state, active setpoints, proposed and applied actions, tool calls, latency, and
fallback status all come from the selected real run."

Caption:

`Real telemetry and control evidence, not an animation.`

### 1:35-2:02 - Agent Decisions

Screen transition: **Agent Decisions** -> latest decision and tool trace.

Narration:

"The trace shows the compact state summary, evaluated candidates, selected
action, proposed-versus-applied values, safety modifications, reason code,
latency, and MCP tools called. Private hidden reasoning is neither requested nor
stored."

Caption:

`The model proposes. Deterministic software validates and applies.`

### 2:02-2:22 - Reliability and Errors

Screen transition: **Reliability and Errors**.

Narration:

"Timeouts hold the last safe action for one interval, then use deterministic
fallback. Repeated failures open a circuit breaker. EnergyPlus warnings, severe
errors, fatal errors, clamps, fallbacks, and timeout attempts stay visible."

Caption:

`Failures are bounded, recorded, and recoverable.`

### 2:22-2:48 - Baseline vs Agent

Screen transition: **Baseline vs Agent**.

If the dashboard says `Compatible completed real runs.`, keep both run selectors
and that banner visible before showing the metrics.

Narration for a verified pair:

"Both runs use the same model preparation, weather, period, EnergyPlus version,
and internal loads. Final totals come from EnergyPlus outputs and are
cross-checked against telemetry. Energy, peak, comfort, cost, carbon, and
reliability are reported together, including an unfavorable result."

If the representative-week pair is incomplete, show the honest incomplete
message and use:

"The representative-week pair is not yet publishable, so EcoLoop shows status
instead of a savings number. The verified one-day integration result remains
separate from the headline evaluation."

Caption:

`Matched real runs + official totals + telemetry cross-check.`

### 2:48-3:00 - Close

Screen transition: **Methodology** -> repository layout or submission manifest.

Narration:

"The same evidence exports to replay schedules, reports, checksums, and the
submission package. Moving from simulation to a real building requires a
commissioned BACnet or MQTT adapter behind the same capability and safety
boundary."

Caption:

`Simulation proof today; commissioned protocol adapters next.`

## No-narration caption script

Use one caption at each transition:

```text
00:00  Fixed schedules miss changing conditions.
00:08  EcoLoop adds local adaptive control with deterministic guardrails.
00:18  Every external dependency is discovered and version-checked.
00:38  EnergyPlus -> SQLite -> MCP -> local model -> validator -> actuator.
00:58  Real telemetry and control evidence, never fabricated production data.
01:35  Every selected, proposed, clamped, and applied action is auditable.
02:02  Timeouts, fallback, and circuit state remain visible.
02:22  Only compatible completed real runs can display a comparison.
02:48  Reproducible simulation evidence; commissioned building adapters next.
```

## Failure-recovery alternatives

If the live model times out:

1. Keep the timeout and fallback record on screen; do not restart in fake mode.
2. Allow deterministic fallback to continue if the circuit remains healthy.
3. If the live session must stop, use Ctrl+C so the demo terminates its child
   processes and records the interrupted run honestly.
4. Switch to the stable real-run visualization command above and keep the
   `REAL RUN REPLAY` banner visible.

If no completed real controlled run exists, record the doctor blocker and an
implementation tour only. Do not record a results demonstration.

## Schedule-driven EnergyPlus replay

To prove that a completed action sequence can drive EnergyPlus without local
inference:

```powershell
uv run python -m ecoloop replay <COMPLETED_REAL_AGENT_RUN_ID>
uv run python -m ecoloop dashboard
```

Select the newly created replay run in the dashboard. This executes a new
schedule-driven EnergyPlus case and is distinct from the visualization-only
`REAL RUN REPLAY` mode.
