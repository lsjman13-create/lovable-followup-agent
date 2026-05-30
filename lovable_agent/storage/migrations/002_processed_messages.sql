-- 002_processed_messages.sql
-- 중복 분석 방지를 위한 카카오톡 메시지 해시 테이블

CREATE TABLE IF NOT EXISTS processed_messages (
    message_hash TEXT PRIMARY KEY,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
