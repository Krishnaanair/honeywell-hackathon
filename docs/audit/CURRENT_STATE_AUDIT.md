# Current-State Forensic Audit

Audit date: 2026-07-26  
Protected source revision: `0fc71b18c2c37fbb5ead3319a6bb41a85ff1d2e1`  
Rescue branch starting checkpoint: `c611b49`

This audit describes the implementation that existed at the protected revision.
It distinguishes executed evidence from source inspection and does not treat a
polished dashboard as proof of control.

## Executive finding

The primary implementation is a genuine EnergyPlus 26.1 Runtime API closed
loop. A local `qwen3:8b` model communicates with a real FastMCP stdio server,
selects an evaluated candidate, and reaches an independent safety validator.
The approved action is read and written by EnergyPlus callbacks, and subsequent
telemetry reports the changed schedule values and physical response.

The largest correctness defect was replay timing: accepted actions were exported
at their originating observation time even though EnergyPlus physically applied
them at the next begin-zone-timestep callback. The representative-week replay
therefore applied 169 of 170 actions 15 simulated minutes early. The largest
outcome limitation is equally important: the verified week improved occupied
comfort but used 10.5242% more facility electricity. EcoLoop cannot claim energy
savings from that experiment.

## Component disposition

| Classification | File or component | Current responsibility | What works | What does not work | Connected to real runtime | Evidence | Score risk | Required action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| KEEP | `src/ecoloop/doctor.py`, `energyplus/discovery.py` | Cross-platform dependency discovery | Finds EnergyPlus 26.1, PyEnergyPlus, schema/tools, weather, Ollama/model, and writable paths with fixes | Backend/frontend health are mostly local-file checks rather than long-running service probes | Yes | `python -m ecoloop doctor --json` returned ready on the audit host | Low | Retain and extend only for new smoke evidence |
| REFACTOR | `src/ecoloop/energyplus/model.py` | Schema-aware epJSON preparation | Uses installed 26.1 schema, official conversion tools, real office example, reference/actuated schedules, Fanger fields, and actuator map | Normal runs wrote period-specific output into tracked `models/generated`; manifest omitted dates and output hashes | Yes | Real preparation tests pass; rescue rule run changed three tracked IDFs | High | Prepare execution inputs in each run directory; fingerprint dates and artifacts |
| KEEP WITH MINOR REPAIR | `src/ecoloop/energyplus/runtime.py` | Fresh-state PyEnergyPlus execution, callbacks, handle discovery, sensing and actuation | Genuine Runtime/Data Exchange API, readiness and warmup guards, separate process, de-duplication, message/progress callbacks, actuator writes, state disposal on normal completion | Full action binding occurred after actuator writes; callback waits for decisions; exceptional construction/termination cleanup is incomplete | Yes | Real baseline, fixed override, rule, and closed-loop tests; DB sense-act-sense trace | High | Prevalidate before writes; document bounded callback handshake; add exceptional cleanup tests |
| REFACTOR | `src/ecoloop/db/` and `energyplus/store_adapter.py` | SQLite WAL audit/communication bus | Versioned migrations, run IDs, monotonic observations, immutable telemetry, proposals, validation, tool calls, errors, metrics and artifacts | `applied_actions` meant approved/queued; physical write existed only as a text message | Yes | 25 approved rows and 25 physical messages in fresh real smoke | High | Add structured physical actuator acknowledgements and replay from them |
| KEEP WITH MINOR REPAIR | `src/ecoloop/mcp/server.py`, `mcp/sqlite_service.py` | FastMCP tools over durable state | Official SDK server, typed allow-list, stdio transport, path confinement, dynamic tools/list and tools/call | Observation context omitted recent trends/previous action; aligned baseline setpoints were not passed to candidate generation; real forecast unavailable | Yes | MCP stdio tests and 99 successful calls in fresh real run | Medium | Enrich state; fix trend names; add honest bounded EPW forecast or keep unavailable label |
| KEEP | `src/ecoloop/agent/client.py`, `agent/loop.py`, `agent/ollama_host.py` | Local-model MCP tool loop | Starts MCP child, discovers schemas, converts tools for Ollama, enforces mandatory sequence, one corrective reprompt, timeout/retry/circuit breaker | Runtime result quality depends on candidate/scorer inputs; no hidden reasoning is stored | Yes | Real `qwen3:8b` smoke contains state, constraints, candidate, and terminal calls | Low | Retain; rerun after scorer-context repair |
| KEEP | `src/ecoloop/control/safety.py`, `constraints.py`, `fallback.py` | Deterministic authorization and fallback | Finite checks, ranges, deadband, rate limit, capabilities, freshness, expiry, generation, emergency protection, repair/clamp/reject/fallback | Runtime and MCP maximum-hold authorities could diverge | Yes | Focused safety, stale action, timeout, and fallback tests pass | Medium | Unify/persist effective runtime configuration |
| REFACTOR | `src/ecoloop/control/candidates.py` | Bounded candidates and transparent one-step scoring | Correctly labelled as heuristic, not MPC; exposes score components | Slope key mismatch made trend term zero; production observations contained no trends or previous action; week scorer over-prioritized comfort/tight setpoints | Yes | All week decision summaries empty for trends; verified week is energy-negative | High | Supply real context, correct keys, calibrate only with documented experiments |
| REFACTOR | `src/ecoloop/coordinator.py`, `demo.py` | Baseline/agent orchestration and asynchronous model worker | EnergyPlus runs in worker process and local model/MCP in coordinator; bounded timeouts and fallback | Reusable baseline checks were weaker than publication checks; demo printed a baseline that the child did not lock; runtime prep dirtied tracked files | Yes | Static audit plus incompatible fingerprints in retained week/rule runs | High | Lock parent ID; require exact non-null hashes and verified final metrics; stage inputs per run |
| KEEP WITH MINOR REPAIR | `src/ecoloop/evaluation.py`, `metrics.py`, `energyplus/results.py` | Official output parsing, cross-check, metrics and comparison | Final energy comes from EnergyPlus SQL/CSV; central tested formulas; fake/incomplete/fatal runs are refused; provenance hashes checked | Same period name did not prove the same finalized start/end clock | Yes | Fresh real telemetry/official difference `8.53e-14 kWh` | Medium | Require exact final simulation window and timestep provenance |
| REFACTOR | `src/ecoloop/energyplus/replay.py`, `reporting.py` | Action export and deterministic replay | Uses real accepted action values and runs without the model | Scheduled observation time rather than physical application time; some paths used mutable global model/map | Yes, but incorrect | Source week 1245.5644 kWh versus replay 1249.1801 kWh | Critical | Use structured acknowledgements and immutable per-run inputs |
| KEEP WITH MINOR REPAIR | `src/ecoloop/dashboard/` | Streamlit command center over SQLite | Real-only production queries, six useful tabs, explicit live/replay labels, 49 metrics and nine charts in AppTest | Default could select rule run; replay label accepted invalid run types/statuses; completed banner overclaimed verification; `--include-fake` did nothing | Yes | HTTP 200 and Streamlit AppTest without exceptions | Medium | Fail closed on labels, honor current run, prefer agent, remove dead option |
| KEEP | `tests/unit`, most `tests/integration` | Deterministic boundary tests | 228 non-real tests passed; strong MCP, safety, timeout, dashboard and metric coverage | No real replay-equivalence or exceptional-cleanup test; some tests encoded the wrong replay timestamp | Test boundary | Executed audit suites | Medium | Correct timestamp expectations and add acknowledgement/equivalence coverage |
| KEEP WITH MINOR REPAIR | `tests/real` | Dependency-aware EnergyPlus/Ollama gates | Proves baseline, fixed actuator effect, rule control, Ollama calls and full real closed loop | No public `smoke-energyplus` command and no replay-equivalence hard gate | Yes | Previously executed six dependency-aware tests | Medium | Add command/evidence JSON and rerun real suite |
| KEEP WITH MINOR REPAIR | `README.md`, `docs/` | Setup, architecture, policy, results, demo, limitations | Honest negative week result, exact commands, real run IDs, local-only runtime, no fabricated savings | New audit path documents were missing; minor button wording and evidence IDs drifted | N/A | Link/file review | Low | Add traceability, final test, evidence map and updated demo wording |
| REFACTOR | `src/ecoloop/submission.py`, existing bundle | Source/report/deck package | Clean checksummed bundle, theme-derived presentation, readable PDFs, no secrets or local machine paths | Packaged replay depends on ignored local SQLite data and has no compact import path | Partially | ZIP inventory and checksums | Medium | Package a bounded verified replay dataset/import path or state the limitation clearly |
| REMOVE | Dead dashboard fake inclusion option | Purported production switch | Nothing; production queries already correctly exclude fake rows | Flag had no effect and could imply fake production display | No | CLI/app source comparison | Low | Remove the option |
| MISSING | General HTTP API | Optional service boundary | Not required for the local single-machine proof because dashboard and MCP use SQLite | No FastAPI/WebSocket facade for remote clients | No | Repository search | Low | Keep as a documented extension, not last-minute infrastructure |

## Direct answers

1. **Is the EnergyPlus integration real or simulated?** Real on the default path.
   The explicit `--fake` plant is confined to tests/CI and carries `is_fake`.
2. **Is EnergyPlus controlled during an active run?** Yes. Schedule actuators are
   written in the next begin-zone-timestep callback.
3. **Is the runtime model open-source/self-hosted?** Yes. Retained evidence names
   local Ollama `qwen3:8b`; no cloud key is used.
4. **Does the model receive current building state?** Yes, through the MCP state
   tool and compact state hint. Trend and forecast enrichment needed repair.
5. **Does the model perform meaningful work?** Yes. It chooses the terminal tool
   path and one of the bounded scored candidates. Deterministic code remains the
   safety authority.
6. **Is MCP genuine?** Yes. The implementation uses FastMCP plus the official
   `ClientSession` and stdio transport, including `tools/list` and `tools/call`.
7. **Does a model-selected tool call influence control?** Yes. The persisted
   terminal `apply_control_action` creates the action consumed by EnergyPlus.
8. **Is there a deterministic safety layer?** Yes, independent of Ollama.
9. **Does the dashboard read real telemetry?** Yes, directly from the SQLite bus.
10. **Are baseline and agent results comparable?** The retained week passed the
    then-current provenance gate, but the gate needed exact finalized-window
    enforcement. The new rescue rule week is not comparable to that baseline.
11. **Are displayed savings recalculable?** Yes for verified compatible runs;
    the retained week is an energy increase, not a saving.
12. **Are claims unsupported?** No fabricated KPI was found. Replay-equivalence
    wording and generic “verified outputs” dashboard wording were too broad.
13. **What should be retained?** The Runtime API adapter, SQLite bus, real MCP
    boundary, local model host, safety validator, official metrics parser,
    dashboard component system, tests, documentation, reports and presentation.
14. **Largest architectural defect?** Physical actuation was not a first-class
    structured event, which caused replay timing to be derived incorrectly.

## Executed evidence inspected

- Fresh real baseline: `baseline-20260726T151534Z-df519672`
- Fresh real agent: `agent-20260726T151601Z-4f076a27`
- Retained representative-week baseline:
  `baseline-20260726T105124Z-4adce000`
- Retained representative-week agent:
  `agent-20260726T130255Z-65de6a32`
- Rescue diagnostic rule week:
  `rule-20260726T160009Z-e2de2c52` (not compatible with the retained baseline)

The fresh agent run contains 96 facility observations, 480 zone rows, 25
decisions, 99 MCP calls, 25 approved actions, 25 physical actuator messages,
and zero EnergyPlus warnings, severe errors, or fatal errors.
