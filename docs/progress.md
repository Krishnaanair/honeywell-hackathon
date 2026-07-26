# EcoLoop Implementation Progress

This file is an execution record. A `PASS` means the command was actually run and
succeeded. `BLOCKED` means the required external dependency was not available.

## 2026-07-26 - Repository and environment audit

> Historical environment snapshot. Later entries in this file supersede the
> blocker states recorded in this section.

### Completed

- Confirmed the project directory was empty and selected it as the repository
  root.
- Confirmed the directory was not yet a Git repository.
- Inspected the Windows platform, Python registrations, executable path,
  EnergyPlus common installation paths, Ollama installation/API, document
  renderers, and presentation template.
- Created `docs/implementation-plan.md`.
- Created durable repository instructions in `AGENTS.md`.
- Created this progress log.

### Commands run

```text
Get-ChildItem -Force
git status --short --branch
python --version
py -0p
Get-Command <required toolchain executables>
where.exe python
where.exe py
where.exe energyplus
where.exe ollama
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

### Audit results

| Check | Result |
| --- | --- |
| Operating system | Windows 11 Pro 64-bit, build 10.0.26200 |
| Python 3.11 | **BLOCKED** - only CPython 3.14.3 is registered |
| `uv` | **BLOCKED** - not installed |
| Git | PASS - 2.51.2 |
| Node.js | PASS - 24.11.0 |
| EnergyPlus executable | **BLOCKED** - not on PATH or common install paths |
| EnergyPlus 26.1.0 | **BLOCKED** - installation not found |
| `pyenergyplus` | **BLOCKED** pending EnergyPlus installation |
| `ExpandObjects` | **BLOCKED** pending EnergyPlus installation |
| `ENERGYPLUS_HOME` | Not set |
| Ollama executable | PASS - installed under the user profile |
| Ollama API | **BLOCKED** - `127.0.0.1:11434` unreachable |
| Configured Ollama model | **BLOCKED** - API unavailable; no env override |
| Presentation template | Not provided; fallback deck required |
| LibreOffice/soffice | **BLOCKED** - not installed |
| Poppler (`pdftoppm`, `pdfinfo`) | **BLOCKED** - not installed |
| Make | Not installed on this Windows host; Python/PowerShell commands remain usable |

### Test results

No implementation tests existed at audit time.

### Unresolved blockers

- Install Python 3.11 before producing the authoritative lock file and supported
  runtime validation.
- Install EnergyPlus 26.1.0 to select and copy a version-matched example, inspect
  the schema, and execute all real EnergyPlus acceptance tests.
- Start Ollama and pull `qwen3:8b` to execute tool-call and closed-loop smoke
  tests.
- A system PPTX/PDF renderer is absent. The project will use verified Python PDF
  generation; PowerPoint PDF export remains conditional on an available renderer.

No fake telemetry or metric has been substituted for any blocked real check.

## 2026-07-26 - Local toolchain and implementation validation

### Completed

- Installed CPython 3.11.9 and created the project `.venv`.
- Installed `uv` 0.11.32 and generated `uv.lock`.
- Installed the official EnergyPlus 26.1.0 Windows x86-64 distribution under
  `%USERPROFILE%\EnergyPlusV26-1-0`.
- Verified the EnergyPlus release archive SHA-256 as
  `0bb6932d277eed62f996b625f37c533b8c35f9af0c53710d961d8442fc4e70b3`.
- Selected the official 26.1.0 `HVACTemplate-5ZoneUnitaryHeatPump.idf` example
  and preserved source/licence records under `models/base/`.
- Configured the EnergyPlus-distribution Chicago TMY3 EPW as a local,
  git-ignored default. Chennai weather was not substituted because no verified
  redistributable EPW was available in the installed distribution.
- Started the local Ollama API and installed `qwen3:8b`.
- Copied the supplied seven-slide source template (one instruction slide plus
  six submission slides) to `presentation/template.pptx` and completed the
  layout audit.
- Implemented the versioned SQLite WAL store, typed schemas, control
  constraints, independent validator, bounded candidate scorer, fallback and
  cadence logic.
- Implemented all runtime and diagnostic FastMCP tools, a genuine stdio MCP
  client, local Ollama tool loop, mandatory tool-sequence enforcement, bounded
  retry, cache, fallback, circuit breaker, and decision/tool audit records.
- Implemented the Streamlit dashboard query layer and six required views.
- Implemented honest run export and source/PDF submission packaging primitives.
- Added Poppler for PDF rendering and page-level visual verification.

### Commands run

```text
EnergyPlus-26.1.0-6f2e40d102-Windows-x86_64\energyplus.exe --version
ollama pull qwen3:8b
uv lock
uv sync --extra dev
.venv\Scripts\python.exe -m ecoloop doctor
.venv\Scripts\python.exe -m ecoloop prepare-model --period smoke
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q tests\real\test_ollama_smoke.py
.venv\Scripts\python.exe -m pytest tests\integration\test_reporting.py tests\integration\test_dashboard_queries.py -q
```

### Test results

| Check | Result |
| --- | --- |
| Doctor | PASS - READY; all required checks passed |
| Model preparation | PASS - generated artifacts and repeatability check |
| Core deterministic unit suite | PASS - 65 tests |
| MCP/Ollama unit and fake integration suite | PASS - 40 tests |
| Full available non-real suite checkpoint | PASS - 153 tests |
| Real Ollama-to-MCP stdio smoke | PASS - 1 test; 14.01 seconds |
| Dashboard/reporting/CLI/demo integration tests | PASS - 11 focused tests |
| Ruff | PASS on all currently implemented source |
| mypy | PASS on 51 source files |
| Real EnergyPlus baseline | PASS - 96 observations; exit 0 |
| Real fixed actuator proof | PASS - 96 observations; exit 0; two setpoint overrides applied |
| Real EnergyPlus + MCP + Ollama loop | IN PROGRESS |

The real Ollama smoke uses an explicitly fake test plant to prove local-model
tool calling and MCP transport. It is not an EnergyPlus or savings result.

### Real EnergyPlus evidence

The real tests below use official EnergyPlus SQL output for totals and cross-check
the Runtime API accumulator. The fixed override is an actuator-integration proof,
not an agent-savings claim.

| Case | Facility electricity | HVAC electricity | Peak demand | API cross-check | Errors |
| --- | ---: | ---: | ---: | ---: | --- |
| `real-baseline-smoke` | 217.9123 kWh | 89.4373 kWh | 22.2732 kW | 217.9123 kWh | 1 warning; 0 severe/fatal |
| `real-fixed-override` | 212.0778 kWh | 83.6028 kWh | 20.8186 kW | 212.0778 kWh | 1 warning; 0 severe/fatal |

The override case applied 21 C heating and 25 C cooling setpoints inside the
active simulation. Its energy difference is not reported as product savings
because the local-model supervisory loop did not produce that action.

### Persistent public-CLI run

Command:

```text
.venv\Scripts\python.exe -m ecoloop run baseline --period smoke
```

Result: PASS. Run `baseline-20260726T101938Z-b7983d5f` completed with 96
observations, no applied actions, no severe/fatal messages, and a successful
official-output/API cross-check. Official facility electricity was 217.9123 kWh,
HVAC electricity was 89.4373 kWh, and peak demand was 22.2732 kW. This run
exposed a preparation limitation: PMV/PPD remained unavailable because the
source People objects lacked all Fanger prerequisite schedules. The structured
model patcher was subsequently extended with documented constant schedules for
work efficiency 0.0, summer office clothing insulation 0.5 clo, and air
velocity 0.1 m/s, plus enclosure-averaged mean radiant temperature. All five
People objects now have Fanger enabled and mapped back to their zones. This
earlier baseline will therefore not be used for the final PMV comparison.

### Real PMV-enabled rule-controller handshake

Command:

```text
.venv\Scripts\pytest.exe -q tests\real\test_energyplus_smoke.py::test_real_one_day_rule_controller_closes_action_handshake -m real_energyplus
```

Result: PASS in 28.55 seconds. Run `real-rule-smoke` recorded 96 observations,
480 zone rows, 480 PMV values, 480 PPD values, 26 proposals, 26 applied
deterministic-fallback actions, 26 successful bounded handshakes, and zero
decision timeouts or stored errors. Official EnergyPlus SQL reported 224.2746
kWh facility electricity, 95.7996 kWh HVAC electricity, and 23.6791 kW peak
demand; callback telemetry summed to 224.2746 kWh. EnergyPlus reported zero
warnings, severe errors, and fatal errors.

At the unoccupied-to-occupied transition, the prior 17/29 C setback did not
intersect the occupied 19-22/23-26 C bounds under a normal 1 C ramp. The safety
validator now applies a narrow, audited exception only for this inferred
occupancy-boundary transition: it moved the request to 19/26 C, stored two
`SETPOINT_CLAMPED` issues, and resumed normal ramp enforcement afterward. This
rule run used more electricity than the baseline and is integration evidence,
not an energy-savings claim.

### Remaining validation and release work

- Finish the runtime/coordinator event-handshake synchronization and rerun the
  real rule-controller smoke without unnecessary wall-clock waits.
- Complete the real EnergyPlus + MCP + Ollama closed-loop smoke.
- Run a paired representative-week baseline and controlled evaluation.
- Parse and cross-check official totals, then publish only the measured paired
  comparison.
- Populate the final dashboard evidence, results documentation, and supplied
  presentation template from verified metrics.
- Render and visually inspect the completed presentation and submission PDFs.
- Run final full test, lint, type, secret, junk, and misleading-claim audits.
- Commit, create the public GitHub repository, and push the reviewed source.

## 2026-07-26 - Release-readiness audit

### Completed

- Audited README, required documentation, CI, licences, ignored files, MCP
  boundaries, result claims, generated metadata, and submission contents.
- Corrected the baseline methodology to record the exact 20/24 C occupied and
  18/28 C setback schedule plus fixed HVAC and outdoor-air availability.
- Documented tariff and carbon factors as configured scenario inputs rather than
  telemetry.
- Corrected EnergyPlus example-model and weather redistribution notices and
  expanded the direct-dependency licence inventory.
- Hardened the source ZIP to use an explicit release allow-list, exclude runtime
  outputs, weather, installers, model weights, credentials, symlinks, caches,
  and oversized files, and validate required contents.
- Added stale-submission cleanup and export support for verified comparison
  JSON/CSV artifacts.
- Extended ignored secret-file patterns and added focused package regression
  tests.
- Extended genuine FastMCP stdio integration coverage to discover and invoke all
  15 runtime/diagnostic tools, persist the complete test audit trace, and prove
  diagnostic path escape is rejected through the protocol.
- Added a dedicated dependency-aware `real_closed_loop` smoke acceptance test
  for the complete baseline, MCP stdio, Ollama, validated actuator, physical
  response, official-output verification, and comparison-export evidence chain.
  The expensive test was collected but intentionally not executed during this
  audit.
- Verified that generated model provenance is repository-relative and contains
  no host-specific installation path.

### Commands and results

```text
.venv\Scripts\python.exe -m pytest -q tests\integration\test_reporting.py
.venv\Scripts\python.exe -m pytest -q tests\integration\test_mcp_stdio.py tests\unit\test_mcp_tools.py
.venv\Scripts\python.exe -m pytest --collect-only -q -m real_closed_loop
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest -q -m "not real_energyplus and not real_ollama and not real_closed_loop"
```

| Check | Result |
| --- | --- |
| Reporting/package tests | PASS - 4 tests |
| MCP unit + genuine stdio tool coverage | PASS - 24 tests; all 15 tools invoked over stdio |
| Real closed-loop acceptance test | COLLECTED - 1 test; execution remains pending |
| Ruff lint | PASS |
| Strict mypy | PASS - 51 source files |
| Non-real CI suite | PASS - 165 tests; 5 real tests deselected |
| Local documentation links | PASS - no broken relative links |
| Sensitive-value scan | PASS - no credential or private-key value found |
| Release-content provenance scan | PASS |
| Ruff formatting | PENDING - final formatting is required after active EnergyPlus edits settle |

The existing ignored submission artifacts predate the package hardening and must
be regenerated after the verified controlled comparison and completed
presentation are available.

## 2026-07-26 - Real closed-loop acceptance and incremental release

### Completed

- Created and pushed the public repository at
  `https://github.com/Krishnaanair/honeywell-hackathon`.
- Split the initial publication into separate foundation, runtime, automated
  verification, controller-recovery, portability, and demo-template commits.
- Completed a real one-day EnergyPlus + MCP stdio + Ollama run. The first
  acceptance attempt correctly failed because model-selected actions retained
  the already-active setpoints.
- Kept the acceptance assertion unchanged and corrected the product:
  - candidate generation now shares the safety validator's guarded
    occupancy-transition envelope;
  - the host exposes only tools that can advance the required protocol stage;
  - repeated tool-sequence restarts are rejected with a precise correction;
  - the circuit breaker skips one interval and permits a half-open recovery
    probe;
  - model-requested safe abstention is not counted as a model failure;
  - per-decision timeout attempts are persisted through migration 002;
  - generic last-safe reuse is no longer mislabeled as a timeout.
- Reran the strict real acceptance without weakening it. The run passed with
  model-selected changed setpoints and a later physical temperature response.
- Started the paired representative-week agent run against the already verified
  evaluation baseline.

### Commands and results

```text
.venv\Scripts\pytest.exe -q tests\real\test_closed_loop_smoke.py -m real_closed_loop
.venv\Scripts\pytest.exe -q tests\real\test_ollama_smoke.py -m real_ollama
.venv\Scripts\pytest.exe -q -m "not real_energyplus and not real_ollama and not real_closed_loop"
.venv\Scripts\ruff.exe check src tests
.venv\Scripts\ruff.exe format --check src tests
.venv\Scripts\mypy.exe src
.venv\Scripts\python.exe -m ecoloop run agent --period evaluation
git push origin main
```

| Check | Result |
| --- | --- |
| Strict real closed-loop acceptance | PASS - 1 test in 438.62 seconds |
| Real Ollama-to-MCP smoke after protocol staging | PASS - 1 test in 13.53 seconds |
| Full non-real suite | PASS - 183 tests; 6 real tests deselected |
| Ruff lint and format | PASS |
| Strict mypy | PASS - 51 source files |
| GitHub publication | PASS - incremental commits pushed to `main` |
| Representative-week agent | IN PROGRESS |

### Passing real closed-loop evidence

| Case | Run ID | Facility electricity | HVAC electricity | Peak demand |
| --- | --- | ---: | ---: | ---: |
| Baseline | `baseline-20260726T121939Z-60ce216d` | 217.912262 kWh | 89.437262 kWh | 22.273231 kW |
| Agent | `agent-20260726T121949Z-ed9aabe0` | 238.680567 kWh | 110.205567 kWh | 23.354306 kW |

The one-day agent used 9.531% more facility electricity and reached a 4.854%
higher peak. Occupied temperature violations improved from 30.385% to 9.615%,
and violation degree-hours fell from 10.4195 to 4.7973. This is honest
integration and comfort evidence, not an energy-savings claim.

The agent recorded 25 decisions and 108 MCP calls. Five model-selected actions
changed the live setpoints and were followed by later telemetry at those
setpoints with a changed zone temperature. Official EnergyPlus SQL and callback
telemetry matched within numerical precision. EnergyPlus reported zero warnings,
severe errors, and fatal errors.

### Remaining release work

- Complete and verify the representative-week controlled run.
- Generate its comparison, replay, exports, and small reviewed result bundle.
- Populate and visually verify the supplied presentation template from the
  representative-week metrics.
- Regenerate and inspect the final PDFs, source ZIP, checksums, and manifest.
- Run the final full quality, security, provenance, and archive-content audits.
- Record the three-minute video and add registered team/portal identifiers.

## 2026-07-26 - Cached-action generation and invalid-attempt accounting

### Completed

- Changed cached-action application to use the authoritative
  `next_action_generation` returned by the constraint tool, with state-derived
  generation retained only as a compatibility fallback.
- Added a regression that starts after generation 7, omits generation history
  from the state payload, and proves cache reuse applies generation 8 without
  invoking the local model.
- Updated final audit metrics to count invalid apply attempts rejected at the
  MCP input-schema boundary as well as validator-rejected proposals.
- Deduplicated a validator rejection when the same attempt is also represented
  by an `invalid_action` decision outcome.
- Left the active representative-week process and run database untouched.

### Commands and results

```text
.venv\Scripts\python.exe -m pytest tests/integration/test_agent_loop.py::test_cached_action_uses_authoritative_advanced_generation tests/integration/test_evaluation.py::test_invalid_action_metric_includes_pre_schema_rejections_once -q
.venv\Scripts\python.exe -m pytest tests/integration/test_agent_loop.py tests/integration/test_evaluation.py -q
.venv\Scripts\python.exe -m ruff check src/ecoloop/agent/loop.py src/ecoloop/evaluation.py tests/integration/test_agent_loop.py tests/integration/test_evaluation.py
.venv\Scripts\python.exe -m ruff format --check src/ecoloop/agent/loop.py src/ecoloop/evaluation.py tests/integration/test_agent_loop.py tests/integration/test_evaluation.py
.venv\Scripts\python.exe -m mypy src/ecoloop/agent/loop.py src/ecoloop/evaluation.py
git diff --check
```

| Check | Result |
| --- | --- |
| New focused regressions | PASS - 2 tests |
| Full changed integration files | PASS - 23 tests |
| Ruff lint | PASS |
| Ruff format | PASS - 4 files already formatted |
| Mypy | PASS - 2 changed source files |
| Diff whitespace check | PASS; only existing line-ending notices |

## 2026-07-26 - Source-package selection hardening

### Completed

- Replaced broad repository filesystem traversal with Git-index selection using
  fixed, non-shell commands when packaging from a checkout.
- Added an exact allowlist for the final presentation and ten small result
  exports under `results/`.
- Added a restricted fallback for unpacked source trees outside a Git checkout;
  checkout discovery or index failures now stop packaging.
- Added shared path and content validation for traversal, Git/filesystem
  symlinks, credentials, user-home paths, raw EnergyPlus outputs, weather
  assets, model weights, installers, caches, archives, unsupported
  presentations, and files larger than 20 MiB.
- Normalized source-ZIP order, timestamps, compression, and permissions.
- Added deterministic Windows/Linux-compatible tests for Git selection,
  fallback selection, generated-artifact inclusion, sensitive content,
  presentation metadata, symlinks, oversized files, and Git failure handling.

### Commands and results

```text
.venv\Scripts\python.exe -m pytest -q tests\integration\test_reporting.py
.venv\Scripts\python.exe -m pytest -q tests\integration\test_reporting.py tests\integration\test_cli.py
.venv\Scripts\ruff.exe check src\ecoloop\reporting.py tests\integration\test_reporting.py
.venv\Scripts\ruff.exe format --check src\ecoloop\reporting.py tests\integration\test_reporting.py
.venv\Scripts\mypy.exe src\ecoloop\reporting.py
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\mypy.exe src
```

| Check | Result |
| --- | --- |
| Reporting/package tests | PASS - 14 tests |
| Reporting + CLI tests | PASS - 17 tests |
| Scoped Ruff lint and format | PASS |
| Scoped strict mypy | PASS |
| Repository-wide Ruff format at this checkpoint | PASS - 105 files |

This checkpoint was superseded by the final evaluation, visual QA, and release
audit recorded below.

## 2026-07-26 - Verified representative-week evaluation

### Completed

- Completed the same-model, same-weather, seven-day baseline and controlled
  EnergyPlus 26.1.0 runs at a 15-minute zone timestep.
- Parsed official EnergyPlus output totals and independently cross-checked the
  callback telemetry accumulator. The controlled-run difference was
  `2.27e-13 kWh`, within the configured 2% tolerance.
- Exported the action schedule, actuator map, API-point catalogue, decisions,
  telemetry, metrics, and comparison artifacts from the completed run.
- Generated and executed a schedule-driven replay without local-model
  inference. It reproduced all 170 control actions and finished 0.290% above
  the live controlled-run electricity total; replay is therefore documented as
  sequence reproducibility rather than bit-identical simulation.
- Published the measured tradeoff without a positive savings claim: comfort
  improved materially, while electricity, HVAC energy, peak demand, cost, and
  operational carbon increased.

### Representative-week evidence

| Metric | Baseline | Controlled | Change |
| --- | ---: | ---: | ---: |
| Run ID | `baseline-20260726T105124Z-4adce000` | `agent-20260726T130255Z-65de6a32` | - |
| Facility electricity | 1126.960 kWh | 1245.564 kWh | 10.524% higher |
| HVAC electricity | 461.785 kWh | 580.389 kWh | 25.684% higher |
| Peak electrical demand | 23.006 kW | 24.229 kW | 5.315% higher |
| Occupied comfort compliance | 67.769% | 90.308% | +22.538 percentage points |
| Occupied violation degree-hours | 80.627 | 22.360 | 72.267% lower |
| PMV compliance | 94.615% | 97.462% | +2.846 percentage points |
| Mean PPD | 7.659% | 6.697% | 0.962 percentage points lower |
| Configured cost | 135.235 | 149.468 | 14.232 higher |
| Operational carbon | 788.872 kg | 871.895 kg | 83.023 kg higher |

The controlled week recorded 170 decisions, 788 MCP calls, zero timeouts, 11
fallbacks, 13 invalid attempts, 24 safety clamps, 12.367-second average decision
latency, and 14.672-second p95 latency. EnergyPlus recorded zero warnings, zero
severe errors, and zero fatal errors.

Six deduplicated coordinator diagnostic records representing eight occurrences
remain in the week database. Two were stale or cached in-run attempts and four
were post-completion attempt shapes from the then-running coordinator. None
reached the EnergyPlus actuators. The shutdown race that admitted those audit
records was fixed and is covered by the later strict real acceptance below.

The replay run is `replay-20260726T134158Z-8fc97abd`. It completed 672
observations and applied the 170 recorded schedule actions with zero inference
calls and zero EnergyPlus warnings, severe errors, or fatal errors.

## 2026-07-26 - Dashboard and visual acceptance

### Completed

- Audited all six dashboard views at 1440 x 1000 and 390 x 844 viewport sizes.
- Reflowed live KPIs into responsive groups and replaced dense side-by-side
  plots with full-width charts so values, legends, and titles remain legible.
- Made the verified controlled week the default comparison instead of the
  replay run.
- Separated the latest proposal from the latest physically applied action and
  added an explicit application-status flag.
- Separated EnergyPlus diagnostics from controller audit records and sourced
  final reliability counts from the verified metrics.
- Corrected mobile navigation, tab styling, labels, tags, and refresh controls.
- Cycled every page at desktop and mobile sizes with no page errors, console
  errors, clipped cards, document-width overflow, or unintended overlap.

### Focused validation

```text
.venv\Scripts\python.exe -m pytest tests/integration/test_dashboard_queries.py tests/unit/test_dashboard.py -q
.venv\Scripts\ruff.exe check src/ecoloop/dashboard
.venv\Scripts\ruff.exe format --check src/ecoloop/dashboard
.venv\Scripts\mypy.exe src/ecoloop/dashboard
```

| Check | Result |
| --- | --- |
| Dashboard query and presentation tests | PASS - 6 tests |
| Ruff lint and format | PASS |
| Strict mypy | PASS |
| Desktop visual QA | PASS - 6 views |
| Mobile visual QA | PASS - 6 views |

## 2026-07-26 - Presentation and PDF acceptance

### Completed

- Completed `presentation/ecoloop-submission.pptx` from the supplied
  `IDEA_Presentation_Format.pptx` theme and dimensions.
- Populated the results slide exclusively from the verified representative
  week, including the higher electricity result and improved comfort result.
- Removed placeholder content and checked all slides for overlap, clipping,
  comments, prompts, local paths, and unsupported claims.
- Added sources to all six slides and neutral document metadata.
- Rendered and visually inspected every slide with both the artifact renderer
  and the installed presentation application.
- Improved the Markdown-to-PDF renderer so bold, code spans, links, publication
  boundaries, identifiers, and metric tables render cleanly.
- Regenerated and inspected all 15 pages across the results, architecture, and
  demo-script PDFs.

### Validation

| Check | Result |
| --- | --- |
| Presentation slides inspected | PASS - 6 of 6 |
| Slide source notes | PASS - 6 of 6 |
| Placeholder, overlap, and clipping audit | PASS |
| Results report | PASS - 4 pages |
| Architecture report | PASS - 6 pages |
| Demo-script report | PASS - 5 pages |
| Focused PDF-rendering regressions | PASS - 18 tests |

## 2026-07-26 - Latest-code strict real closed-loop acceptance

### Completed

- Executed the strict one-day EnergyPlus + stdio MCP + Ollama acceptance twice
  after the shutdown, local-transport, action-generation, and metric-accounting
  fixes. Both independent executions passed.
- Retained the second execution's database and full EnergyPlus output tree for
  an additional read-only audit.
- Proved a complete state -> constraints -> candidate generation -> validated
  application sequence over the MCP protocol.
- Proved physical effect: observation 129 changed the active schedules from
  17/29 C to 19/26 C for 60 minutes; observation 130 reported 19/26 C and the
  mean zone temperature changed from 26.69758 C to 26.05904 C.
- Verified that no proposals, applications, decisions, tool calls, fallbacks,
  errors, severe coordinator records, or orphan proposals were created after
  the terminal run transition.

### Commands and results

```text
$env:ENERGYPLUS_HOME='<ENERGYPLUS_INSTALL>'
$env:OLLAMA_MODEL='qwen3:8b'
.venv\Scripts\python.exe -m pytest tests/real/test_closed_loop_smoke.py::test_real_energyplus_mcp_ollama_closed_loop_smoke -vv -s --durations=1 -o tmp_path_retention_policy=all --basetemp '<RETAINED_TEMP_ROOT>'
```

| Check | Result |
| --- | --- |
| Strict real acceptance | PASS - 1 test in 554.79 seconds |
| Baseline run | `baseline-20260726T140958Z-3f8243d4` |
| Controlled run | `agent-20260726T141008Z-2b4ab89a` |
| EnergyPlus telemetry | PASS - 96 observations and 480 zone rows |
| MCP protocol | PASS - 100 successful calls; zero failed calls |
| Decisions and applications | PASS - 25 decisions; 25 proposals; 25 applications |
| Physical-response assertion | PASS - 0.63854 C next-step mean-zone change |
| EnergyPlus diagnostics | PASS - zero warnings, severe errors, or fatal errors |
| Terminal boundary | PASS - zero post-terminal control work |

The acceptance baseline used 217.9123 kWh and the controlled run used
238.6806 kWh, equivalent to -9.5306% savings. This short acceptance period
proves integration and physical control effect; it is not the primary evaluation
period and is not presented as an energy benefit.

### Remaining manual work

- Record the three-minute video using `docs/demo-script.md` and the completed
  real-run replay.
- Replace the neutral team label with registered team-member and portal details
  if the submission form requires them.
- Publish or attach the final release artifacts through the hackathon portal.

## 2026-07-26 - Final release and adversarial audit

### Completed

- Made source-archive provenance fail closed when tracked source or index
  changes are uncommitted. Archive text is normalized to LF, binary payloads
  retain exact bytes, and the manifest records the verified source commit.
- Added cross-platform archive regressions and verified them under Linux-style
  Git line-ending behavior.
- Made IDF provenance hashing independent of CRLF/LF conversion and forced
  generated JSON manifests to use LF. Real model preparation produced no IDF,
  action-schedule, actuator-map, or simulation-semantic changes.
- Separated the historical executed-run preparation-manifest byte fingerprint
  from the canonical packaged-manifest checksum.
- Corrected the presentation's comparison checksum to the canonical Git-blob
  value. Exactly one internal PPTX member changed; all six rendered slides were
  pixel-identical to their accepted versions.
- Removed documentation claims that depended on ignored temporary presentation
  authoring files and retained a tool-neutral verified-results update contract.
- Regenerated the source ZIP and all available PDFs from a clean pushed commit.
- Audited all 145 source-ZIP members against the corresponding Git archive
  bytes, repeated the build deterministically, and verified CRC, ordering,
  timestamps, modes, checksums, manifest sizes, and source commit.
- Scanned the package for secrets, real host paths, raw run directories,
  installers, weather payloads, model weights, caches, interim decks, restricted
  metadata, and unsupported or fabricated claims. No release blocker remained.
- Confirmed the final dashboard health endpoint and the latest GitHub Actions
  workflow.

### Commands and results

```text
.venv\Scripts\python.exe -m pytest -q -m "not real_energyplus and not real_ollama and not real_closed_loop"
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\mypy.exe src
.venv\Scripts\python.exe -m ecoloop doctor
.venv\Scripts\python.exe -m ecoloop package-submission
git diff --check
git push origin main
```

| Check | Result |
| --- | --- |
| Full non-real suite with Linux-style Git behavior | PASS - 223 tests; 6 real tests deselected |
| Ruff lint | PASS |
| Ruff format | PASS - 107 files |
| Strict mypy | PASS - 51 source files |
| Doctor | READY - EnergyPlus 26.1.0, pyenergyplus, Ollama, qwen3:8b, model, weather, and writable directories |
| Dashboard health | PASS - HTTP 200 |
| GitHub Actions | PASS |
| Source ZIP membership | PASS - 145 expected; zero missing or unexpected |
| Deterministic source rebuild | PASS - byte-identical |
| Manifest and checksums | PASS |
| Package security and provenance scan | PASS - zero blockers |
| Presentation acceptance | PASS - 6 slides, 6 source notes, canonical comparison checksum |
| Generated PDF acceptance | PASS - 21 pages across 4 documents |

### Adversarial outcome review

The verified 118.604 kWh facility increase is exactly the HVAC-electricity
increase; non-HVAC electricity is 665.175 kWh in both runs. The completed
controller spent 49.75 hours at 19/23 C, and all 19/23 C intervals contributed
123.749 kWh above their matched baseline intervals. All other setpoint
combinations together saved 5.145 kWh.

The deterministic safety layer correctly tightened cooling during 18
hot-condition events. Afterward, the candidate scorer's 1.5-point-per-degree
action-change penalty exceeded its modeled energy, tariff, and carbon benefit
for relaxing cooling, so the controller commonly remained at 23 C. The local
model selected the scorer's first-ranked candidate in 157 of 159 non-fallback
decisions, showing that the outcome was driven mainly by the transparent scoring
policy rather than inference randomness.

A preregistered next experiment would change only that stability coefficient,
retain every safety, occupancy, comfort, baseline, model, weather, and
evaluation constraint, and require both positive official-output savings and
baseline-or-better comfort before publication. It has not been executed and is
not represented in any result, dashboard metric, report, or slide.

## 2026-07-26 - Control-room redesign and fresh real-run audit

### Completed

- Rebuilt the Streamlit interface as a responsive six-view building control
  room with explicit live, completed, and replay states.
- Added persisted run, comfort-distribution, comparison, control-reliability,
  latency, action, tool-use, setpoint, provenance, and completeness statistics.
- Kept production selectors real-only and made comparison publication fail
  closed unless both runs are completed, non-test records with matching model
  preparation, matching weather-content checksums, passed official-output
  cross-checks, and verified finalization.
- Corrected replay summaries so comfort statistics stop at the visible replay
  frame instead of revealing future samples.
- Preserved missing optional EnergyPlus points as unavailable and excluded them
  from compliance denominators.
- Ran a fresh real one-day baseline and real EnergyPlus/MCP/Ollama controlled
  case for the live dashboard. Both runs completed successfully with zero
  EnergyPlus warnings, severe errors, or fatal errors.
- Confirmed all 25 applied setpoint messages matched same-timestamp EnergyPlus
  telemetry and that official controlled electricity agreed with persisted
  telemetry to floating-point precision.

### Commands and results

```text
.venv\Scripts\python.exe -m ecoloop run baseline --period smoke
.venv\Scripts\python.exe -m ecoloop run agent --period smoke
.venv\Scripts\python.exe -m pytest tests/integration/test_dashboard_app.py tests/integration/test_dashboard_queries.py -q
.venv\Scripts\python.exe -m pytest -m "not real_energyplus and not real_ollama and not real_closed_loop" -q
.venv\Scripts\ruff.exe check src tests
.venv\Scripts\ruff.exe format --check src tests
.venv\Scripts\mypy.exe src
```

| Check | Result |
| --- | --- |
| Fresh baseline | `baseline-20260726T151534Z-df519672` - completed |
| Fresh controlled run | `agent-20260726T151601Z-4f076a27` - completed |
| Official electricity cross-check | PASS - 238.680567 kWh; difference `8.53e-14` kWh |
| Physical actuation audit | PASS - 25 of 25 applications matched telemetry |
| Dashboard render audit | PASS - 6 views, 49 metrics, 9 charts, 8 data tables, zero exceptions |
| Dashboard query/render tests | PASS - 11 tests |
| Full non-real suite | PASS - 228 tests; 6 real tests deselected |
| Ruff lint and format | PASS |
| Strict mypy | PASS - 52 source files |
| Dashboard health | PASS - HTTP 200 |

The fresh smoke comparison used 217.912262 kWh for the baseline and
238.680567 kWh for the controlled run. Facility electricity was 9.5306% higher
and peak demand was 4.8537% higher, while occupied temperature violation fell
from 30.3846% to 9.6154%. The dashboard presents this measured trade-off
directly and does not label it as an energy saving.
