-- Sift store, schema version 1.
-- One file holds everything: mail metadata, cleaned bodies, verdicts, cases.
-- It is the source of truth; spreadsheets are exports, never inputs.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    name        TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    address     TEXT NOT NULL DEFAULT '',
    cursor      TEXT NOT NULL DEFAULT '{}',   -- historyId / deltaLink / UIDVALIDITY+UIDNEXT
    created_at  TEXT NOT NULL,
    last_sync   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id       TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,
    account          TEXT NOT NULL,
    uid              TEXT,
    folder           TEXT,
    thread_id        TEXT,
    in_reply_to      TEXT,
    refs             TEXT NOT NULL DEFAULT '[]',
    from_name        TEXT NOT NULL DEFAULT '',
    from_addr        TEXT NOT NULL DEFAULT '',
    from_domain      TEXT NOT NULL DEFAULT '',
    to_addrs         TEXT NOT NULL DEFAULT '[]',
    cc_addrs         TEXT NOT NULL DEFAULT '[]',
    reply_to         TEXT,
    date_utc         TEXT NOT NULL,
    subject          TEXT NOT NULL DEFAULT '',
    subject_norm     TEXT NOT NULL DEFAULT '',
    list_id          TEXT,
    list_unsubscribe TEXT,
    precedence       TEXT,
    auto_submitted   TEXT,
    x_mailer         TEXT,
    auth_results     TEXT,
    return_path      TEXT,
    has_html         INTEGER NOT NULL DEFAULT 0,
    language         TEXT NOT NULL DEFAULT 'und',
    attachments      TEXT NOT NULL DEFAULT '[]',
    direction        TEXT NOT NULL DEFAULT 'inbound',
    headers          TEXT NOT NULL DEFAULT '{}',
    simhash          INTEGER NOT NULL DEFAULT 0,
    ingested_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_date     ON messages(date_utc DESC);
CREATE INDEX IF NOT EXISTS idx_messages_domain   ON messages(from_domain);
CREATE INDEX IF NOT EXISTS idx_messages_thread   ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_account  ON messages(account);
CREATE INDEX IF NOT EXISTS idx_messages_dir      ON messages(direction);
CREATE INDEX IF NOT EXISTS idx_messages_simhash  ON messages(simhash);

CREATE TABLE IF NOT EXISTS bodies (
    message_id    TEXT PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
    body_text     TEXT NOT NULL DEFAULT '',
    body_raw_text TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    message_id UNINDEXED,
    subject,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Contact graph. "Have I ever written to this address?" is the strongest single
-- importance feature in the system and it costs one indexed lookup.
CREATE TABLE IF NOT EXISTS contacts (
    address       TEXT PRIMARY KEY,
    domain        TEXT NOT NULL DEFAULT '',
    display_name  TEXT NOT NULL DEFAULT '',
    sent_count    INTEGER NOT NULL DEFAULT 0,
    received_count INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT,
    last_seen     TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain);

CREATE TABLE IF NOT EXISTS verdicts (
    message_id     TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    pack           TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'other',
    action_required INTEGER NOT NULL DEFAULT 0,
    urgency        TEXT NOT NULL DEFAULT 'none',
    next_action    TEXT NOT NULL DEFAULT 'none',
    summary        TEXT NOT NULL DEFAULT '',
    org            TEXT,
    org_domain     TEXT,
    role           TEXT,
    contact_person TEXT,
    event_datetime TEXT,
    deadline       TEXT,
    amount         TEXT,
    evidence       TEXT,
    evidence_ok    INTEGER,
    confidence     REAL NOT NULL DEFAULT 0,
    stage          TEXT NOT NULL DEFAULT 'rules',
    lane           TEXT NOT NULL DEFAULT 'candidate',
    model          TEXT,
    template_id    TEXT,
    reasons        TEXT NOT NULL DEFAULT '[]',
    needs_review   INTEGER NOT NULL DEFAULT 0,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (message_id, pack)
);
CREATE INDEX IF NOT EXISTS idx_verdicts_pack   ON verdicts(pack, category);
CREATE INDEX IF NOT EXISTS idx_verdicts_action ON verdicts(pack, action_required);

CREATE TABLE IF NOT EXISTS cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pack            TEXT NOT NULL,
    key             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    org             TEXT,
    org_domain      TEXT,
    role            TEXT,
    state           TEXT NOT NULL DEFAULT '',
    opened_at       TEXT,
    last_event_at   TEXT,
    last_inbound_at TEXT,
    last_outbound_at TEXT,
    next_action     TEXT NOT NULL DEFAULT 'none',
    next_action_due TEXT,
    event_datetime  TEXT,
    contact_person  TEXT,
    message_count   INTEGER NOT NULL DEFAULT 0,
    stalled         INTEGER NOT NULL DEFAULT 0,
    closed          INTEGER NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT '',
    UNIQUE (pack, key)
);
CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(pack, state);

CREATE TABLE IF NOT EXISTS case_messages (
    case_id    INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    PRIMARY KEY (case_id, message_id)
);

CREATE TABLE IF NOT EXISTS case_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id    INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    message_id TEXT,
    at         TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT,
    category   TEXT,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_case_events_case ON case_events(case_id, at);

-- Planned provider labels. Sift never applies a label without an explicit,
-- non-default command; this table is the reviewable plan.
CREATE TABLE IF NOT EXISTS label_plan (
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    applied    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY (message_id, label)
);

-- Every correction a human makes becomes a training row. This is what makes
-- the classifier personal within a week.
CREATE TABLE IF NOT EXISTS training (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    pack            TEXT NOT NULL,
    category        TEXT NOT NULL,
    action_required INTEGER,
    source          TEXT NOT NULL DEFAULT 'user',  -- user | distilled | seed
    created_at      TEXT NOT NULL,
    UNIQUE (message_id, pack, source)
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pack       TEXT NOT NULL DEFAULT '',
    processed  INTEGER NOT NULL DEFAULT 0,
    by_stage   TEXT NOT NULL DEFAULT '{}',
    notes      TEXT NOT NULL DEFAULT ''
);
