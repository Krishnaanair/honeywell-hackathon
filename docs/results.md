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
The matched representative-week pair is complete and verified:

- baseline: `baseline-20260726T105124Z-4adce000`;
- agent: `agent-20260726T130255Z-65de6a32`;
- EnergyPlus: 26.1.0, evaluation period July 15-21;
- executed-run preparation-manifest SHA-256 (historical Windows CRLF bytes):
  `09558397d303ca52813cc7354fd34af8a9ef2d4cd100fe30732c555c89337230`;
- packaged canonical preparation-manifest SHA-256 (LF JSON with the source IDF
  hashed after LF line-ending normalization):
  `4ea4d121f7946e3e89f9b683c677c0c12f4c45633f4833c6571bdd2f316356f3`;
- weather SHA-256:
  `c7d4efcf93ba316a1d874352e743df5cf137ba5c0e3459eb2dc4b5442d5b7f5c`;
- comparison SHA-256:
  `7fcf6609940d43641b3f53642f3ba46572d716278611dc389571fdace14ec1da`.

| Metric | Baseline | Agent | Measured change |
| --- | ---: | ---: | ---: |
| Facility electricity | 1126.960 kWh | 1245.564 kWh | 10.524% higher |
| HVAC electricity | 461.785 kWh | 580.389 kWh | 25.684% higher |
| Peak electrical demand | 23.006 kW | 24.229 kW | 5.315% higher |
| Cost | 135.235 configured currency | 149.468 configured currency | 14.232 higher |
| Operational carbon | 788.872 kgCO2e | 871.895 kgCO2e | 83.023 kgCO2e higher |
| Occupied temperature compliance | 67.769% | 90.308% | 22.538 percentage points better |
| Occupied violation degree-hours | 80.627 | 22.360 | 72.267% lower |
| PMV compliance | 94.615% | 97.462% | 2.846 percentage points better |
| Mean PPD | 7.659% | 6.697% | 0.962 percentage points lower |

This configured week produced a comfort gain, not an energy saving. The agent
used 10.524% more facility electricity and increased peak demand by 5.315%.
Those unfavorable energy results are retained because the comparison passed all
publication gates.

The agent recorded 170 decisions and 788 MCP tool calls, with 0 inference
timeouts, 11 safe fallbacks, 13 invalid attempts, and 24 safety clamps. Mean
decision latency was 12.367 s and p95 latency was 14.672 s. EnergyPlus completed
with 0 warnings, 0 severe errors, and 0 fatal errors. The official facility
total and callback telemetry differed by only
`2.27e-13 kWh` (`1.83e-14%`), well inside the 2% tolerance. No other fuel was
reported.

The coordinator audit contains six deduplicated severe records across eight
occurrences: two safely rejected stale/cached requests during the run and four
post-completion shutdown-race records. Five post-completion proposals were
validator-accepted but never applied and therefore could not affect the physical
simulation or official totals. The shutdown guard was subsequently hardened;
these records are still disclosed, so the run is not described as error-free.

Schedule replay of this week was repaired and re-executed on 2026-07-26. The
repaired replay `replay-20260726T171617Z-5ffa3497` rebuilds the schedule from
the source run's verified physical actuator acknowledgements and immutable
model snapshot, applied all 170 recorded actions, produced 672 observations,
reported 0 EnergyPlus warnings, severe errors, or fatal errors, and reproduced
the source agent run digit-for-digit: facility electricity 1245.564416842928
kWh, HVAC 580.389416842928 kWh, peak 24.2293835189757 kW, occupied violation
9.6923%, and PMV compliance 97.4615% are all identical to the source values.
An earlier pre-repair replay (`replay-20260726T134158Z-8fc97abd`) differed by
0.290% because it applied actions one timestep early; it is retained as the
diagnostic that motivated the repair.
<!-- END VERIFIED_EVALUATION_BLOCK -->

The marked block above is the deterministic publication boundary. It is derived
from the verified `comparison.json` and two verified final-metrics exports.
Both run IDs and the comparison artifact SHA-256 are recorded in the result
narrative and presentation notes; values are not transcribed from terminal
output or dashboard screenshots.

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
