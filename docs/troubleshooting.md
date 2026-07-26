# Troubleshooting

Start with:

```powershell
python -m ecoloop doctor
```

## Python is not 3.11

Install Python 3.11 and create a clean environment:

```powershell
winget install --id Python.Python.3.11 --exact
py -3.11 -m pip install uv
uv sync --extra dev
```

Do not use the EnergyPlus-bundled Python as the project environment.

## EnergyPlus not found or wrong version

Install the official EnergyPlus 26.1.0 package for the platform, then set:

```powershell
$env:ENERGYPLUS_HOME = "C:\path\to\EnergyPlusV26-1-0"
python -m ecoloop doctor
```

The directory must contain `energyplus`, `ExpandObjects`,
`Energy+.schema.epJSON`, the platform dynamic library, and `pyenergyplus`.
Other EnergyPlus versions are rejected.

## PyEnergyPlus import fails

PyEnergyPlus ships with EnergyPlus. Ensure `ENERGYPLUS_HOME` points to the
installation root. EcoLoop adds only that root to the worker import path. Do not
install an unrelated package with the same name from PyPI.

On Windows, verify the required Visual C++ runtime is installed.

## Model or weather missing

```powershell
$env:ECOLOOP_MODEL_PATH = "C:\absolute\path\building.idf"
$env:ECOLOOP_WEATHER_PATH = "C:\absolute\path\weather.epw"
python -m ecoloop doctor
```

The model must be EnergyPlus 26.1-compatible. Record weather source, licence, and
checksum before redistribution.

## Missing EnergyPlus API handle

Open the run's `api_points.csv` and the error record. The error lists requested
name/key and normalized near matches.

- Confirm model preparation requested the variable.
- Confirm key names after HVAC Template expansion.
- Confirm the handle is requested only after API readiness.
- Treat a required missing point as a failed integration.

Do not replace a missing value with zero.

## EnergyPlus fatal error

Inspect:

- `eplusout.err`
- simulation messages/errors in the dashboard
- generated model
- run manifest and exact command

The run remains failed and cannot be compared. Use only deterministic,
version-aware model repairs.

## Ollama unreachable

```powershell
ollama serve
curl.exe http://127.0.0.1:11434/api/tags
```

If using another local bind address, set `OLLAMA_HOST`. The runtime does not
connect to hosted inference.

## Model missing

```powershell
ollama pull qwen3:8b
$env:OLLAMA_MODEL = "qwen3:8b"
python -m ecoloop doctor
```

The selected model must support tool calling. A different model can be
configured but must pass the real tool-call smoke test.

## MCP stdio server exits

Run:

```powershell
python -m ecoloop mcp-server
```

Protocol output belongs on stdout; operational logs belong on stderr. Validate
with the MCP Inspector instructions in the README. General filesystem/shell
tools are intentionally absent.

## Database locked

EcoLoop enables WAL and a busy timeout. Confirm all processes use the configured
database path and that antivirus/file synchronization is not holding the file.
Do not place the run database in a network-synchronized directory during a live
demo.

## Dashboard says incomplete

This is an integrity gate. Confirm both run states are `completed`, fingerprints
and periods match, official output totals parsed, and telemetry cross-check is
within tolerance.

## Presentation PDF unavailable

The PPTX is generated from the supplied template. PDF export requires a working
PowerPoint/LibreOffice renderer. Install LibreOffice and ensure `soffice` is on
PATH, then rerun `package-submission`. The command reports a missing renderer
instead of writing a fake PDF.
