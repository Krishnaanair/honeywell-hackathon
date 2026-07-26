# Limitations

## Demonstrated scope

EcoLoop controls an EnergyPlus reference building, not a physical building. The
real integration evidence proves the local simulation/control path; it does not
prove readiness to command installed HVAC equipment. A physical deployment needs
a commissioned BACnet or MQTT adapter, device identity, authentication, command
priority, point mapping, operator override, rollback, and site safety review.

Optional actuators remain unavailable until exact Runtime API discovery proves
support. The current mandatory action surface is limited to heating setpoint,
cooling setpoint, and hold duration.

## Model and environmental scope

- The locally installed default EPW is Chicago TMY3. It is not committed to the
  repository and is not representative of Chennai or another Indian climate.
- The selected five-zone heat-pump example is useful for reproducible system
  evidence, but it is not a calibrated model of a particular building.
- PMV/PPD use configured summer-office assumptions: 0.5 clo, 0.1 m/s air
  velocity, and zero external work. They are not measured occupant inputs.
- CO2 is unavailable in the selected model because a verified contaminant-
  simulation point is not exposed. No IAQ result is inferred from missing data.

## Control and runtime scope

- The candidate scorer is transparent bounded scoring, not predictive-horizon
  model-predictive control.
- Forecast and grid signals use configured local data; the runtime MCP surface
  has no unrestricted network access.
- Local model quality and latency depend on hardware and the selected
  tool-capable Ollama model. Deterministic validation and fallback reduce the
  control consequence of inference failures but do not improve model quality.
- SQLite WAL is appropriate for one local demonstration. Multiple physical sites
  need distributed messaging, durable site identity, clock strategy, and
  operational monitoring.

## Result transfer

Observed energy, peak, comfort, cost, and carbon outcomes depend on weather,
period, baseline, equipment, internal loads, constraints, tariff, and carbon
factors. A one-week comparison is evidence for that configured week, not an
annual guarantee. No result transfers to another building or climate without a
matched simulation and commissioning study.

Current executed dependency and integration status is maintained in
`docs/progress.md`; measured real results are maintained only in
`docs/results.md`.
