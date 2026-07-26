# Safety and Failure Model

EcoLoop assumes model output, MCP inputs, IDF/log text, weather metadata and
dashboard parameters may be malformed or adversarial.

## Safety invariants

- Ollama never writes an EnergyPlus actuator directly.
- The model selects only among bounded candidates or requests fallback.
- Independent typed validation authorizes every action.
- Unsupported capability fields must be absent.
- Heating remains below cooling with the configured deadband.
- Actions are finite, fresh, unexpired, exact-run, exact-observation and
  monotonically generated.
- A physical actuator application is recorded only after successful callback
  writes.
- Fatal EnergyPlus failure terminates the run and suppresses comparison KPIs.

## Failure behavior

| Failure | Response |
| --- | --- |
| Model unavailable or timeout | One bounded retry, last-safe for at most one interval, then deterministic fallback |
| MCP unavailable/tool error | Reject model attempt and activate deterministic fallback |
| Malformed/unknown action | Typed rejection; no actuator write |
| Unsafe but repairable proposal | Clamp/repair with original and final values logged |
| Repeated validation/model failures | Open circuit breaker; deterministic control only |
| Missing required EnergyPlus handle | Persist API catalogue and precise near matches; fail the run |
| EnergyPlus severe message | Preserve and expose bounded diagnostic |
| EnergyPlus fatal/exit failure | Stop, mark failed, preserve all outputs; publish no savings |
| Dashboard disconnect | Show disconnected/empty state; infer no metrics |

## Tool and filesystem boundary

The MCP surface has no shell, command execution, package installation, arbitrary
network, delete, or unrestricted filesystem operations. Diagnostic paths are
resolved and confined to the repository or discovered EnergyPlus installation.
Model- or log-provided text cannot grant additional authority.

No hosted-model secret is required, and `.env` is ignored. Release audits scan
for secrets, fixed KPI claims, unlabelled fake data and private machine paths.
