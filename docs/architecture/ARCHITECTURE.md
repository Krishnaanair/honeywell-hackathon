# EcoLoop Runtime Architecture

This document is the acceptance-oriented architecture view. The longer module
inventory and configuration notes remain in
[`../architecture.md`](../architecture.md).

## Component boundary

```mermaid
flowchart LR
    subgraph SIM["EnergyPlus child process"]
        API["EnergyPlus 26.1 Runtime API"]
        OBS["End-zone observation callback"]
        ACT["Begin-zone actuation callback"]
        REG["Handle registry"]
    end

    subgraph BUS["Durable process boundary"]
        DB[("SQLite WAL")]
        ACK["Physical actuator applications"]
    end

    subgraph CONTROL["Supervisory host process"]
        COORD["Cadence + bounded coordinator"]
        CLIENT["Official MCP client"]
        OLLAMA["Local Ollama / qwen3:8b"]
        SAFE["Deterministic optimizer + safety shield"]
    end

    subgraph MCP["Private stdio child"]
        SERVER["FastMCP server"]
        TOOLS["Allow-listed building tools"]
    end

    OUTPUT["EnergyPlus SQL/CSV outputs"]
    DASH["Streamlit command center"]

    API --> OBS --> DB
    REG --> OBS
    DB --> COORD
    COORD --> CLIENT
    CLIENT <--> SERVER
    SERVER --> TOOLS --> DB
    CLIENT <--> OLLAMA
    TOOLS --> SAFE --> DB
    DB --> ACT --> API
    ACT --> ACK --> DB
    API --> OUTPUT --> DB
    DB --> DASH
```

The EnergyPlus state never crosses the child-process boundary. The supervisory
worker and MCP tools exchange only typed, persisted state. Ollama cannot access
the EnergyPlus API, shell, package manager, or unrestricted filesystem.

## Closed-loop sequence

```mermaid
sequenceDiagram
    participant E as EnergyPlus
    participant R as Runtime callback
    participant D as SQLite WAL
    participant H as Agent host
    participant M as MCP stdio server
    participant L as Local Ollama
    participant S as Safety shield

    E->>R: End of zone timestep
    R->>D: Observation + zone telemetry
    H->>D: Read new due observation
    H->>L: Compact state and discovered tools
    L->>H: get_current_building_state
    H->>M: tools/call
    M->>D: Read exact run state
    D-->>M: Typed observation
    M-->>H: Structured result
    L->>H: get_constraints + candidate tool
    H->>M: tools/call
    M->>S: Generate/evaluate bounded candidates
    S-->>M: Components and recommendation
    M-->>H: Structured candidates
    L->>H: apply_control_action or fallback
    H->>M: terminal tools/call
    M->>S: Independent validation
    S->>D: Proposed and approved values
    E->>R: Begin next zone timestep
    R->>D: Read exact fresh generation
    R->>E: Write verified schedule actuators
    R->>D: Physical application acknowledgement
    E->>R: End of subsequent zone timestep
    R->>D: Subsequent state
```

## Fast and supervisory loops

The EnergyPlus child owns the fast loop. It reads sensors, records one
deterministic timestep identity, applies only the latest exact-observation
approved action, and performs immediate runtime freshness/generation checks.

The host owns the slower supervisory loop. The default decision interval is 60
simulated minutes, with configured comfort, occupancy, demand, outdoor-change,
and expiry triggers. Model inference runs outside the EnergyPlus process. The
current accelerated demonstration uses a bounded exact-observation handshake so
EnergyPlus does not outrun a local model; it never waits indefinitely. A future
real-time building adapter would use the same durable queue without pausing a
field controller.

## Control authority

The local model has a meaningful but limited role:

- choose whether to use the evaluated candidate path or safe fallback;
- inspect current state and constraints through MCP;
- select one of the bounded candidates;
- provide a concise reason code and operational explanation.

Deterministic code alone decides permissible numeric ranges, occupied and
unoccupied comfort bounds, thermostat deadband, ramp rate, hold duration,
capabilities, emergency recovery, freshness, expiry, monotonic generation, and
final authorization.

## Process and thread boundaries

| Boundary | Owner | Data crossing it |
| --- | --- | --- |
| EnergyPlus process | Fresh PyEnergyPlus state per run | SQLite rows and output files only |
| Runtime callback thread | EnergyPlus | Typed sensor values and approved action records |
| Coordinator event loop | Parent Python process | Observation IDs, compact state, decision results |
| MCP stdio child | FastMCP server | JSON-RPC initialization, tools/list and tools/call |
| Ollama HTTP endpoint | Local/self-hosted runtime | Compact state, tool schemas/results, concise decision |
| Dashboard process | Streamlit | Read-only SQLite queries and bounded evidence downloads |

Baseline and controlled runs always receive separate EnergyPlus processes and
states. No callback or API state is reused.

## Failure paths

```mermaid
flowchart TD
    O["Due real observation"] --> Q{"MCP and model complete before deadline?"}
    Q -->|Yes| V{"Deterministic validation"}
    V -->|Pass / clamp / repair| A["Approved action queue"]
    V -->|Reject| F["Safe fallback"]
    Q -->|No| L{"Recent safe action still valid?"}
    L -->|Yes, one interval| K["Last-known-safe action"]
    L -->|No| F
    F --> C{"Consecutive failures exceed limit?"}
    C -->|No| A
    C -->|Yes| B["Open circuit breaker; deterministic control only"]
    A --> E["EnergyPlus callback applies"]
    E --> X{"Fatal EnergyPlus error?"}
    X -->|No| O
    X -->|Yes| P["Stop run, preserve outputs, mark failed, publish no savings"]
```

Malformed model output, unknown tools, skipped mandatory tools, MCP transport
failure, timeout, stale actions, unavailable actuators, and repeated rejection
all converge on deterministic fallback. A fatal EnergyPlus run is never
described as recovered; a configured restart is a new run ID.

## Live, replay and test data paths

- **LIVE SIMULATION:** a running real EnergyPlus row is receiving new callback
  telemetry. Dashboard freshness is based on persisted wall time.
- **VERIFIED RUN REPLAY:** a completed real controlled run with verified final
  metrics and passing official-energy cross-check is rendered at a controlled
  visual clock. No model is called during display replay.
- **DEMO DATA:** explicit `--fake` fixtures used only by tests/CI. Production
  dashboard queries exclude these rows.
- **DISCONNECTED:** no valid database/run or stale backend; no KPI is inferred.

Runtime action replay uses the source run's immutable model, actuator map,
weather checksum, and physical application timestamps. It is distinct from
dashboard visual replay.

## Baseline comparison path

The baseline is a conventional fixed occupied/unoccupied schedule. A controlled
run may attach only to a completed baseline with matching EnergyPlus version,
period snapshot, model-preparation fingerprint, weather file checksum, and
verified official metrics. Publication also requires identical finalized
simulation start/end and passing telemetry-to-official energy cross-checks.

Energy, peak, cost, carbon, and comfort formulas exist only in the backend
metrics/evaluation layer. The dashboard consumes the resulting canonical
metrics; it does not implement alternative KPI formulas.

## Security boundary

The MCP server exposes only typed building operations and repository/installed
EnergyPlus diagnostic paths. It exposes no shell, package installation, network
browser, deletion, or arbitrary read/write tools. IDF content and EnergyPlus log
text are treated as untrusted data and bounded before model context. No secret
or cloud API credential is required.
