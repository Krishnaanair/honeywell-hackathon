# Limitations

- EcoLoop controls an EnergyPlus reference building, not a physical building.
  BACnet/MQTT integration is a future adapter and requires commissioning,
  authentication, command priority, and site safety review.
- Optional actuators remain unavailable until exact Runtime API discovery proves
  support.
- The candidate scorer is transparent bounded scoring, not full
  predictive-horizon MPC.
- Forecast and grid signals use configured local data; the runtime MCP surface
  has no unrestricted network access.
- PMV/PPD use configured summer-office assumptions (0.5 clo, 0.1 m/s air
  velocity, zero external work) rather than measured occupant inputs; different
  clothing or air movement changes comfort results.
- CO2 is unavailable unless contaminant simulation and the corresponding model
  objects are valid.
- The locally installed default EPW is Chicago TMY3. It is not committed to the
  repository and is not representative of Chennai.
- Savings vary by weather, period, baseline, equipment, and constraints. No
  result generalizes to another building without simulation and commissioning.
- Local model quality and latency depend on hardware and the selected
  tool-capable Ollama model.
- SQLite WAL is appropriate for a single local demonstration. Multiple physical
  sites would need a more explicit distributed messaging and identity design.
- A one-week evaluation is evidence for that configured period, not an annual
  energy guarantee.

Current executed dependency and integration status is maintained in
`docs/progress.md`; measured real results are maintained only in
`docs/results.md`.
