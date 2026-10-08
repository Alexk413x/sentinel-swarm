CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY,
    prd TEXT,
    state TEXT NOT NULL,
    plugin_version TEXT,
    settings_json TEXT,
    outcome TEXT,
    -- The Oracle's most recent repo_check result and the branch repo_branch_create made.
    repo_check_json TEXT,
    repo_checked_at TEXT,
    branch TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at TEXT,
    watch_heartbeat_at TEXT,
    watch_expires_at TEXT,
    watch_owner TEXT
);

CREATE TABLE IF NOT EXISTS phases (
    phase_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Set when phase_update moves the phase to handed_up. phase_review and the
    -- approved gate both measure freshness against this timestamp.
    handed_up_at TEXT,
    ended_at TEXT,
    -- Set by run_pause(phases=[...]) for a scoped pause; NULL for a phase that keeps going.
    paused_at TEXT,
    pause_reason TEXT
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
    -- Set by hook 7 after a Coder's edit. handoff_submit refuses a self review
    -- older than this, so a stale review can never wave through a later edit.
    stale_since TEXT,
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
    cost_usd REAL,
    context_pct_peak REAL,
    context_pct_at_end REAL,
    context_overflow_count INTEGER DEFAULT 0,
    tool_uses INTEGER,
    duration_ms INTEGER,
    transcript_path TEXT,
    session_name TEXT,
    bg_id TEXT
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
    effort TEXT,
    body TEXT NOT NULL,
    phase_id INTEGER REFERENCES phases (phase_id) ON DELETE RESTRICT,
    module_id INTEGER REFERENCES modules (module_id) ON DELETE RESTRICT,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    -- The drive_findings this brief fixes, as a JSON list of finding ids. A child's brief
    -- inherits its parent's list unless brief_create names its own.
    finding_ids_json TEXT,
    -- The public contract of the child's file or module. A dependent's brief needs one on
    -- each dependency's latest brief.
    contract TEXT,
    -- Set by the pre_ledger hook when the child's own session calls brief_get.
    last_read_by_child_at TEXT,
    acked_by_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    acked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS module_deps (
    module_id INTEGER NOT NULL REFERENCES modules (module_id) ON DELETE RESTRICT,
    depends_on_module_id INTEGER NOT NULL REFERENCES modules (module_id) ON DELETE RESTRICT,
    PRIMARY KEY (module_id, depends_on_module_id)
);

CREATE TABLE IF NOT EXISTS file_deps (
    file_id INTEGER NOT NULL REFERENCES files (file_id) ON DELETE RESTRICT,
    depends_on_file_id INTEGER NOT NULL REFERENCES files (file_id) ON DELETE RESTRICT,
    PRIMARY KEY (file_id, depends_on_file_id)
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
    applicable_json TEXT,
    -- For kind='manager': {"disagreement_notes": {file_id: note}}.
    -- For kind='oracle': {"low_score_notes": {file_id: note}}.
    details_json TEXT,
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
    dimension TEXT,
    criterion TEXT,
    closed_by_agent_id TEXT,
    resolution TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS change_requests (
    cr_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    from_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    to_agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    path TEXT,
    state TEXT NOT NULL,
    body TEXT NOT NULL,
    decision_reason TEXT,
    accepted_at TEXT,
    completed_at TEXT,
    completion_notes TEXT,
    evidence_test_run_id INTEGER REFERENCES test_runs (test_run_id) ON DELETE RESTRICT,
    verified_at TEXT,
    verify_notes TEXT,
    decided_at TEXT,
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
    -- The working tree's state when the run started, and the earlier run a reuse copies.
    fingerprint TEXT,
    reused_from INTEGER REFERENCES test_runs (test_run_id) ON DELETE RESTRICT,
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
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    handoff_id INTEGER REFERENCES handoffs (handoff_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    body TEXT NOT NULL,
    decided_by TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    decided_at TEXT,
    decision_reason TEXT,
    solution TEXT,
    -- The role whose decision is next: lead, manager, or oracle. NULL once no decision
    -- is pending. departure_decisions holds every decision made.
    level TEXT,
    signed_off_at TEXT,
    reworked_at TEXT,
    reworked_by_handoff_id INTEGER REFERENCES handoffs (handoff_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS departure_decisions (
    decision_id INTEGER PRIMARY KEY,
    departure_id INTEGER NOT NULL REFERENCES departures (departure_id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    solution TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (file_id) ON DELETE RESTRICT,
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    content BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    -- Path to the human-readable file copy, relative to the records folder.
    -- The BLOB above is authoritative; this is what version_restore reads.
    stored_path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (file_id) ON DELETE RESTRICT,
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    test_run_id INTEGER REFERENCES test_runs (test_run_id) ON DELETE RESTRICT,
    version_id INTEGER REFERENCES versions (version_id) ON DELETE RESTRICT,
    test_version_id INTEGER REFERENCES versions (version_id) ON DELETE RESTRICT,
    self_review_id INTEGER REFERENCES reviews (review_id) ON DELETE RESTRICT,
    open_issues_json TEXT,
    departures_json TEXT,
    state TEXT NOT NULL,
    compared_at TEXT,
    decided_notes TEXT,
    -- Dimensions approve() accepted below target but at or above the floor, once the
    -- file's last escalation round ran out. NULL when approval was a full pass.
    floor_pass_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at TEXT,
    decided_by TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (file_id) ON DELETE RESTRICT,
    handoff_id INTEGER REFERENCES handoffs (handoff_id) ON DELETE RESTRICT,
    issue_ids_json TEXT,
    targeted_json TEXT,
    outcome TEXT,
    round INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS deferrals (
    deferral_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    file_id INTEGER REFERENCES files (file_id) ON DELETE RESTRICT,
    proposed_by TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    -- file, module, cross_module, phase, plan, or prd: the level that decides it.
    kind TEXT,
    -- A dispute: the agent names on the other side, and the closest shared ancestor who
    -- alone decides it.
    parties_json TEXT,
    arbiter_agent_id TEXT,
    -- For accept_incomplete: the Coder's open issue strings and the file's open issue ids.
    open_issues_json TEXT,
    issue_ids_json TEXT,
    -- For a prd deferral: the user_chat directive the decision rests on.
    directive_id INTEGER REFERENCES directives (directive_id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'open',
    decided_by TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    decision_reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at TEXT
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

CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages (run_id, to_name, read_at);

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
    notified_at TEXT,
    -- Set when outcome is set to needs_user, and left in place after a later
    -- resolve overwrites outcome/resolution, so the report can still show what
    -- was asked. report_build's notifications section reads this column.
    question TEXT,
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

CREATE TABLE IF NOT EXISTS wakeups (
    wakeup_id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES runs (run_id) ON DELETE RESTRICT,
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    to_name TEXT NOT NULL,
    to_session_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    pointer TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    sent_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_wakeups_owed
    ON wakeups (from_agent_id) WHERE sent_at IS NULL;

CREATE TABLE IF NOT EXISTS watchdog_findings (
    finding_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    reported_at TEXT,
    directive_id INTEGER REFERENCES directives (directive_id) ON DELETE RESTRICT,
    cleared_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_watchdog_findings_live
    ON watchdog_findings (run_id, agent_id, kind) WHERE cleared_at IS NULL;

CREATE TABLE IF NOT EXISTS drive_requests (
    request_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    -- driver-e<ordinal> is the exploration number in this run's session name.
    ordinal INTEGER NOT NULL,
    opened_by TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    focus TEXT NOT NULL,
    -- The Driver session drive_request starts for this exploration; set once agent_spawn
    -- returns, so it is NULL for the instant between the two calls.
    agent_id TEXT REFERENCES agents (agent_id) ON DELETE RESTRICT,
    -- open, done (drive_done), or abandoned (the Driver was released, or drive_unavailable
    -- closed it). An abandoned exploration never counts as clean.
    state TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    done_at TEXT,
    last_checkin_at TEXT,
    last_covered TEXT,
    last_steps TEXT,
    last_notes TEXT,
    -- Set by the pre_skill hook when the Driver invokes map-test; map-explore waits on it.
    map_test_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_drive_requests_open
    ON drive_requests (run_id) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS drive_findings (
    finding_id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES drive_requests (request_id) ON DELETE RESTRICT,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    -- The check, the location, and what it saw: matches an issue across explorations.
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    steps TEXT,
    expected TEXT,
    actual TEXT,
    severity TEXT NOT NULL,
    area TEXT,
    -- Paths into cartographer's run folder, knowledge/cartographer/runs/<run-id>/.
    evidence_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_drive_findings_fingerprint
    ON drive_findings (run_id, fingerprint);

-- One row per fingerprint or area a Driver stop rule names. kind 'finding' stops fixes for
-- that fingerprint for the rest of the run; kind 'pattern' pauses fixes for that area and
-- fingerprint while its directive is open; kind 'stalled' ends the loop.
CREATE TABLE IF NOT EXISTS drive_stops (
    stop_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    directive_id INTEGER NOT NULL REFERENCES directives (directive_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    fingerprint TEXT,
    area TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_drive_stops_run
    ON drive_stops (run_id, kind);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    -- done, warning, or error.
    kind TEXT NOT NULL,
    -- One row per event: drive_done:<request_id>, directive:<directive_id>,
    -- drive_unavailable:<directive_id>, or issue:<issue_id> for an issue that ended its
    -- last round below the floor.
    event_key TEXT NOT NULL,
    message TEXT NOT NULL,
    -- 1 when the settings' notify list holds push: the run's Oracle owes a PushNotification.
    push_owed INTEGER NOT NULL DEFAULT 0,
    -- Set when the Oracle's watchdog Monitor printed the owed call.
    announced_at TEXT,
    -- Set by post_any on the Oracle's PushNotification call, whatever its result text.
    sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_event
    ON notifications (run_id, event_key);

-- One row per Grep or Glob a swarm role ran: what the code graph did not answer.
CREATE TABLE IF NOT EXISTS graph_gaps (
    gap_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs (run_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    pattern TEXT,
    path TEXT,
    results_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
