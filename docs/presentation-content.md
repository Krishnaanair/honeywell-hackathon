# Presentation Content

The supplied hackathon template limits the final deck to six slides including
the title. The instruction slide is removed, and source slides 2-7 are
duplicated and edited in place.

## Slide 1 - EcoLoop Building Agents

- Problem statement: energy-aware, comfort-safe building supervisory control
- Theme: smart automation and sustainability
- Category: software
- Team/portal identifiers: use registered submission values
- Repository: `github.com/Krishnaanair/honeywell-hackathon`

## Slide 2 - Local intelligence, deterministic authority

- EnergyPlus telemetry becomes a compact building observation every 15 minutes.
- A local model selects among bounded, transparently scored candidates through
  real MCP tools.
- An independent validator clamps or rejects unsafe, stale, expired, or
  unsupported actions.
- The next EnergyPlus callback applies only the validated thermostat schedules.
- Novelty: flexible selection without opaque direct actuator authority.

## Slide 3 - Technical approach

- EnergyPlus 26.1 Runtime/Data Exchange API with fresh-state child processes.
- SQLite WAL as telemetry, action, decision, error, and artifact audit bus.
- Official MCP SDK/FastMCP over stdio; dynamic tool discovery.
- Ollama `qwen3:8b`, compact state, bounded rounds, timeout, and keep-alive.
- Pydantic safety, deterministic fallback/circuit breaker, official output
  parsing, replay schedule, Streamlit/Plotly dashboard.

## Slide 4 - Feasibility and viability

- All-electric official five-zone example keeps simulations fast and credible.
- Capability discovery prevents commands to missing actuators.
- Missing handles produce point dumps and near matches, never zero telemetry.
- Timeout -> last-safe interval -> deterministic fallback -> circuit breaker.
- Matched baseline and controlled inputs prevent inflated savings claims.
- Physical-building scaling requires commissioned BACnet/MQTT adapters and site
  safety review.

## Slide 5 - Artifacts and verified evidence

- Source, tests, doctor, model preparation, MCP Inspector instructions.
- Live/replay dashboard and complete decision trace.
- Baseline, controlled, replay models and action schedule.
- Metrics, comparison, reports, checksums, manifest, and source ZIP.
- Populate energy/comfort/latency results only from the final compatible real
  run pair. Otherwise state the exact blocked integration.

## Slide 6 - Research, limitations, and next step

- EnergyPlus 26.1 documentation and official example model.
- Model Context Protocol official Python SDK.
- Ollama tool-calling documentation.
- ASHRAE comfort/ventilation targets as configured project constraints.
- Limitation: period/weather/building-specific evidence.
- Next: representative-week validation, then commissioned protocol adapter.
