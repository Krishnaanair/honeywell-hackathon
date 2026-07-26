CREATE TABLE actuator_applications (
    physical_application_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    simulation_timestamp TEXT NOT NULL,
    action_id TEXT,
    observation_id INTEGER NOT NULL CHECK (observation_id >= 1),
    action_generation INTEGER NOT NULL CHECK (action_generation >= 1),
    heating_setpoint_c REAL NOT NULL,
    cooling_setpoint_c REAL NOT NULL,
    hold_minutes INTEGER NOT NULL CHECK (hold_minutes >= 1),
    simulation_expires_at TEXT NOT NULL,
    wall_expires_at TEXT,
    validation_result TEXT NOT NULL,
    fallback_status TEXT,
    source TEXT NOT NULL,
    acknowledgement_source TEXT NOT NULL
        CHECK (acknowledgement_source = 'runtime_callback'),
    heating_actuator_handles_json TEXT NOT NULL,
    cooling_actuator_handles_json TEXT NOT NULL,
    UNIQUE (run_id, action_generation)
);

CREATE INDEX idx_actuator_applications_run_simulation_time
    ON actuator_applications(run_id, simulation_timestamp, action_generation);

CREATE INDEX idx_actuator_applications_action
    ON actuator_applications(run_id, action_id);
