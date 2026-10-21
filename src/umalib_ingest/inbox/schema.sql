-- Inbox database schema (TASK-002). Schema version is tracked by PRAGMA user_version = 1.

CREATE TABLE IF NOT EXISTS inbox_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('candidate','incomplete','suspected_duplicate','approved','exported','ignored')),
    original_draft_json TEXT NOT NULL,
    working_draft_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL UNIQUE,
    content_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES inbox_item(id),
    from_status TEXT CHECK (from_status IS NULL OR from_status IN ('candidate','incomplete','suspected_duplicate','approved','exported','ignored')),
    to_status TEXT NOT NULL CHECK (to_status IN ('candidate','incomplete','suspected_duplicate','approved','exported','ignored')),
    reason TEXT NOT NULL CHECK (
        length(trim(reason, char(9) || char(10) || char(13) || ' ')) > 0
    ),
    changed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inbox_item_status ON inbox_item(status);
CREATE INDEX IF NOT EXISTS idx_inbox_item_content_sha256 ON inbox_item(content_sha256);
