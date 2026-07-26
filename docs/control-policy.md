# Control Policy

## Controlled variables

Mandatory capability:

- heating thermostat schedule setpoint;
- cooling thermostat schedule setpoint;
- hold duration.

Optional values are absent by default and enabled only after exact actuator
discovery:

- ventilation multiplier;
- lighting fraction;
- supply-air temperature;
- shading state.

The validator rejects any value for an unsupported capability.

## Default constraints

| State | Heating | Cooling | Minimum deadband | Maximum normal change |
| --- | ---: | ---: | ---: | ---: |
| Occupied | 19-22 C | 23-26 C | 2 C | 1 C |
| Unoccupied | 17-21 C | 24-29 C | 2 C | 2 C |

Occupied targets:

- operative temperature: 22-26 C;
- absolute PMV: at most 0.7;
- CO2: at most 1000 ppm when available.

Additional rules enforce finite values, freshness, expiry, a 120-minute maximum
hold, capability, monotonic generation, idempotency, demand emergency response,
freeze protection, overheat protection, and heating below cooling.

The change limit is for normal decisions. If a trusted setback schedule is
wholly outside the newly active occupancy bounds and no normal-ramp intersection
exists, one occupancy-transition action is pinned to the nearest active
boundary. The validator records the exception as `SETPOINT_CLAMPED`; all later
decisions return to the normal change limit.

## Cadence

- EnergyPlus zone timestep: 15 simulated minutes.
- Telemetry: every zone timestep.
- Normal model decision: every 60 simulated minutes.
- Maximum action hold: 120 simulated minutes.

An event can trigger an earlier decision when:

- occupied operative temperature approaches a limit;
- absolute PMV approaches 0.7;
- available CO2 approaches 1000 ppm;
- demand crosses its configured threshold;
- occupancy changes;
- outdoor temperature changes sharply;
- the previous action expires.

Level-based comfort, PMV, CO2, and demand triggers are edge-based: they fire when
the state enters risk and can fire again only after it leaves and re-enters.
Hourly cadence, occupancy change, outdoor shock, and action expiry remain
independent triggers. This prevents a persistent near-boundary state from
calling the model every 15 minutes.

## Candidate grid

Candidates are the bounded Cartesian combination of values near:

- current setpoints;
- baseline/reference setpoints;
- occupied/unoccupied recommended values.

Before scoring, invalid deadbands, ranges, unsupported fields, excessive ramp
changes, and overlong holds are removed.

Candidate generation uses the same ramp envelope as the validator. At the
narrow occupied-boundary transition described above, it can therefore return
the nearest active-boundary pair for evaluation instead of failing before the
model can make a bounded selection.

## Candidate scoring

Each candidate exposes components rather than only one opaque score:

```text
total =
  energy_or_demand_term
  + comfort_margin_term
  + occupancy_term
  + weather_and_forecast_term
  + tariff_term
  + carbon_term
  + action_change_penalty
  + safety_margin_penalty
```

Temperature slope and a simple fitted/online response coefficient may estimate
near-term direction. The scorer does not claim full MPC.

The current stability term is 0.15 score units per degree Celsius of setpoint
change. This replaces the original 1.5 coefficient after the hostile
representative-week review showed that the former penalty kept cooling at
23 C long after comfort recovery and dominated the estimated energy benefit of
relaxing the setpoint. The repaired coefficient is a documented policy
assumption; no new savings claim is made until a fresh compatible week is run.

The local model must choose an evaluated candidate or request fallback. It
cannot invent an out-of-grid actuator value and bypass validation.

## Fallback

1. A successful validated action is cached as last-known-safe.
2. On one timeout, that action may continue for one interval if still safe and
   unexpired after revalidation.
3. A deterministic rule controller then selects a bounded candidate based on
   occupancy, comfort margin, demand, and reference schedules.
4. Repeated failures open the circuit breaker and disable model control for one
   decision interval.
5. The next decision is a half-open model probe. A successful validated terminal
   action closes the breaker; another failure reopens it and applies another
   skipped fallback interval.

Every fallback records a reason code and source.

## Emergency overrides

Freeze/overheat and demand emergencies are evaluated independently of the model.
Comfort-protection overrides can move setpoints within hard bounds despite a
change penalty. Demand response cannot violate freeze/overheat protection,
deadband, capability, or finite-value rules.
