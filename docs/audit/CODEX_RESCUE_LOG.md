# EcoLoop Rescue Log

This journal records the evidence-driven rescue and acceptance audit requested
for the existing hackathon repository. It supplements, rather than replaces,
the historical implementation journal in `docs/progress.md`.

## Phase 0 - Protect the current work

### Work completed

- Inspected tracked, untracked, ignored, branch, worktree, and remote state.
- Confirmed the tracked working tree was clean at
  `0fc71b18c2c37fbb5ead3319a6bb41a85ff1d2e1`.
- Preserved the existing `main` revision and created the isolated
  `codex/ecoloop-rescue` branch.
- Recorded the protected source tree and a summary of ignored runtime evidence
  in `docs/audit/ORIGINAL_REPOSITORY_TREE.txt`.
- Reviewed and strengthened the durable repository instructions in `AGENTS.md`.
- Kept all existing real run directories, SQLite evidence, generated reports,
  presentation files, weather data, and upload bundles intact.

### Commands executed

```text
git status --short
git status --ignored --short
git branch --all --verbose --no-abbrev
git worktree list --porcelain
git rev-parse HEAD
git remote -v
git ls-tree -r --name-only 0fc71b18c2c37fbb5ead3319a6bb41a85ff1d2e1
git switch -c codex/ecoloop-rescue
```

### Tests

- Not applicable to this protection-only phase.

### Decisions

- Use a rescue branch in the existing worktree because there were no tracked
  uncommitted changes to isolate or save as a patch.
- Treat ignored run/output directories as evidence, not source; preserve them
  locally and keep them excluded from source commits.
- Audit before changing runtime architecture because the repository already
  contains executed real EnergyPlus, Ollama, and MCP evidence.

### Current blockers

- None.

### Next action

- Complete the forensic component audit and requirement traceability matrix,
  then repair only evidence-backed gaps.

## Phase 1 - Forensic audit

### Work completed

- Inspected runtime callbacks, handle discovery, SQLite migrations and store,
  MCP server/client protocol, local-model host, safety/candidate logic,
  coordinator/demo behavior, final metrics, replay/report generation,
  dashboard queries/rendering, tests, documents and submission archive.
- Independently queried the retained real SQLite evidence and full EnergyPlus
  output directories.
- Created `CURRENT_STATE_AUDIT.md` and
  `REQUIREMENT_TRACEABILITY_MATRIX.md`.
- Traced a real closed-loop decision from observation 3083 through local
  `qwen3:8b`, four MCP calls, validation, the 08:30 actuator write, and
  observation 3084.
- Identified the replay timing defect, missing structured physical
  acknowledgement, run-time mutation of tracked generated IDFs, weak demo
  baseline locking, incomplete supervisory trend context, and dashboard
  truth-label edge cases.
- Confirmed the representative week is an honest negative energy outcome:
  comfort compliance rose from 67.77% to 90.31%, while facility electricity
  rose from 1126.9605 to 1245.5644 kWh.

### Commands executed

```text
python -m ecoloop doctor --json
pytest -q tests/unit/test_energyplus_runtime.py tests/unit/test_energyplus_store_adapter.py tests/unit/test_handles.py tests/unit/test_safety.py tests/unit/test_candidates.py tests/unit/test_mcp_tools.py tests/unit/test_ollama_host.py
pytest -q tests/integration/test_mcp_stdio.py tests/integration/test_agent_loop.py tests/integration/test_coordinator.py tests/integration/test_timeout_circuit_breaker.py tests/integration/test_fake_closed_loop.py tests/integration/test_cli.py
pytest -q -m "not real_energyplus and not real_ollama and not real_closed_loop"
python -m ecoloop --help
```

### Tests

- Focused runtime/control/MCP unit tests: 66 passed.
- Focused integration tests: 34 passed.
- Complete non-real suite at the protected revision: 228 passed, 6 real tests
  deselected.
- Dashboard real-database AppTest: six tabs, zero exceptions.

### Decisions

- Preserve the working Runtime API, MCP, local-model, safety, metrics, dashboard
  and documentation architecture.
- Repair physical action evidence and provenance rather than replacing the
  working loop.
- Treat the retained week as a comfort/energy trade-off, not a saving.
- Do not compare the rescue rule week with the historical baseline because
  their preparation fingerprints differ.

### Current blockers

- No external dependency blocker on this host.
- The verified week does not satisfy the energy-efficiency objective.

### Next action

- Repair replay/physical acknowledgement, immutable preparation and baseline
  locking; enrich control context; then rerun all validation gates.

## Phase 2 - Provenance and dashboard repairs

### Work completed

- Moved normal run model preparation to a run-local staging directory.
- Added exact period dates and generated artifact hashes to the preparation
  manifest.
- Required exact non-null preparation and weather hashes plus verified final
  metrics when selecting a real baseline.
- Added an explicit baseline lock to the run CLI and demo child process, with a
  parent-ID assertion.
- Added exact finalized simulation-window compatibility to publication.
- Restored the canonical tracked generated IDFs to the configured smoke period.
- Tightened dashboard run selection, replay eligibility, verified-evidence
  branding and rule-controller wording.
- Removed the nonfunctional production dashboard fake-data flag.
- Added the acceptance-oriented architecture, evaluation, comfort, safety,
  demo and presentation documents requested by the rescue brief.

### Commands executed

```text
python -m ecoloop prepare-model --period smoke
pytest -q tests/integration/test_demo.py tests/integration/test_evaluation.py tests/integration/test_coordinator.py
ruff check <changed runtime-provenance files and tests>
```

### Tests

- Provenance/demo/evaluation/coordinator tests: 25 passed.
- Dashboard/CLI focused tests: 18 passed.
- Ruff, dashboard/CLI mypy and diff checks: passed.
- Real-database dashboard AppTest: zero exceptions and zero UI errors.

### Current blockers

- Structured physical application/replay repair is still in progress.
- Full rescue suite and real dependency tests have not yet been rerun.

### Next action

- Finish physical acknowledgement/replay repair, then repair supervisory context
  and execute the complete validation matrix.

## Phase 3 - Final validation, replay equivalence, and truth audit

### Work completed

- Recovered the interrupted real closed-loop smoke result from retained
  artifacts (comparison written 2026-07-26 22:11:51 IST; pytest cache shows a
  clean finish with no recorded failure) and reran the test standalone with
  captured output on the final tree.
- Fixed the three concrete validation failures found today: ten unformatted
  files, one mypy unreachable-statement error in
  `coordinator._select_compatible_baseline`, and a real concurrency defect in
  which simulation children shared the repository working directory and
  collided on ReadVarsESO's `readvars.audit` scratch file. Simulation children
  now resolve request paths and run inside their own output directory
  (`runtime._isolated_request`, `_child_entry`), with two regression tests.
- Executed the repaired replay against both retained agent runs. Both replays
  completed with zero EnergyPlus diagnostics and reproduced their sources
  digit-for-digit (one-day 238.68056748751272 kWh; week 1245.564416842928 kWh,
  170/170 actions).
- Persisted the one-day comparison artifact via the real compare command.
- Ran four independent hostile audits: a repository truth sweep (zero
  submission blockers), a read-only recomputation of every published number
  from raw store rows (all confirmed), a documentation claims cross-check
  (two artifact-name defects fixed in the evidence map; wrong env-var names
  fixed in the three-minute demo script), and a packaging-source wording audit
  (content sources already correct; packaging requires a clean tree).
- Completed `docs/audit/FINAL_TEST_REPORT.md` and the requirement traceability
  matrix final statuses; updated `docs/results.md` with the repaired replay
  equivalence result.
- Restarted the dashboard on the final code; it selects the genuine completed
  agent run.

### Commands executed

```text
python -m ecoloop doctor --json                          -> ok: true
ruff check src tests                                     -> pass
ruff format --check src tests                            -> pass (after fixes)
mypy src                                                 -> pass (after fix)
pytest -q -m "not real_energyplus and not real_ollama and not real_closed_loop"
                                                         -> 260 passed
pytest -q tests/real/test_closed_loop_smoke.py::test_real_energyplus_mcp_ollama_closed_loop_smoke
                                                         -> 1 passed in 472.87s
python -m ecoloop replay agent-20260726T151601Z-4f076a27 -> replay-20260726T165649Z-6ff9f492, identical totals
python -m ecoloop replay agent-20260726T130255Z-65de6a32 -> replay-20260726T171617Z-5ffa3497, identical totals
python -m ecoloop compare baseline-20260726T151534Z-df519672 agent-20260726T151601Z-4f076a27
                                                         -> verified comparison persisted
```

### Tests

- Full deterministic suite: 260 passed, 6 deselected (258 before the two new
  regression tests).
- Real closed-loop smoke: passed twice today on independent run pairs with
  digit-identical official totals (deterministic preparation plus
  temperature=0, seed=0 inference).
- One first-rerun failure was root-caused to the shared-scratch-directory
  defect above, fixed, and re-verified; no expectations were lowered.

### Decisions

- Keep `qwen3:8b` as the submission model; the installed `mistral:latest` is
  older, unvalidated for this pipeline, and switching would orphan the entire
  verified evidence chain. The model remains configurable via `OLLAMA_MODEL`.
- Keep the EcoLoop name; every validated artifact and the prescribed upload
  inventory are EcoLoop-branded, and a "save"-themed rename would conflict
  with the honestly reported no-saving week result.
- Report the week as a comfort gain with an electricity increase, never as a
  saving; replay equivalence is now claimed only because it was measured.

### Current blockers

- None external. Remaining work is packaging, upload-folder regeneration,
  checksum verification, and push.

### Next action

- Commit in reviewable stages, rebuild the dashboard presentation layer,
  regenerate the submission package, verify checksums, and push the branch.
