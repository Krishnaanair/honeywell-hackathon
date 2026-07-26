# Three-Minute Demo Script

Use only a completed verified baseline and a real controlled run. If the local
model or EnergyPlus preflight fails, use the verified run replay and say so.

## Shot list and narration

**0:00–0:20 — Problem and product**

Show the title slide. Say: “Fixed schedules cannot react to changing occupancy,
weather or demand. EcoLoop is a local, guardrailed supervisory controller that
closes the loop around an EnergyPlus digital twin.”

**0:20–0:45 — Architecture and health**

Show the architecture slide, then run `python -m ecoloop doctor`. Point to
EnergyPlus 26.1.0, PyEnergyPlus, local Ollama `qwen3:8b`, weather/model checks,
and the absence of cloud credentials.

**0:45–1:20 — Real observations**

Open the dashboard Live Operations tab. Show the LIVE SIMULATION badge, run ID,
simulated clock, zone temperatures, occupancy, demand and current setpoints.
Say that every value is persisted from Runtime API callbacks.

**1:20–1:50 — Model and real MCP tools**

Open Agent Decisions. Select a decision with the required state, constraints,
candidate and terminal MCP calls. State that FastMCP runs over private stdio and
that the local model chooses among evaluated candidates.

**1:50–2:15 — Safety and actuation**

Show proposed versus approved setpoints, clamps when present, and the physical
application acknowledgement. Explain that deterministic code owns deadband,
bounds, rate limit, expiry and final authorization.

**2:15–2:40 — Subsequent response and honest comparison**

Show the next observation and aligned baseline comparison. For the retained
week, explicitly say that comfort improved from 67.77% to 90.31% while
electricity increased 10.52%; this is a measured trade-off, not a saving claim.
Use a newer comparison only if the generated verified report says so.

**2:40–3:00 — Failure recovery**

Show a recorded timeout/fallback event or run the approved fault-injection test.
Point to the circuit breaker and EnergyPlus health panel. Finish with the exact
evidence download/report location.

## Commands

```powershell
.\.venv\Scripts\python.exe -m ecoloop doctor
.\.venv\Scripts\python.exe -m ecoloop demo --period smoke
```

For a stable verified visual replay:

```powershell
$env:ECOLOOP_DEMO_REPLAY = "1"
$env:ECOLOOP_DEMO_REPLAY_RUN_ID = "agent-20260726T151601Z-4f076a27"
.\.venv\Scripts\python.exe -m ecoloop dashboard
```

## Failure-recovery alternative

If EnergyPlus or Ollama becomes unavailable during recording, do not switch to
unlabelled demo values. Run the doctor, show the failed dependency and exact
fix, then launch **VERIFIED RUN REPLAY** from the retained completed real run.
State clearly that control already occurred and the UI is replaying its immutable
evidence.

## No-narration captions

1. “Real EnergyPlus 26.1.0 telemetry — no cloud API”
2. “Local qwen3:8b discovers and calls genuine MCP tools”
3. “Independent safety shield authorizes every setpoint”
4. “Physical actuator write recorded at the next control callback”
5. “Subsequent EnergyPlus state proves sense → act → sense”
6. “Verified week: comfort +22.54 points; electricity use +10.52%”
7. “Timeouts fall back safely; failures never become savings claims”
