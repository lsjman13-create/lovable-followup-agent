-- Migration 001 — initial schema
-- ARCHITECTURE §5.2 참조.

CREATE TABLE IF NOT EXISTS processed_files (
    file_hash    TEXT PRIMARY KEY,
    file_name    TEXT NOT NULL,
    processed_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS send_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL,
    chatroom_title    TEXT NOT NULL,
    message           TEXT NOT NULL,
    scheduled_at      TIMESTAMP NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued',
    attempted_count   INTEGER NOT NULL DEFAULT 0,
    last_attempted_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_send_queue_status_scheduled
    ON send_queue (status, scheduled_at);

CREATE TABLE IF NOT EXISTS send_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    send_queue_id   INTEGER NOT NULL REFERENCES send_queue(id),
    sent_at         TIMESTAMP NOT NULL,
    success         BOOLEAN NOT NULL,
    error_detail    TEXT,
    screenshot_path TEXT
);

CREATE TABLE IF NOT EXISTS whitelist_cache (
    chatroom_title TEXT PRIMARY KEY,
    active         BOOLEAN NOT NULL,
    cached_at      TIMESTAMP NOT NULL
);

-- 마이그레이션 추적
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL
);
