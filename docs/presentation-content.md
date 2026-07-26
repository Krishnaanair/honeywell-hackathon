# Presentation Content

The supplied hackathon template limits the final deck to six slides including
the title. The instruction slide is removed, and source slides 2-7 are
duplicated and edited in place. The deck remains text-led because the source
template contains no inherited chart, image, or diagram frame.

## Slide 1 - EcoLoop Building Agents

- Problem: static HVAC schedules.
- Solution: guardrailed local control.
- Theme: smart automation.
- Category: software.
- Team: EcoLoop.
- Repository: `github.com/Krishnaanair/honeywell-hackathon`.

## Slide 2 - Local choice, safe control

- The model proposes; the safety layer decides.
- Sense: 15-minute telemetry becomes compact zone and building state.
- Choose: `qwen3:8b` uses real MCP tools to select a scored candidate.
- Act: validation clamps or rejects; only safe setpoints reach the next
  EnergyPlus callback.

## Slide 3 - One auditable loop, end to end

- Control: EnergyPlus 26.1 -> SQLite WAL -> MCP stdio -> local Ollama -> safety
  validator -> thermostat schedules.
- Evidence: every proposal, clamp, action, and later response is durable;
  EnergyPlus outputs verify energy, peak, and comfort.

## Slide 4 - Safety fails closed

- Feasible now: official five-zone heat-pump model, discovered capabilities,
  and no cloud key.
- Failure-safe: stale or unsupported actions are rejected; timeout uses bounded
  fallback; repeated failures open the breaker.
- Honest scaling: matched inputs gate claims; BACnet/MQTT deployment requires
  commissioning and site safeties.

## Slide 5 - Evidence before claims

Until the representative-week pair is verified, the slide states:

<!-- BEGIN SLIDE_5_EVIDENCE -->
- Closed-loop smoke: real telemetry -> safe setpoint -> later physical response.
- Bundle ready: source, tests, dashboard, replay, audit, and reports.
- Representative-week results: **PENDING VERIFICATION**.
- Publish only after official-output and matched-run checks pass.
<!-- END SLIDE_5_EVIDENCE -->

The presentation build accepts an optional JSON file conforming to
`tmp/presentation/week-metrics.schema.json`. It will populate week metrics only
when the input declares a verified EnergyPlus 26.1 evaluation comparison and
the referenced comparison artifact matches its SHA-256 checksum.

## Slide 6 - Limits define the next step

- Foundation: official EnergyPlus 26.1 Runtime API, MCP Python SDK/FastMCP, and
  Ollama tool calling.
- Evidence boundary: Chicago TMY3, one reference heat-pump building, and
  configured PMV assumptions; CO2 is unavailable.
- Next: verify the matched week, then commission a BACnet/MQTT adapter with
  site-level safeties.

## Verified-results insertion contract

The accepted final deck is not produced by manually editing Slide 5. After the
evaluation agent run completes:

1. Generate the machine-readable comparison:

   ```powershell
   python -m ecoloop compare BASELINE_RUN_ID AGENT_RUN_ID --output-dir results
   ```

2. Create the bounded presentation input from that `comparison.json` and the two
   verified final-metrics exports.
3. Record the repository-relative comparison path and its SHA-256.
4. Build the accepted filename:

   ```powershell
   node tmp/presentation/build_ecoloop_draft.mjs `
     --metrics <verified-json> `
     --out presentation/ecoloop-submission.pptx
   ```

5. Re-run template fidelity, render, metadata, source-note, placeholder, and
   restricted-trace checks before PDF conversion.

The JSON maps only these fields into the inherited Slide 5 text frame:

- baseline and agent facility electricity kWh;
- baseline and agent HVAC electricity kWh;
- baseline and agent peak demand kW;
- baseline and agent occupied temperature-violation percent;
- baseline and agent PMV-compliance percent;
- agent decision, timeout-attempt, and fallback counts.

The builder calculates all relative or percentage-point changes. If the agent
uses more electricity or reaches a higher peak, the deck says `higher`; it does
not present the result as savings. Speaker notes must name both run IDs, the
comparison artifact, its SHA-256, and the methodology/results sources.

Values are never copied from a screenshot, terminal transcript, or manually
edited prose. If any gate fails, the pending block remains in the deck.
