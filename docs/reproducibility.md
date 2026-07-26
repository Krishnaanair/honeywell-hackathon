# Reproducibility

## Environment

```powershell
py -3.11 --version
uv --version
uv sync --extra dev --locked
Copy-Item .env.example .env
python -m ecoloop doctor
```

Record:

- operating system and architecture;
- Python and `uv` versions;
- EnergyPlus build;
- Ollama version and model digest;
- Git commit;
- model and weather SHA-256.

## Model preparation

```powershell
python -m ecoloop prepare-model
```

Expected committed/generated paths:

```text
models/base/building.idf
models/generated/baseline.idf
models/generated/agent_ready.idf
models/generated/agent_replay.idf
models/generated/action_schedule.csv
models/generated/actuator_map.csv
models/generated/preparation-manifest.json
```

Preparation uses the EnergyPlus 26.1 schema/epJSON conversion path and writes a
manifest. Repeating it with the same inputs must produce matching semantic
fingerprints.

## Runs

```powershell
python -m ecoloop run baseline --period smoke
python -m ecoloop run rule --period smoke
python -m ecoloop run agent --period smoke
python -m ecoloop compare BASELINE_RUN_ID AGENT_RUN_ID
```

Each run has a unique ID and directory. Baseline and controlled runs execute in
separate processes. Preserve the full EnergyPlus output directory.

## Replay

```powershell
python -m ecoloop replay RUN_ID
```

Replay uses the completed run's applied action sequence and does not call
Ollama. Compare replay setpoint sequence and official outputs against the source
run within documented numerical tolerance.

## Export

```powershell
python -m ecoloop export RUN_ID
```

Required exports:

```text
action_schedule.csv
agent_replay.idf
decisions.jsonl
telemetry.csv
metrics.json
metrics.csv
comparison.json
comparison.csv
actuator_map.csv
api_points.csv
full EnergyPlus output directory
```

Optional fields remain blank/missing rather than zero.

## Quality gates

```powershell
ruff check .
ruff format --check .
mypy src
pytest -m "not real_energyplus and not real_ollama and not real_closed_loop"
pytest -m real_energyplus
pytest -m real_ollama
pytest -m real_closed_loop
```

A skipped real test is `BLOCKED`, not `PASS`.

## Submission package

```powershell
python -m ecoloop package-submission
```

Verify `submission/checksums.txt` against every packaged file and inspect
`submission/submission-manifest.json`. The source archive excludes Git metadata,
virtual environments, model weights, installers, caches, and oversized raw
temporary runs while retaining source, tests, lock file, models/replay, small
verified exports, documentation, licences, and setup instructions.

In a Git checkout, the packager enumerates indexed paths with fixed, non-shell
Git commands and reads only those reviewed paths from the working tree. The only
untracked additions it accepts are the completed
`presentation/ecoloop-submission.pptx` and the named export files directly under
`results/` (`action_schedule.csv`, `actuator_map.csv`, `agent_replay.idf`,
`api_points.csv`, `comparison.csv`, `comparison.json`, `decisions.jsonl`,
`metrics.csv`, `metrics.json`, and `telemetry.csv`). Run:

```powershell
python -m ecoloop export RUN_ID --output-dir results
```

An unpacked source distribution outside any Git checkout uses a restricted walk
of known source directories. If the source is within a checkout but indexed
paths cannot be enumerated, or the selected root is not that checkout's top
level, packaging stops instead of falling back. Both modes reject symlinks,
credential-like files or values, user-home paths, raw EnergyPlus output,
weather data, model weights, installers, caches, archives, and files larger
than 20 MiB. ZIP entry order, timestamps, and permissions are normalized for
reproducible output.
