CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    is_fake INTEGER NOT NULL DEFAULT 0 CHECK (is_fake IN (0, 1)),
    energyplus_version TEXT,
    model_path TEXT,
    weather_path TEXT,
    period_name TEXT,
    parent_run_id TEXT REFERENCES runs(run_id),
    started_at TEXT,
    completed_at TEXT,
    progress_percent REAL CHECK (
        progress_percent IS NULL OR
        (progress_percent >= 0.0 AND progress_percent <= 100.0)
    ),
    error_summary TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS telemetry (
    telemetry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    simulation_timestamp TEXT NOT NULL,
    timestep_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    outdoor_temperature_c REAL,
    facility_demand_kw REAL,
    timestep_electricity_kwh REAL,
    cumulative_electricity_kwh REAL,
    hvac_electricity_kwh REAL,
    heating_setpoint_c REAL,
    cooling_setpoint_c REAL,
    UNIQUE (run_id, timestep_key)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_run_simulation_time
    ON telemetry(run_id, simulation_timestamp);

CREATE TABLE IF NOT EXISTS zone_telemetry (
    zone_telemetry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    simulation_timestamp TEXT NOT NULL,
    timestep_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    zone_name TEXT NOT NULL,
    mean_air_temperature_c REAL NOT NULL,
    operative_temperature_c REAL,
    relative_humidity_percent REAL,
    occupant_count REAL,
    pmv REAL,
    ppd_percent REAL,
    co2_ppm REAL,
    heating_setpoint_c REAL,
    cooling_setpoint_c REAL,
    UNIQUE (run_id, timestep_key, zone_name)
);

CREATE INDEX IF NOT EXISTS idx_zone_telemetry_run_simulation_time
    ON zone_telemetry(run_id, simulation_timestamp);

CREATE TABLE IF NOT EXISTS observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    simulation_timestamp TEXT NOT NULL,
    timestep_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (run_id, timestep_key)
);

CREATE INDEX IF NOT EXISTS idx_observations_run_observation
    ON observations(run_id, observation_id DESC);

CREATE TABLE IF NOT EXISTS proposed_actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES observations(observation_id),
    action_generation INTEGER NOT NULL CHECK (action_generation >= 1),
    proposed_values_json TEXT NOT NULL,
    applied_values_json TEXT,
    validation_result_json TEXT,
    clamp_details_json TEXT NOT NULL DEFAULT '[]',
    expiry TEXT NOT NULL,
    model TEXT NOT NULL,
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0.0),
    reason_code TEXT NOT NULL,
    explanation TEXT NOT NULL,
    fallback_status TEXT,
    cache_hit INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit IN (0, 1)),
    UNIQUE (run_id, observation_id, action_generation)
);

CREATE INDEX IF NOT EXISTS idx_proposed_actions_run_observation
    ON proposed_actions(run_id, observation_id DESC, action_generation DESC);

CREATE TABLE IF NOT EXISTS applied_actions (
    action_id TEXT PRIMARY KEY REFERENCES proposed_actions(action_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES observations(observation_id),
    action_generation INTEGER NOT NULL CHECK (action_generation >= 1),
    proposed_values_json TEXT NOT NULL,
    applied_values_json TEXT NOT NULL,
    validation_result_json TEXT NOT NULL,
    clamp_details_json TEXT NOT NULL,
    expiry TEXT NOT NULL,
    model TEXT NOT NULL,
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0.0),
    reason_code TEXT NOT NULL,
    explanation TEXT NOT NULL,
    fallback_status TEXT,
    UNIQUE (run_id, action_generation),
    UNIQUE (run_id, observation_id, action_generation)
);

CREATE INDEX IF NOT EXISTS idx_applied_actions_run_generation
    ON applied_actions(run_id, action_generation DESC);

CREATE TABLE IF NOT EXISTS agent_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES observations(observation_id),
    action_generation INTEGER,
    model TEXT NOT NULL,
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0.0),
    reason_code TEXT,
    explanation TEXT,
    fallback_status TEXT,
    candidate_scores_json TEXT NOT NULL DEFAULT '[]',
    state_summary_json TEXT NOT NULL DEFAULT '{}',
    completed INTEGER NOT NULL CHECK (completed IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_run_time
    ON agent_decisions(run_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    observation_id INTEGER REFERENCES observations(observation_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    error TEXT,
    duration_ms REAL NOT NULL CHECK (duration_ms >= 0.0),
    control_affecting INTEGER NOT NULL DEFAULT 0 CHECK (control_affecting IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_time
    ON tool_calls(run_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS simulation_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    severity TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    message TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (run_id, severity, message_hash)
);

CREATE INDEX IF NOT EXISTS idx_simulation_messages_run_severity
    ON simulation_messages(run_id, severity, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    severity TEXT NOT NULL,
    error_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, severity, error_hash)
);

CREATE INDEX IF NOT EXISTS idx_errors_run_severity
    ON errors(run_id, severity, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS metrics (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL,
    value_json TEXT,
    units TEXT,
    source TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    UNIQUE (run_id, metric_name)
);

CREATE TABLE IF NOT EXISTS run_artifacts (
    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, artifact_type, path)
);

CREATE INDEX IF NOT EXISTS idx_run_artifacts_run
    ON run_artifacts(run_id, artifact_type);
