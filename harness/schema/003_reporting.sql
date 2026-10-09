CREATE TABLE feedback_items (
    world TEXT NOT NULL,
    item_id TEXT NOT NULL,
    after_batch TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(world, item_id)
);
CREATE INDEX feedback_decided_at ON feedback_items(decided_at);

CREATE TABLE audit_runs (
    input_hash TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    assessments_json TEXT NOT NULL
);

CREATE TABLE reports (
    report_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('supervised', 'autonomous')),
    input_hash TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE INDEX reports_date ON reports(as_of);

CREATE TABLE action_outbox (
    action_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(report_id),
    finding_id TEXT NOT NULL,
    action TEXT NOT NULL,
    recipient TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('recorded', 'draft', 'held', 'ready', 'superseded')),
    UNIQUE(report_id, finding_id)
);
PRAGMA user_version = 3;
