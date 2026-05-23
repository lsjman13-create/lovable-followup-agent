"""SendDispatcher 통합 테스트 — fake KakaoSender 로 흐름 검증."""

from __future__ import annotations

from datetime import datetime

import pytest

from lovable_agent.domain import SendQueueItem, WindowSpec
from lovable_agent.output.kakao_sender import SendResult
from lovable_agent.output.notifier import Notifier
from lovable_agent.output.send_dispatcher import SendDispatcher
from lovable_agent.storage.sqlite_repo import SqliteRepository


class _FakeSender:
    """결정적 응답을 돌려주는 가짜 KakaoSender."""

    def __init__(self, *, always_success: bool = True, fail_at_step: str | None = None) -> None:
        self.always_success = always_success
        self.fail_at_step = fail_at_step
        self.calls: list[tuple[WindowSpec, str]] = []

    def send(self, target: WindowSpec, message: str) -> SendResult:
        self.calls.append((target, message))
        if self.always_success:
            return SendResult(
                success=True,
                completed_steps=["s1", "s2", "s3", "s4", "s5", "s6"],
            )
        return SendResult(
            success=False,
            completed_steps=["s1"],
            failed_step=self.fail_at_step or "s2",
            error_reason="의도된 실패",
        )


@pytest.fixture()
def sqlite():
    r = SqliteRepository(":memory:")
    yield r
    r.close()


def _enqueue(sqlite: SqliteRepository, task_id: str, chatroom: str, message: str) -> int:
    return sqlite.enqueue_send(
        SendQueueItem(
            task_id=task_id,
            chatroom=WindowSpec(title_exact=chatroom),
            message=message,
            scheduled_at=datetime(2026, 5, 23, 12, 0),
        )
    )


# ──────────────────────────────────────────────────────────────
# 성공 시나리오
# ──────────────────────────────────────────────────────────────
def test_dispatch_empty_queue(sqlite):
    sender = _FakeSender()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender)
    outcome = dispatcher.dispatch_pending()
    assert outcome.attempted == 0
    assert outcome.succeeded == 0
    assert sender.calls == []


def test_dispatch_single_success(sqlite):
    qid = _enqueue(sqlite, "t1", "MOP 운영방", "[AI 자동 팔로우업] 테스트")
    sender = _FakeSender(always_success=True)
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender)

    outcome = dispatcher.dispatch_pending()
    assert outcome.attempted == 1
    assert outcome.succeeded == 1
    assert outcome.failed == 0
    assert outcome.sent_queue_ids == [qid]

    # 큐 상태 sent 로 갱신
    pending_after = sqlite.list_pending()
    assert pending_after == []  # 'queued' 가 아니라 'sent'

    # send_history 기록
    history = sqlite.list_history()
    assert len(history) == 1
    assert history[0]["success"] == 1


def test_dispatch_multiple_success(sqlite):
    _enqueue(sqlite, "t1", "방A", "메시지 1")
    _enqueue(sqlite, "t2", "방B", "메시지 2")
    _enqueue(sqlite, "t3", "방C", "메시지 3")
    sender = _FakeSender(always_success=True)
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender)

    outcome = dispatcher.dispatch_pending()
    assert outcome.attempted == 3
    assert outcome.succeeded == 3
    assert len(sender.calls) == 3
    # 호출 메시지 검증
    assert sender.calls[0][1] == "메시지 1"
    assert sender.calls[2][1] == "메시지 3"


# ──────────────────────────────────────────────────────────────
# 실패 시나리오
# ──────────────────────────────────────────────────────────────
def test_dispatch_failure_records_failure_and_notifies(sqlite):
    qid = _enqueue(sqlite, "t1", "방A", "메시지")
    sender = _FakeSender(always_success=False, fail_at_step="verify_chatroom_title")
    notifier = Notifier()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender, notifier=notifier)

    outcome = dispatcher.dispatch_pending()
    assert outcome.attempted == 1
    assert outcome.failed == 1
    assert outcome.failed_queue_ids == [qid]

    # 큐 상태 failed
    with sqlite._conn() as conn:  # noqa: SLF001
        row = conn.execute("SELECT * FROM send_queue WHERE id = ?", (qid,)).fetchone()
    assert row["status"] == "failed"
    assert row["attempted_count"] == 1

    # send_history 에 실패 기록
    history = sqlite.list_history()
    assert len(history) == 1
    assert history[0]["success"] == 0
    assert "verify_chatroom_title" in (history[0]["error_detail"] or "")

    # Notifier 가 error 로 호출됨
    assert notifier.last_notification is not None
    assert notifier.last_notification.title == "자동 발송 실패"


def test_dispatch_failure_without_notifier_does_not_crash(sqlite):
    """Notifier 가 없어도 (예: 테스트) 안전하게 동작."""
    _enqueue(sqlite, "t1", "방A", "메시지")
    sender = _FakeSender(always_success=False)
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender, notifier=None)

    outcome = dispatcher.dispatch_pending()
    assert outcome.failed == 1  # 그래도 dispatch 자체는 정상


# ──────────────────────────────────────────────────────────────
# 데이터 무결성 케이스
# ──────────────────────────────────────────────────────────────
def test_dispatch_skips_empty_message(sqlite):
    _enqueue(sqlite, "t1", "방A", "")  # 빈 메시지
    sender = _FakeSender()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender)

    outcome = dispatcher.dispatch_pending()
    assert outcome.skipped_no_message == 1
    assert sender.calls == []  # 발송 시도 안 함


def test_dispatch_skips_empty_chatroom(sqlite):
    _enqueue(sqlite, "t1", "", "메시지")  # 빈 톡방명
    sender = _FakeSender()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender)

    outcome = dispatcher.dispatch_pending()
    assert outcome.skipped_no_message == 1
    assert sender.calls == []


# ──────────────────────────────────────────────────────────────
# 배치 제한
# ──────────────────────────────────────────────────────────────
def test_dispatch_respects_batch_limit(sqlite):
    for i in range(15):
        _enqueue(sqlite, f"t{i}", f"방{i}", f"메시지 {i}")
    sender = _FakeSender()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender, batch_limit=5)

    outcome = dispatcher.dispatch_pending()
    assert outcome.attempted == 5
    # 나머지 10개는 여전히 queued
    pending = sqlite.list_pending(limit=100)
    assert len(pending) == 10


# ──────────────────────────────────────────────────────────────
# Scheduler ↔ Dispatcher 통합 (핵심)
# ──────────────────────────────────────────────────────────────
def test_scheduler_to_dispatcher_full_flow(sqlite):
    """Scheduler.tick 이 enqueue 한 항목을 Dispatcher 가 처리하는 통합 흐름."""
    from lovable_agent.scheduling.scheduler import ReminderScheduler
    from lovable_agent.storage.mock_notion_repo import MockNotionRepository

    notion = MockNotionRepository()
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    sender = _FakeSender(always_success=True)
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender)

    # 1) Scheduler.tick — 시드 업무가 발송 큐에 enqueue
    tick_outcome = scheduler.tick()
    assert tick_outcome.enqueued >= 1

    # 2) Dispatcher.dispatch_pending — 발송 처리
    dispatch_outcome = dispatcher.dispatch_pending()
    assert dispatch_outcome.attempted >= 1
    assert dispatch_outcome.succeeded == dispatch_outcome.attempted

    # 3) 모든 메시지에 접두어 존재 검증 (KakaoSender 가 enforce_prefix 하지만 여기선
    #    fake sender 라 호출 메시지에 prepend 안 됨. 대신 Scheduler 가 빌드 시 prepend)
    for _target, msg in sender.calls:
        assert msg.startswith("[AI 자동 팔로우업] ")
