# Evaluation Methodology

## Objective

EcoLoop compares a credible conventional schedule against a guardrailed
supervisory controller using identical EnergyPlus inputs. The evaluation asks:

1. Does the controlled run reduce electricity, peak demand, cost, or operational
   carbon for the configured period?
2. Does it preserve occupied comfort and available IAQ constraints?
3. Does the complete protocol/control path remain reliable and auditable?

No metric is published until both real runs complete, use compatible inputs, and
pass output integrity checks.

## Building and weather

The base is the expanded EnergyPlus 26.1.0
`HVACTemplate-5ZoneUnitaryHeatPump.idf` example:

- five conditioned office zones;
- 50 scheduled occupants;
- five all-electric unitary air-to-air heat pumps;
- DX heating and cooling;
- electric supplemental heat;
- outdoor air and economizers;
- scheduled dual thermostat setpoints.

Provenance and checksums are in `models/base/SOURCE.md`.

The source People objects provide activity schedules but omit other inputs
required for Fanger PMV/PPD. Structured preparation adds explicit, documented
constant schedules for sedentary-office external work efficiency (0.0), summer
office clothing insulation (0.5 clo), and still-air velocity (0.1 m/s), selects
enclosure-averaged mean radiant temperature, and enables Fanger on all five
People objects. These are evaluation assumptions rather than measured occupant
properties; their values and the People-to-zone mapping are recorded in the
preparation manifest.

Weather is configured by EPW path. The reproducible locally installed default is
the Chicago TMY3 file distributed with EnergyPlus 26.1.0. It is not represented
as Indian weather. A Chennai/India EPW is accepted only with verified source,
redistribution terms, and checksum.

Tariff and operational-carbon factors are configured scenario inputs, not
EnergyPlus telemetry. The sample configuration uses `0.12` currency units/kWh
and `0.70` kg CO2e/kWh solely as disclosed defaults. A published comparison
records the configured values; site-specific cost or carbon claims require
replacing them with sourced local factors.

## Conventional-BMS baseline

Model preparation replaces the example's unusually wide thermostat values with
one explicit conventional office schedule used by both the fixed baseline and
the controlled run's unmodified reference:

- weekdays and design/custom days, 08:00-18:00: 20 C heating and 24 C cooling;
- all other weekday hours: 18 C heating setback and 28 C cooling setup;
- weekends and holidays: 18 C heating setback and 28 C cooling setup;
- HVAC fan availability remains fixed at the source schedule: weekdays
  07:00-21:00, off otherwise, with design-day operation retained;
- minimum outdoor-air availability remains fixed at the source schedule:
  weekdays 08:00-21:00, off otherwise, with design-day operation retained;
- occupancy and all other internal-load schedules remain unchanged from the
  version-matched example (50 people at peak, with fractional weekday
  occupancy).

The baseline thermostat schedule is copied into separate reference and actuated
schedule objects. The baseline thermostat points to the reference objects; the
controlled thermostat points to the actuated objects and reads the independent
reference values for comparison. The fixed schedule is inside the project
safety ranges and is not made intentionally poor to inflate savings.

## Controlled cases

- `rule`: deterministic cadence, candidate scoring, validator, and fallback
  without local-model calls. This isolates actuator/runtime behavior.
- `agent`: the full MCP/Ollama tool loop selecting among bounded evaluated
  candidates.
- `replay`: the exact applied action schedule from a completed controlled run,
  without inference.

Optional actuator fields remain disabled until the runtime discovers the exact
actuator and reports the capability.

The normal setpoint-change limit applies to ordinary decisions. On an inferred
occupancy-mode transition, an old setback may lie entirely outside the newly
active bounds (for example, 17/29 C becoming occupied 19-22/23-26 C), leaving no
mathematical intersection with the 1 C normal ramp. Only in that condition, the
validator moves to the nearest newly active boundary, records accepted
`SETPOINT_CLAMPED` issues, and resumes the normal ramp limit on the next
decision. This exception cannot move outside the active hard bounds.

## Matched-run requirements

Comparison requires exact agreement on:

- EnergyPlus version/build;
- base/generated model fingerprint;
- EPW fingerprint;
- run start/end;
- timestep;
- internal loads and schedules;
- sizing/run-period flags;
- evaluation exclusion policy.

The baseline and controlled simulations execute in separate processes.

## Evaluation periods

| Period | Default dates | Duration | Purpose |
| --- | --- | ---: | --- |
| Smoke | July 15 | 1 day | Fast integration and actuator proof |
| Demo | July 15-17 | 3 days | Recorded live/replay demonstration |
| Evaluation | July 15-21 | 7 days | Primary representative comparison |
| Hot/mild week | Configured separately | Optional | Robustness |
| Full month | Configured separately | Optional | Longer-period stability |

The configured period is always reported. A result is not selected solely
because it is favorable.

## Official output sources

EnergyPlus CSV or SQLite outputs provide final energy and demand totals. Runtime
API telemetry provides live display and an independent accumulation cross-check.
If the relative difference exceeds the configured tolerance (default 2%), the
comparison is marked failed and no saving percentage is published.

Every non-electric fuel is converted and reported independently. Facility
electricity is not labelled total energy when another fuel exists.

## Metric definitions

Let `B` be a completed baseline value and `A` the matching controlled value.

```text
energy_saving_percent = 100 * (B_kWh - A_kWh) / B_kWh
peak_reduction_percent = 100 * (B_peak_kW - A_peak_kW) / B_peak_kW
cost = sum(timestep_kWh * timestep_tariff)
carbon_kg = sum(timestep_kWh * timestep_carbon_kg_per_kWh)
```

Percent metrics are undefined when the baseline denominator is not positive.

Occupied temperature violation:

```text
violation = max(0, target_min - operative_temperature)
          + max(0, operative_temperature - target_max)
degree_hours = sum(violation_C * timestep_hours)
violation_percent = 100 * violating_occupied_timesteps / occupied_timesteps
```

PMV compliance uses occupied samples for which PMV is available:

```text
pmv_compliance_percent =
    100 * samples(abs(PMV) <= 0.7) / occupied_PMV_samples
```

PPD and CO2 are reported only when available. Missing optional data stays
missing; it is never converted to zero.

Latency p95 uses the nearest-rank/quantile implementation documented in the
metrics module. Reliability counts include decisions, tool calls, timeouts,
fallbacks, invalid actions, safety clamps, and EnergyPlus message severities.

## Baseline reference in observations

An agent observation may include the baseline value at the same simulated
timestamp only after a compatible completed baseline exists. Alignment uses
simulated timestamp and period identity, not row position or wall-clock time.

## Interpretation

Candidate scoring is transparent bounded scoring using recent slopes, weather,
occupancy, demand, comfort margin, tariff, carbon, and action-change penalty. It
is not described as a full model-predictive controller because it does not solve
a predictive-horizon optimization problem.
