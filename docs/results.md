# Results

## Publication gate

This document is generated or updated only from completed, compatible real
EnergyPlus runs. Fake, failed, incomplete, or mismatched cases are never used for
published savings.

## How to read the evidence

EcoLoop separates three evidence levels so a successful integration test is not
mistaken for a favorable energy result:

1. **Actuator proof** confirms that a requested schedule value reaches the active
   EnergyPlus simulation and is followed by later physical telemetry.
2. **Closed-loop smoke** confirms the complete observation-to-action path,
   including MCP, local inference, safety validation, actuation, and response.
3. **Representative comparison** evaluates energy, peak, comfort, reliability,
   cost, and carbon only for a matched completed baseline/agent pair.

The sections below use different run cohorts. Values from an actuator proof are
not combined with closed-loop or representative-week values.

## Verified one-day closed-loop smoke

The strict real acceptance test completed on 2026-07-26 using EnergyPlus 26.1.0,
the Chicago O'Hare TMY3 weather distributed with EnergyPlus, a 15-minute zone
timestep, FastMCP over stdio, Ollama, and `qwen3:8b`.

| Metric | Baseline | Agent | Change |
| --- | ---: | ---: | ---: |
| Facility electricity | 217.912262 kWh | 238.680567 kWh | **+9.531% consumption** |
| HVAC electricity | 89.437262 kWh | 110.205567 kWh | **+23.221% consumption** |
| Peak electrical demand | 22.273231 kW | 23.354306 kW | **+4.854% peak** |
| Occupied temperature violations | 30.385% | 9.615% | **-20.769 percentage points** |
| Violation degree-hours | 10.4195 | 4.7973 | **-53.958%** |
| PMV compliance | 98.462% | 97.308% | -1.154 percentage points |
| Mean PPD | 6.993% | 6.843% | -0.150 percentage points |

Run identities:

- Baseline: `baseline-20260726T121939Z-60ce216d`
- Agent: `agent-20260726T121949Z-ed9aabe0`

Both official EnergyPlus SQL electricity totals matched Runtime API telemetry to
far better than the configured 2% tolerance. Both cases completed with zero
EnergyPlus warnings, severe errors, and fatal errors. No other fuel was reported.

The agent recorded 25 decisions, 108 MCP tool calls, zero inference timeouts,
three fallbacks, two rejected model actions, and three safety clamps. Average
decision latency was 15.930 seconds and p95 latency was 16.834 seconds. Five
model-selected actions changed the live setpoints and were followed by later
telemetry at the applied setpoints with a changed zone temperature. The first
such transition was 17/29 C to 19/26 C at the occupied boundary.

This is a one-day integration smoke, not the headline energy evaluation. It
shows a comfort-versus-energy tradeoff: comfort violations improved materially,
but electricity and peak demand increased. EcoLoop therefore makes no savings
claim from this period.

## Verified real integration evidence

The one-day real Runtime API acceptance tests completed on 2026-07-26 with
EnergyPlus 26.1.0 and the configured Chicago TMY3 weather:

| Evidence run | Facility electricity | HVAC electricity | Peak demand | API observations | Warnings / severe / fatal |
| --- | ---: | ---: | ---: | ---: | ---: |
| `real-baseline-smoke` | 217.912262 kWh | 89.437262 kWh | 22.273231 kW | 96 | 1 / 0 / 0 |
| `real-fixed-override` | 212.077832 kWh | 83.602832 kWh | 20.818589 kW | 96 | 1 / 0 / 0 |

The baseline official SQL total was 217.91226189328628 kWh and callback
telemetry summed to 217.91226189328637 kWh. The fixed-override official SQL
total was 212.07783197929632 kWh and callback telemetry summed to
212.07783197929626 kWh. Both runs reported no other fuel.

The fixed-override test confirmed 21 C heating and 25 C cooling schedule
actuators inside the active simulation and recorded a subsequent changed
physical/energy response. It is an actuator proof, not an agent evaluation; the
difference between these two acceptance-test totals is therefore not published
as agent savings.

## Representative-week status

<!-- BEGIN VERIFIED_EVALUATION_BLOCK -->
The compatible representative-week baseline
`baseline-20260726T105124Z-4adce000` is complete and verified at:

- 1126.960494 kWh facility electricity;
- 461.785494 kWh HVAC electricity;
- 23.006488 kW peak demand;
- 32.231% occupied temperature violations;
- 80.6269 occupied violation degree-hours;
- 94.615% PMV compliance.

The paired local-model agent run is in progress. No representative-week savings
or comparison is published until that run completes and passes the same
official-output, telemetry, provenance, and compatibility checks.
<!-- END VERIFIED_EVALUATION_BLOCK -->

The marked block above is the deterministic publication boundary. Once the
agent run completes, replace it only from the verified `comparison.json` and the
two verified final-metrics exports produced by the commands below. Record both
run IDs and the comparison artifact SHA-256 in the result narrative and
presentation notes; do not transcribe values from terminal output or dashboard
screenshots.

Run:

```powershell
python -m ecoloop run baseline --period evaluation
python -m ecoloop run agent --period evaluation
python -m ecoloop compare BASELINE_RUN_ID AGENT_RUN_ID
```

After comparison, the report must include:

- run IDs, model/weather fingerprints, EnergyPlus version, and period;
- facility electricity and HVAC electricity;
- each other fuel independently;
- peak demand, cost, and operational carbon;
- occupied temperature violations/degree-hours;
- PMV compliance, mean PPD, and CO2 when available;
- decision/tool-call/latency/timeout/fallback/clamp/error counts;
- official-output versus telemetry cross-check.

CO2 is unavailable in the selected source model, so no IAQ performance claim is
made.
