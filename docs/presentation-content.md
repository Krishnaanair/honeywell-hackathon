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

The verified representative-week slide states:

<!-- BEGIN SLIDE_5_EVIDENCE -->
- Matched week: 1126.960 kWh baseline versus 1245.564 kWh agent,
  **10.524% higher electricity**.
- Occupied-temperature compliance: 67.769% versus 90.308%,
  **22.538 percentage points better**.
- Peak demand: 23.006 kW versus 24.229 kW, **5.315% higher**.
- Reliability: 170 decisions, 788 MCP calls, 0 timeouts, 11 fallbacks; official
  EnergyPlus totals matched telemetry within numerical precision.
<!-- END SLIDE_5_EVIDENCE -->

The reviewed final deck is stored at
`presentation/ecoloop-submission.pptx`. Its Slide 5 source note identifies the
verified comparison artifact, the two run IDs, the calculation formulas, and
the SHA-256 of the exact repository blob used for the claims.

## Slide 6 - Limits define the next step

- Foundation: official EnergyPlus 26.1 Runtime API, MCP Python SDK/FastMCP, and
  Ollama tool calling.
- Evidence boundary: Chicago TMY3, one reference heat-pump building, and
  configured PMV assumptions; CO2 is unavailable.
- Next: strengthen the energy/demand penalty and tune on additional weather
  periods, then commission a BACnet/MQTT adapter with site-level safeties.

## Verified-results update contract

The temporary presentation-authoring workspace is intentionally excluded from
the repository. To update the included final deck after a new verified
evaluation:

1. Generate the machine-readable comparison:

   ```powershell
   python -m ecoloop compare BASELINE_RUN_ID AGENT_RUN_ID --output-dir results
   ```

2. Confirm that the comparison is verified for an EnergyPlus 26.1 evaluation
   period, has no verification failures, and references distinct baseline and
   agent run IDs.
3. Read only the fields listed below from `results/comparison.json` and the two
   verified final-metrics exports. Calculate changes from those values; never
   transcribe a value from a screenshot or terminal capture.
4. Compute the SHA-256 from the exact Git blob bytes that will be packaged, such
   as the bytes returned by `git show <revision>:results/comparison.json`.
   Avoid text-mode pipelines that can convert LF to CRLF before hashing.
5. Update only the inherited Slide 5 evidence text and its `[Sources]` speaker
   note in a standards-compliant presentation editor. Preserve the template
   theme, slide geometry, all other slide content, and neutral document
   metadata.
6. Render and inspect every slide, compare all non-target visible content with
   the prior reviewed deck, check for empty placeholders and clipping, verify
   every source note, and then export the PDF with
   `scripts/export_presentation_pdf.ps1`.

The bounded field contract for Slide 5 is:

- baseline and agent facility electricity kWh;
- baseline and agent HVAC electricity kWh;
- baseline and agent peak demand kW;
- baseline and agent occupied temperature-violation percent;
- baseline and agent PMV-compliance percent;
- agent decision, timeout-attempt, and fallback counts.

Calculate every relative or percentage-point change from those fields. If the
agent uses more electricity or reaches a higher peak, the deck says `higher`;
it does not present the result as savings. Speaker notes must name both run IDs,
the comparison artifact, its canonical-blob SHA-256, and the
methodology/results sources.

Values are never copied from a screenshot, terminal transcript, or manually
edited prose. If any gate fails, the pending block remains in the deck.
