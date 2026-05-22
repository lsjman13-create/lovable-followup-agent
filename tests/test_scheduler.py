"""ReminderScheduler 단위 테스트 — 6시간 룰 + 이미 큐 회피 + 확정 필터."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lovable_agent.domain import TaskStatus, TaskSummary
from lovable_agent.scheduling.scheduler import (
    ReminderScheduler,
    classify_pending,
    compute_send_times,
    is_due_for_followup,
)
from lovable_agent.storage.mock_notion_repo import MockNotionRepository
from lovable_agent.storage.sqlite_repo import SqliteRepository


# ──────────────────────────────────────────────────────────────
# Pure 함수들
# ──────────────────────────────────────────────────────────────
def test_compute_send_times_default():
    due = datetime(2026, 6, 1, 15, 0)
    times = compute_send_times(due)
    assert times == [datetime(2026, 5, 31, 15, 0), datetime(2026, 6, 1, 15, 0)]


def test_compute_send_times_custom_offsets():
    due = datetime(2026, 6, 1, 15, 0)
    times = compute_send_times(due, offsets_hours=(48, 24, 4, 0))
    assert len(times) == 4
    # 정렬되어 있어야 함 (오래된 → 최근)
    assert times == sorted(times)
    assert times[-1] == due


def test_classify_pending_future_is_wait():
    now = datetime(2026, 5, 23, 10, 0)
    future = now + timedelta(hours=1)
    assert classify_pending(future, now) == "wait"


def test_classify_pending_within_threshold_is_send():
    now = datetime(2026, 5, 23, 10, 0)
    past_4h = now - timedelta(hours=4)
    assert classify_pending(past_4h, now, late_threshold_hours=6) == "send"


def test_classify_pending_over_threshold_is_skip():
    now = datetime(2026, 5, 23, 10, 0)
    past_7h = now - timedelta(hours=7)
    assert classify_pending(past_7h, now, late_threshold_hours=6) == "skip_too_late"


def test_classify_pending_exact_threshold_is_send():
    """경계값 — 정확히 6시간 지각은 send (≤ 검사)."""
    now = datetime(2026, 5, 23, 10, 0)
    past_6h = now - timedelta(hours=6)
    assert classify_pending(past_6h, now, late_threshold_hours=6) == "send"


# ──────────────────────────────────────────────────────────────
# is_due_for_followup
# ──────────────────────────────────────────────────────────────
def _make_task(**overrides) -> TaskSummary:
    base = dict(
        task_id="t1",
        title="X",
        assignee="A",
        due_date=datetime(2026, 6, 1, 15, 0),
        one_line_summary="x",
        status=TaskStatus.CONFIRMED,
        chatroom_title="MOP 운영방",
        followup_enabled=True,
    )
    base.update(overrides)
    return TaskSummary(**base)  # type: ignore[arg-type]


def test_is_due_for_followup_happy_path():
    assert is_due_for_followup(_make_task()) is True


def test_is_due_blocked_when_not_confirmed():
    assert is_due_for_followup(_make_task(status=TaskStatus.REVIEW_PENDING)) is False
    assert is_due_for_followup(_make_task(status=TaskStatus.DONE)) is False


def test_is_due_blocked_when_followup_disabled():
    assert is_due_for_followup(_make_task(followup_enabled=False)) is False


def test_is_due_blocked_when_no_due_date():
    assert is_due_for_followup(_make_task(due_date=None)) is False


def test_is_due_blocked_when_no_chatroom():
    assert is_due_for_followup(_make_task(chatroom_title="")) is False


# ──────────────────────────────────────────────────────────────
# tick() 통합 — 시간 주입
# ──────────────────────────────────────────────────────────────
@pytest.fixture()
def sqlite():
    r = SqliteRepository(":memory:")
    yield r
    r.close()


@pytest.fixture()
def notion():
    # MockNotionRepository 의 시드 데이터: 확정+MOP 운영방+due=now+10일
    return MockNotionRepository()


def test_tick_enqueues_future_send_times(notion, sqlite):
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    # 시드 업무의 due 가 now+10일이라 모든 발송 시점이 미래
    outcome = scheduler.tick(now=datetime.now())
    assert outcome.enqueued == 2  # D-1, D-day
    assert outcome.skipped_too_late == 0
    pending = sqlite.list_pending()
    assert len(pending) == 2


def test_tick_idempotent_does_not_double_enqueue(notion, sqlite):
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    scheduler.tick(now=datetime.now())
    second = scheduler.tick(now=datetime.now())
    assert second.enqueued == 0
    assert second.already_queued == 2
    pending = sqlite.list_pending()
    assert len(pending) == 2  # 여전히 2개만


def test_tick_skips_when_lag_exceeds_threshold():
    """6시간 룰 — 발송 시점이 7시간 지났으면 skipped_too_late 로 마킹."""
    sqlite = SqliteRepository(":memory:")
    try:
        notion = MockNotionRepository()
        # 업무의 due 를 과거로 강제 변경
        active = notion.list_active_tasks()[0]
        notion._tasks[active.task_id]["due_date"] = datetime(2026, 5, 23, 0, 0)  # noqa: SLF001

        scheduler = ReminderScheduler(notion=notion, sqlite=sqlite, late_threshold_hours=6)
        # now = 2026-05-23 10:00 — D-1(전날 0시)은 34시간 지각, D-day(00:00)는 10시간 지각
        # 둘 다 6시간 초과
        outcome = scheduler.tick(now=datetime(2026, 5, 23, 10, 0))
        assert outcome.skipped_too_late == 2

        # 모두 queued 가 아니라 skipped_too_late 상태로 저장
        pending = sqlite.list_pending()
        assert len(pending) == 0  # 'queued' 상태 없음

        with sqlite._conn() as conn:  # noqa: SLF001
            rows = conn.execute("SELECT status FROM send_queue ORDER BY scheduled_at").fetchall()
        statuses = [r["status"] for r in rows]
        assert statuses == ["skipped_too_late", "skipped_too_late"]
    finally:
        sqlite.close()


def test_tick_within_threshold_still_sends():
    """발송 시점이 6시간 이내 지각이면 정상 큐에 enqueue."""
    sqlite = SqliteRepository(":memory:")
    try:
        notion = MockNotionRepository()
        active = notion.list_active_tasks()[0]
        # due 를 now+3시간으로 → D-1 은 21시간 전(스킵), D-day 는 3시간 미래(wait)
        now = datetime(2026, 5, 23, 10, 0)
        notion._tasks[active.task_id]["due_date"] = now + timedelta(hours=3)  # noqa: SLF001

        scheduler = ReminderScheduler(notion=notion, sqlite=sqlite, late_threshold_hours=6)
        outcome = scheduler.tick(now=now)
        # D-1 은 -21h 지각 → skip / D-day 는 +3h 미래 → wait → enqueue (queued)
        assert outcome.enqueued == 1
        assert outcome.skipped_too_late == 1
    finally:
        sqlite.close()


def test_tick_filters_non_confirmed_tasks(notion, sqlite):
    """검토 대기·완료·취소 상태는 발송 후보가 아님."""
    # 시드 업무를 REVIEW_PENDING 으로 변경
    active = notion.list_active_tasks()[0]
    notion._tasks[active.task_id]["status"] = TaskStatus.REVIEW_PENDING

    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    outcome = scheduler.tick(now=datetime.now())
    assert outcome.enqueued == 0
    assert outcome.skipped_too_late == 0


def test_tick_skips_when_followup_disabled(notion, sqlite):
    active = notion.list_active_tasks()[0]
    notion._tasks[active.task_id]["followup_enabled"] = False

    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    outcome = scheduler.tick(now=datetime.now())
    assert outcome.enqueued == 0


def test_tick_message_has_required_prefix(notion, sqlite):
    """발송 큐에 들어간 메시지에는 [AI 자동 팔로우업] 접두어가 반드시 있어야 함."""
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    scheduler.tick(now=datetime.now())
    pending = sqlite.list_pending()
    assert len(pending) >= 1
    for row in pending:
        assert row["message"].startswith("[AI 자동 팔로우업] ")
