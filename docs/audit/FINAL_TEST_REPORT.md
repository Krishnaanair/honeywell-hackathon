# Final Test Report

Date: 2026-07-26 (22:00–23:15 IST). Host: Windows 11 Pro 10.0.26200, Python
3.11.9, EnergyPlus 26.1.0 (via `ENERGYPLUS_HOME`), Ollama with
`qwen3:8b` at `http://127.0.0.1:11434`. All commands ran from the repository
root with `.venv\Scripts\python.exe` unless noted. Raw logs for this session
are retained in the session scratchpad; durable evidence paths are listed per
item below.

## 1. Environment doctor

```text
python -m ecoloop doctor --json
```

Result: `ok: true`. All fourteen checks PASS, including EnergyPlus executable,
version 26.1.0, pyenergyplus, `energyplusapi.dll`, ExpandObjects,
ConvertInputFormat + epJSON schema, model `models\base\building.idf`, weather
`weather\default.epw`, Ollama executable and loopback API, configured model
`qwen3:8b`, and writable `runs/` and `submission/` directories.

## 2. Static quality gates (final working tree)

| Command | Result |
| --- | --- |
| `ruff check src tests` | PASS (`All checks passed!`) |
| `ruff format --check src tests` | PASS (91 files formatted) |
| `mypy src` | PASS (`no issues found in 53 source files`) |

Two concrete failures were found and fixed during this pass:

1. `ruff format --check` initially reported 10 unformatted files (formatting
   drift from the rescue edits). Fixed mechanically with `ruff format`; no
   semantic changes.
2. `mypy` reported `src\ecoloop\coordinator.py:1097: Statement is unreachable`.
   Root cause: a defensive `if run is None: continue` inside
   `_select_compatible_baseline` on a list whose element type can never be
   `None`. The dead branch was removed; behaviour is unchanged.

## 3. Full deterministic suite

```text
python -m pytest -q -m "not real_energyplus and not real_ollama and not real_closed_loop"
```

Result history today:

| Tree state | Result | Duration |
| --- | --- | --- |
| Handoff tree (before fixes) | 258 passed, 6 deselected | 58.78s |
| After formatting + mypy fix | 258 passed, 6 deselected | 55.86s |
| Final tree (incl. CWD-isolation fix + 2 new regression tests) | **260 passed, 6 deselected** | 57.82s |

## 4. Real closed-loop smoke test

Command (as specified in the handoff):

```text
python -m pytest -q tests\real\test_closed_loop_smoke.py::test_real_energyplus_mcp_ollama_closed_loop_smoke -o tmp_path_retention_policy=all
```

### 4.1 Recovered interrupted run (evidence: retained pytest tmp `pytest-178`)

The handoff-time run's processes were gone, but its retained artifacts prove it
ran to completion at 22:11:51 IST: baseline `baseline-20260726T163354Z-91bc32a4`
(completed 22:04), agent `agent-20260726T163404Z-69aa78d5` (completed 22:11:50),
and `verified-comparison/comparison.json` + `comparison.csv` written 22:11:51 —
the comparison write is the final assertion stage of the test. Repository
`.pytest_cache/v/cache/nodeids` was rewritten at exactly 22:11:51 (a graceful
pytest session finish) while `lastfailed` still contains `{}` from 21:57 —
pytest records failures at session end, so a failure would have rewritten it.
Recovered comparison: baseline 217.912 kWh → agent 222.457 kWh (+2.085%),
comfort compliance 69.615% → 81.923%.

### 4.2 First rerun: failed due to a genuine concurrency defect (fixed)

A rerun executed concurrently with the replay proof run failed in 15.44s:
EnergyPlus exited 1 with one severe error —
`remove: The process cannot access the file because it is being used by another
process.: "readvars.audit"`. Root cause: every simulation child process
inherited the repository root as its working directory, and ReadVarsESO
(EnergyPlus `-r`) locks and deletes `readvars.audit` in the process CWD, so two
concurrent real runs collided. The run was honestly marked `failed`
(fail-closed behaviour worked). Evidence: retained `pytest-181` tmp directory,
`baseline-20260726T165648Z-da3d6b4d/energyplus/eplusout.err`.

Fix: `src/ecoloop/energyplus/runtime.py` — `_isolated_request` resolves every
request path, then `_child_entry` creates and `chdir`s into the run's own
output directory before executing, so per-run scratch files can no longer
collide. Regression tests added in `tests/unit/test_energyplus_runtime.py`
(`test_isolated_request_resolves_paths_before_the_child_changes_directory`,
`test_child_entry_executes_from_the_run_output_directory`).

### 4.3 Definitive standalone rerun (final tree)

Captured result: **`1 passed in 472.87s (0:07:52)`** (exit 0). Runs
`baseline-20260726T170305Z-ce8c8199` and `agent-20260726T170317Z-cf8ebc53`,
retained under pytest tmp `pytest-184` with `verified-comparison/` written at
17:10:55Z. The test asserts the full closed-loop chain: verified real baseline
and agent runs, a `qwen3:8b`-selected MCP tool sequence
(`get_current_building_state` → `get_constraints` → candidate tools →
`apply_control_action`), a changed model-selected setpoint reported by later
telemetry with a zone-temperature response, verified official EnergyPlus
totals with the energy cross-check, and an honest comparison written and
re-read.

Repeatability: this pass reproduced the recovered 22:11 run **digit-for-digit**
— baseline 217.91226189328628 kWh (also identical to the retained Pair A
baseline) and agent 222.4567741294708 kWh, comfort compliance 69.61538% →
81.92308% (+2.0855% electricity, +6.3118% peak in this smoke configuration).
Two independent full closed-loop executions today produced identical official
totals because model preparation is content-hashed and inference is pinned to
`temperature=0, seed=0`.

## 5. Replay equivalence proof (repaired replay)

```text
python -m ecoloop replay agent-20260726T151601Z-4f076a27
```

Result: replay run `replay-20260726T165649Z-6ff9f492`, status `completed`,
EnergyPlus exit 0, **0 warnings / 0 severe / 0 fatal**, 96 observations, 25/25
actions applied via `runtime_schedule_replay_from_physical_acknowledgements`
(timing source: the source run's verified physical acknowledgements; model
source: the source run's immutable snapshot).

| Metric | Source agent run | Replay | Difference |
| --- | --- | --- | --- |
| Facility electricity (kWh) | 238.68056748751272 | 238.68056748751272 | 0 (identical) |
| HVAC electricity (kWh) | 110.20556748751262 | 110.20556748751262 | 0 (identical) |
| Peak demand (kW) | 23.35430606664108 | 23.35430606664108 | 0 (identical) |
| Occupied temp violation (%) | 9.615384615384617 | 9.615384615384617 | 0 (identical) |
| PMV compliance (%) | 97.3076923076923 | 97.3076923076923 | 0 (identical) |

The repaired replay reproduces the source run's official EnergyPlus results
digit-for-digit. Evidence: `runs/replay-20260726T165649Z-6ff9f492/metrics.json`
and `runs/agent-20260726T151601Z-4f076a27/metrics.json`; energy cross-check
difference 3.6e-14%.

The same command was then executed for the representative-week agent run:

```text
python -m ecoloop replay agent-20260726T130255Z-65de6a32
```

Result: `replay-20260726T171617Z-5ffa3497`, status `completed`, exit 0, 0
warnings / 0 severe / 0 fatal, 672 observations, **170/170** actions applied.
Official totals are again identical to the source week agent run: facility
1245.564416842928 kWh, HVAC 580.389416842928 kWh, peak 24.2293835189757 kW,
occupied violation 9.6923077%, PMV compliance 97.4615385%, cost
149.46773002115137, carbon 871.8950917900496 kg; cross-check difference
1.8e-14%. The pre-repair replay (`replay-20260726T134158Z-8fc97abd`) had been
0.290% high with actions applied one timestep early; the repair fully closes
that gap on both the one-day and week sources.

## 6. Verified one-day comparison artifact (Pair A)

```text
python -m ecoloop compare baseline-20260726T151534Z-df519672 agent-20260726T151601Z-4f076a27
```

Persisted to `runs/agent-20260726T151601Z-4f076a27/export/comparison.json` and
`.csv`: electricity 217.91226189328628 → 238.68056748751272 kWh (**+9.5306%,
not a saving**), peak 22.273230537572715 → 23.35430606664108 kW (+4.8537%),
comfort compliance 69.61538% → 90.38462%, HVAC 89.437 → 110.206 kWh.

## 7. Independent recomputation of all published numbers

An independent read-only audit recomputed every claimed number directly from
raw stored telemetry and audit rows in `runs/ecoloop.db` (never from docs),
using the comfort policy in `config/default.toml`. Every claim for both pairs
was CONFIRMED, including: Pair A electricity/peak/violation percentages and
25 decisions / 25 actions / 99 successful MCP calls / 4 fallbacks / 3 clamps /
zero EnergyPlus warning-severe-fatal; Pair B (representative week)
1126.9604940141178 → 1245.564416842928 kWh (+10.5242%), peak +5.3154%, comfort
67.769231% → 90.307692%, PMV 94.615385% → 97.461538%, 170 decisions, 788 MCP
calls (784 success + 4 failed), 11 fallbacks, 24 clamps. The diagnostic rule
run `rule-20260726T160009Z-e2de2c52` is confirmed non-comparable with the
retained week baseline (different preparation fingerprints; no stored
comparison references it).

Disclosed nuances (kept in all claim wording):

- The Pair A agent run's 25 physical actuator applications are stored via the
  legacy verified runtime-callback-message path (each `occurrence_count=1`,
  setpoints matching validated actions within 5.1e-4, and all facility
  telemetry rows in each application's active window consistent with it). The
  structured migration-003 `actuator_applications` rows exist from the replay
  child onward (the migration landed after that source run); new runs record
  structured rows directly, as the standalone smoke rerun demonstrates.
- The Pair B agent run's `errors` table contains 6 agent-side severe rows
  (coordinator/MCP fallback diagnostics). EnergyPlus itself reported zero
  severe/fatal for that run. Wording must never conflate the two.

## 8. Hostile truth sweep

`git diff --check`: clean. Case-insensitive sweep for
`mock|fake|sample|random|hardcoded|TODO|FIXME|placeholder|openai|anthropic|gemini|secret|api[_-]?key`
across `src`, `tests`, `docs`, `README.md`: **zero submission blockers**.
All `fake` sites are hermetic test fixtures or the explicit `--fake`/`is_fake`
boundary excluded from production selectors; dashboard/report KPIs are computed
from the run store (no hardcoded result numbers); no `@pytest.mark.skip`/
`xfail` anywhere (only precise runtime dependency skips inside `tests/real/`);
no non-loopback network access in `src`; `agent/ollama_host.py` enforces a
loopback allowlist and actively raises if `OLLAMA_API_KEY` is set; zero
matches for hosted-model providers in `src`; no `random` usage in `src`
(model pinned to `temperature=0, seed=0`). The secret-looking regexes in
`reporting.py` are a defensive packaging scanner that rejects bundles
containing credentials.

## 9. Summary of failures found today and their dispositions

| Failure | Root cause | Fix | Re-verified by |
| --- | --- | --- | --- |
| `ruff format --check` (10 files) | Formatting drift in rescue edits | `ruff format` | Format check PASS |
| `mypy` unreachable statement | Dead defensive branch in `_select_compatible_baseline` | Removed dead branch | mypy PASS, suite 260 passed |
| Real smoke rerun severe error (concurrent) | Shared child CWD → ReadVarsESO `readvars.audit` collision | Per-run child CWD isolation in `runtime.py` | 2 new regression tests; standalone smoke PASS |

No test expectations were lowered, and no real dependency was mocked in any
end-to-end path.

## 10. Packaging procedure

Executed after all commits, on a clean tracked tree:

1. `python -m ecoloop package-submission` regenerates `submission/`
   (`ecoloop-source.zip`, `system-architecture.pdf`, `results-report.pdf`,
   `demo-script.pdf`, `presentation.pdf`, `checksums.txt`,
   `submission-manifest.json`). The command itself resolves `HEAD` at package
   time, refuses dirty trees, scans the source archive for credentials, and
   verifies rendered PDF content.
2. The upload folder `Honeywell_Hackathon_Submission/` is rebuilt from that
   fresh output with the prescribed artifact names
   (`EcoLoop_Demo_Script.pdf`, `EcoLoop_Results_Report.pdf`,
   `EcoLoop_Source_Code.zip`, `EcoLoop_Submission_Deck.pdf`,
   `EcoLoop_Submission_Deck.pptx`, `EcoLoop_System_Architecture.pdf`,
   `checksums.txt`, `submission-manifest.json`), and the outer
   `Honeywell_Hackathon_Submission.zip` is rebuilt from the folder.
3. Verification: every SHA-256 in both `checksums.txt` files and both
   manifests is recomputed from the bytes on disk, the manifest
   `source_commit` is compared against `git rev-parse HEAD`, and the outer
   zip entry list is compared against the folder inventory. The verified
   hashes are recorded in the package manifests themselves, which are the
   authoritative inventory.
