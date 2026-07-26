# Evidence Map

This map tells a judge where to verify each material claim. Run-directory
artifacts are local evidence and are not treated as version-controlled source.

| Claim | Source file or implementation | Runtime log / row | Test | Dashboard location | Evidence artifact |
| --- | --- | --- | --- | --- | --- |
| EnergyPlus 26.1.0 runs the building | `energyplus/discovery.py`, `energyplus/runtime.py` | `runs/<id>/energyplus/eplusout.err`, run version | Real baseline smoke | Run status header / Reliability | Run manifest and full EnergyPlus output |
| Telemetry is read during the active run | `EnergyPlusSession._on_observation` | `telemetry`, `zone_telemetry`, `observations` | Real baseline and closed-loop smoke | Live Operations / Comfort | `telemetry.csv`, database rows |
| Required handles are discovered only after API readiness | `HandleRegistry.resolve`, `EnergyPlusSession._ensure_handles` | API catalogue precedes first observation | Handle/runtime unit tests | Evidence / Reliability | `api_points.csv`, resolved handle map |
| A real actuator is written | `EnergyPlusSession._on_actuation`, `_apply` | Structured physical application and callback message | Fixed-override real smoke | Decisions / Reliability | Actuator map and smoke evidence JSON |
| Sense-act-sense is correlated | Coordinator, MCP service, physical application store | Observation 3083, generation 9, 08:30 application, observation 3084 | Real closed-loop smoke | Agent Decisions | `decisions.jsonl`, actions, acknowledgements, telemetry |
| Runtime model is local `qwen3:8b` | `agent/ollama_host.py` | Decision model and run metadata | Real Ollama and closed-loop smoke | Run header / Agent Decisions | Run manifest and decision export |
| MCP is genuine stdio protocol | `agent/client.py`, `mcp/server.py` | `tool_calls` sequence | MCP stdio integration | Agent Decisions | SQLite `tool_calls` rows in `runs/ecoloop.db` |
| The model performs operational work | `AgentHost.decide` | Model selects candidate/terminal tool | Agent loop plus real closed loop | Agent Decisions | Tool trace and selected action |
| Safety is independent of the model | `control/safety.py`, MCP apply tool | Proposed/applied values and clamp details | Safety suite | Agent Decisions | SQLite `applied_actions` rows (proposed vs applied, clamps), `action_schedule.csv` |
| Timeout/failure fallback works | `control/fallback.py`, coordinator, reliability | Fallback status, timeout count, circuit state | Timeout/circuit-breaker tests | Reliability and Errors | Decision/action logs |
| Baseline is conventional and fair | `energyplus/model.py`, `coordinator.py` | Parent ID and exact input fingerprints | Demo/coordinator/evaluation tests | Baseline vs Agent | Preparation manifest and fairness data |
| Final electricity comes from EnergyPlus output | `energyplus/results.py`, `evaluation.py` | Official total and callback cross-check | Evaluation tests and real smoke | Comparison / Methodology | `eplusout.sql`, `metrics.json` |
| No other fuel was used in reported runs | Result parser | Empty `other_fuels_kwh` in verified metrics | Real result tests | Methodology | Final metrics export |
| The representative week improved comfort | Central metrics/evaluation | Baseline 67.769%, agent 90.308% | Recalculation and comparison tests | Comparison / Comfort | Verified comparison JSON |
| The representative week did not save energy | Central metrics/evaluation | 1126.9605 vs 1245.5644 kWh | Independent KPI recalculation | Comparison | Verified comparison JSON/report |
| Dashboard uses persisted real data | `dashboard/queries.py`, `dashboard/app.py` | Real-only query path | Dashboard query/AppTest | All tabs | SQLite DB and exports |
| Replay is labelled distinctly | Dashboard replay eligibility helpers | Completed verified controlled run required | Dashboard tests | Header badge | Run/finalization records |
| Fake data cannot become a production KPI | Coordinator `is_fake`, evaluation and dashboard refusal | Fake rows excluded | Fake-boundary tests | Production selector | Explicit test fixtures only |
| Fatal EnergyPlus runs cannot publish savings | Runtime failure and evaluation publication gates | Failed run and preserved error | Error/finalization tests | Reliability | `eplusout.err`, failure report |
| Submission artifacts are checksummed | `reporting.py` package command | Manifest inventory | Packaging tests | Evidence panel / files | `submission/checksums.txt`, manifest |
| Repaired replay reproduces the source run | `energyplus/replay.py` (physical-acknowledgement schedule, immutable model snapshot) | Replay `replay-20260726T165649Z-6ff9f492` of `agent-20260726T151601Z-4f076a27`: identical official electricity 238.68056748751272 kWh, peak 23.35430606664108 kW, violation 9.615385% | Executed `python -m ecoloop replay` 2026-07-26 | Comparison / Reliability | `runs/replay-20260726T165649Z-6ff9f492/metrics.json` vs source `metrics.json` |
| One-day comparison is persisted and verified | `evaluation.py` `compare_and_write` | Comfort compliance 69.615% → 90.385% at +9.5306% electricity (not a saving) | Executed `python -m ecoloop compare` 2026-07-26 | Comparison | `runs/agent-20260726T151601Z-4f076a27/export/comparison.json` |
| Concurrent runs cannot corrupt scratch output | `runtime._isolated_request`, `_child_entry` per-run CWD | ReadVarsESO `readvars.audit` collision root-caused and fixed 2026-07-26 | 2 regression tests in `test_energyplus_runtime.py` | Reliability | `docs/audit/FINAL_TEST_REPORT.md` §4.2 |

## Concrete retained trace

Real run: `agent-20260726T151601Z-4f076a27`

1. Observation `3083`, simulated 08:15, reported 17/29 °C setpoints
   and mean zone temperature 26.69758 °C.
2. MCP sequence 32-35 called current state, constraints, candidate generation
   and `apply_control_action`.
3. Local model `qwen3:8b` selected generation 9 at 19/26 °C.
4. The deterministic validator accepted the bounded action.
5. The Runtime API wrote both schedule actuators at simulated 08:30.
6. Observation `3084` reported 19/26 °C and mean zone temperature
   26.05904 °C.

The trace proves integration and physical control. It is not used as an energy
savings claim.
