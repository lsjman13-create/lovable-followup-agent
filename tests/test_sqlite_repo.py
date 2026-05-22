"""SqliteRepository 단위 테스트 — 인메모리 / 파일 둘 다."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lovable_agent.domain import SendQueueItem, WindowSpec
from lovable_agent.storage.sqlite_repo import SqliteRepository


@pytest.fixture()
def repo():
    r = SqliteRepository(":memory:")
    yield r
    r.close()


def _make_item(task_id: str = "t1", chatroom: str = "MOP 운영방") -> SendQueueItem:
    return SendQueueItem(
        task_id=task_id,
        chatroom=WindowSpec(title_exact=chatroom),
        message="[AI 자동 팔로우업] 테스트",
        scheduled_at=datetime(2026, 5, 23, 15, 0),
    )


# ──────────────────────────────────────────────────────────────
# Migration / 스키마
# ──────────────────────────────────────────────────────────────
def test_migration_creates_all_tables(repo):
    """초기 마이그레이션으로 5개 테이블이 생성되어야 함."""
    with repo._conn() as conn:  # noqa: SLF001 — 테스트에서만 내부 접근
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    expected = {
        "processed_files",
        "send_queue",
        "send_history",
        "whitelist_cache",
        "schema_version",
    }
    assert expected.issubset(names)


def test_migration_records_version(repo):
    with repo._conn() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert rows[0][0] == 1


def test_migration_idempotent(tmp_path):
    """같은 파일 DB 를 두 번 열어도 마이그레이션이 중복 적용되지 않음."""
    db = tmp_path / "agent.db"
    SqliteRepository(db)
    SqliteRepository(db)
    repo3 = SqliteRepository(db)
    try:
        with repo3._conn() as conn:  # noqa: SLF001
            count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert count == 1
    finally:
        repo3.close()


# ──────────────────────────────────────────────────────────────
# processed_files
# ──────────────────────────────────────────────────────────────
def test_processed_files_mark_and_check(repo):
    assert repo.is_file_processed("abc") is False
    repo.mark_file_processed("abc", "chat.txt")
    assert repo.is_file_processed("abc") is True


def test_processed_files_idempotent(repo):
    """같은 해시 두 번 mark — 에러 없이 멱등."""
    repo.mark_file_processed("abc", "chat.txt")
    repo.mark_file_processed("abc", "chat.txt")
    assert repo.is_file_processed("abc") is True


# ──────────────────────────────────────────────────────────────
# send_queue
# ──────────────────────────────────────────────────────────────
def test_enqueue_and_list_pending(repo):
    item = _make_item()
    qid = repo.enqueue_send(item)
    assert qid > 0

    pending = repo.list_pending()
    assert len(pending) == 1
    assert pending[0]["task_id"] == "t1"
    assert pending[0]["status"] == "queued"


def test_list_pending_returns_in_scheduled_order(repo):
    repo.enqueue_send(
        SendQueueItem(
            task_id="late",
            chatroom=WindowSpec(title_exact="A"),
            message="x",
            scheduled_at=datetime(2026, 5, 23, 18, 0),
        )
    )
    repo.enqueue_send(
        SendQueueItem(
            task_id="early",
            chatroom=WindowSpec(title_exact="A"),
            message="x",
            scheduled_at=datetime(2026, 5, 23, 10, 0),
        )
    )
    pending = repo.list_pending()
    assert [p["task_id"] for p in pending] == ["early", "late"]


def test_update_send_status(repo):
    qid = repo.enqueue_send(_make_item())
    repo.update_send_status(qid, "sent", increment_attempt=True)
    pending = repo.list_pending()
    assert pending == []  # queued 가 아니라 sent

    with repo._conn() as conn:  # noqa: SLF001
        row = conn.execute("SELECT * FROM send_queue WHERE id = ?", (qid,)).fetchone()
    assert row["status"] == "sent"
    assert row["attempted_count"] == 1
    assert row["last_attempted_at"] is not None


def test_is_already_queued_detects_duplicate(repo):
    when = datetime(2026, 5, 23, 15, 0)
    repo.enqueue_send(
        SendQueueItem(
            task_id="t1",
            chatroom=WindowSpec(title_exact="A"),
            message="x",
            scheduled_at=when,
        )
    )
    assert repo.is_already_queued("t1", when) is True
    assert repo.is_already_queued("t1", when + timedelta(hours=1)) is False
    assert repo.is_already_queued("t2", when) is False


# ──────────────────────────────────────────────────────────────
# send_history
# ──────────────────────────────────────────────────────────────
def test_record_send_attempt(repo):
    qid = repo.enqueue_send(_make_item())
    hid = repo.record_send_attempt(qid, success=True, screenshot_path="/tmp/s.png")
    assert hid > 0
    history = repo.list_history()
    assert len(history) == 1
    assert history[0]["success"] == 1  # SQLite bool → int
    assert history[0]["screenshot_path"] == "/tmp/s.png"


def test_record_send_attempt_failure(repo):
    qid = repo.enqueue_send(_make_item())
    repo.record_send_attempt(qid, success=False, error_detail="윈도우 못 찾음")
    history = repo.list_history()
    assert history[0]["success"] == 0
    assert history[0]["error_detail"] == "윈도우 못 찾음"


# ──────────────────────────────────────────────────────────────
# whitelist_cache
# ──────────────────────────────────────────────────────────────
def test_cache_whitelist_replaces_entries(repo):
    repo.cache_whitelist([WindowSpec(title_exact="MOP 운영방")])
    assert repo.is_chatroom_in_cache("MOP 운영방") is True
    assert repo.is_chatroom_in_cache("다른방") is False

    # 새 목록으로 교체 시 기존이 사라져야 함
    repo.cache_whitelist([WindowSpec(title_exact="GGE 운영방")])
    assert repo.is_chatroom_in_cache("MOP 운영방") is False
    assert repo.is_chatroom_in_cache("GGE 운영방") is True


def test_cache_age_when_empty(repo):
    assert repo.whitelist_cache_age_seconds() is None


def test_cache_age_after_caching(repo):
    repo.cache_whitelist([WindowSpec(title_exact="A")])
    age = repo.whitelist_cache_age_seconds()
    assert age is not None
    assert age < 5  # 방금 캐싱했으니 매우 작아야 함


# ──────────────────────────────────────────────────────────────
# 파일 DB 모드
# ──────────────────────────────────────────────────────────────
def test_file_db_persists_across_instances(tmp_path):
    db = tmp_path / "agent.db"
    r1 = SqliteRepository(db)
    r1.mark_file_processed("abc", "x.txt")
    r1.close()

    r2 = SqliteRepository(db)
    try:
        assert r2.is_file_processed("abc") is True
    finally:
        r2.close()
