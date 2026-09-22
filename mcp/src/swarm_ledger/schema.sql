CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY,
    prd TEXT,
    state TEXT NOT NULL,
    plugin_version TEXT,
    settings_json TEXT,
    outcome TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS phases (
    phase_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS phase_deps (
    phase_id INTEGER NOT NULL REFERENCES phases (phase_id) ON DELETE RESTRICT,
    depends_on_phase_id INTEGER NOT NULL REFERENCES phases (phase_id) ON DELETE RESTRICT,
    PRIMARY KEY (phase_id, depends_on_phase_id)
);

CREATE TABLE IF NOT EXISTS modules (
    module_id INTEGER PRIMARY KEY,
    phase_id INTEGER NOT NULL REFERENCES phases (phase_id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY,
    module_id INTEGER NOT NULL REFERENCES modules (module_id) ON DELETE RESTRICT,
    path TEXT NOT NULL,
    test_path TEXT,
    -- No REFERENCES agents(agent_id): claim_file records the owner's name before
    -- the owner agent exists. brief_ack binds the name to an agent_id later.
    owner_agent_id TEXT,
    state TEXT NOT NULL,
    claimed_at TEXT,
    released_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_files_path_live
    ON files (path) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    runtime TEXT,
    model TEXT,
    effort TEXT,
    settings_json TEXT,
    parent_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    run_id INTEGER REFERENCES runs (run_id) ON DELETE RESTRICT,
    phase_id INTEGER REFERENCES phases (phase_id) ON DELETE RESTRICT,
    module_id INTEGER REFERENCES modules (module_id) ON DELETE RESTRICT,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    state TEXT NOT NULL,
    current_activity TEXT,
    phase_at_start TEXT,
    phase_at_end TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at TEXT,
    end_reason TEXT,
    last_heartbeat_at TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    context_pct_peak REAL,
    context_pct_at_end REAL,
    context_overflow_count INTEGER DEFAULT 0,
    tool_uses INTEGER,
    duration_ms INTEGER,
    transcript_path TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_name_live
    ON agents (name) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS briefs (
    brief_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    parent_agent_id TEXT NOT NULL REFERENCES agents (agent_id) ON DELETE RESTRICT,
    child_name TEXT NOT NULL,
    child_role TEXT NOT NULL,
    model TEXT,
    body TEXT NOT NULL,
    phase_id INTEGER REFERENCES phases (phase_id) ON DELETE RESTRICT,
    module_id INTEGER REFERENCES modules (module_id) ON DELETE RESTRICT,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    acked_by_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    acked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    module_id INTEGER REFERENCES modules (module_id) ON DELETE RESTRICT,
    phase_id INTEGER REFERENCES phases (phase_id) ON DELETE RESTRICT,
    reviewer_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    subject_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    outcome TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS scores (
    score_id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews (review_id) ON DELETE RESTRICT,
    dimension TEXT NOT NULL,
    criterion TEXT NOT NULL,
    value INTEGER NOT NULL CHECK (value BETWEEN 1 AND 10),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    opened_by_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    body TEXT,
    state TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    escalated_to TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS change_requests (
    cr_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    from_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    to_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    state TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS test_runs (
    test_run_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    scope TEXT NOT NULL,
    target TEXT,
    command TEXT,
    exit_code INTEGER,
    passed INTEGER,
    failed INTEGER,
    skipped INTEGER,
    output TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS guidelines (
    guideline_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS departures (
    departure_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    guideline_id INTEGER REFERENCES guidelines (guideline_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (file_id) ON DELETE RESTRICT,
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    content BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    from_name TEXT NOT NULL,
    to_name TEXT NOT NULL,
    body TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS directives (
    directive_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    source TEXT NOT NULL,
    sender_name TEXT,
    body TEXT NOT NULL,
    reply_to INTEGER REFERENCES directives (directive_id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'open',
    outcome TEXT,
    resolved_at TEXT,
    resolution TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS overrides (
    override_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    rule TEXT NOT NULL,
    target_agent_name TEXT NOT NULL,
    target TEXT,
    reason TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS ideas (
    idea_id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL REFERENCES issues (issue_id) ON DELETE RESTRICT,
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    body TEXT NOT NULL,
    outcome TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS agent_events (
    event_id INTEGER PRIMARY KEY,
    agent_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
