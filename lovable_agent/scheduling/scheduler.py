"""스케줄러 — due 체크 + 발송 큐 enqueue + 6시간 룰.

설계 결정:
- 시간 의존 로직을 pure 함수로 분리해서 단위 테스트 시 `now` 를 주입 가능하게.
- APScheduler 통합은 별도 진입점(`start_in_background`)에 격리. 실제 데몬 모드용.

PRD §FR-4 / ARCHITECTURE §4.5 6시간 룰 참조.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from lovable_agent.domain import (
    SendQueueItem,
    TaskStatus,
    TaskSummary,
    WindowSpec,
)
from lovable_agent.storage.repository import NotionRepository
from lovable_agent.storage.sqlite_repo import SqliteRepository

log = logging.getLogger(__name__)

DEFAULT_REMINDER_OFFSETS_HOURS = (24, 0)
"""기본 발송 시점 — 마감 24시간 전(D-1) + 마감 시점(D-day)."""

DEFAULT_LATE_THRESHOLD_HOURS = 6
"""이 값 초과로 지난 발송 시점은 자동 발송하지 않음 (사용자에게만 알림)."""


# ──────────────────────────────────────────────────────────────
# Pure 함수들 — 단위 테스트 용이
# ──────────────────────────────────────────────────────────────
def compute_send_times(
    due_date: datetime,
    offsets_hours: tuple[int, ...] = DEFAULT_REMINDER_OFFSETS_HOURS,
) -> list[datetime]:
    """마감일 기준으로 발송할 시점들 계산.

    Args:
        due_date: 업무 마감 datetime.
        offsets_hours: 각 발송 시점이 마감보다 몇 시간 전인가 (예: (24, 0)).

    Returns:
        발송 시점 datetime 리스트 (정렬됨).
    """
    return sorted(due_date - timedelta(hours=h) for h in offsets_hours)


Decision = Literal["send", "skip_too_late", "wait"]


def classify_pending(
    scheduled_at: datetime,
    now: datetime,
    late_threshold_hours: int = DEFAULT_LATE_THRESHOLD_HOURS,
) -> Decision:
    """발송 예정 시점·현재 시각·임계치로 행동 결정.

    - send: 이미 도래했고, 6시간 이내 지각
    - skip_too_late: 6시간 초과 지각 — 자동 발송 스킵
    - wait: 아직 도래 안 함
    """
    if now < scheduled_at:
        return "wait"
    lag = now - scheduled_at
    if lag <= timedelta(hours=late_threshold_hours):
        return "send"
    return "skip_too_late"


def is_due_for_followup(task: TaskSummary) -> bool:
    """업무가 자동 발송 후보인가 — 확정 + followup_enabled + due 있음 + 톡방 있음."""
    if task.status != TaskStatus.CONFIRMED:
        return False
    if not task.followup_enabled:
        return False
    if task.due_date is None:
        return False
    return bool(task.chatroom_title)


# ──────────────────────────────────────────────────────────────
# Tick 단위 — 한 사이클의 행동을 결정·실행
# ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TickOutcome:
    """한 번의 tick 에서 일어난 일."""

    enqueued: int = 0
    skipped_too_late: int = 0
    already_queued: int = 0


class ReminderScheduler:
    """노션 Tasks + SQLite send_queue 를 묶어 발송 예정을 관리."""

    def __init__(
        self,
        notion: NotionRepository,
        sqlite: SqliteRepository,
        message_prefix: str = "[AI 자동 팔로우업] ",
        offsets_hours: tuple[int, ...] = DEFAULT_REMINDER_OFFSETS_HOURS,
        late_threshold_hours: int = DEFAULT_LATE_THRESHOLD_HOURS,
    ) -> None:
        self._notion = notion
        self._sqlite = sqlite
        self._prefix = message_prefix
        self._offsets = offsets_hours
        self._late_threshold = late_threshold_hours

    def tick(self, now: datetime | None = None) -> TickOutcome:
        """한 사이클: 확정된 업무들의 발송 시점을 큐로 동기화하고, 지각 판정.

        Args:
            now: 현재 시각. None 이면 datetime.now() 사용. 테스트 시 주입.
        """
        now = now or datetime.now()
        enqueued = 0
        skipped = 0
        already = 0

        active_tasks = self._notion.list_active_tasks()
        for task in active_tasks:
            if not is_due_for_followup(task):
                continue
            assert task.due_date is not None  # is_due_for_followup 에서 보장
            for scheduled_at in compute_send_times(task.due_date, self._offsets):
                if self._sqlite.is_already_queued(task.task_id, scheduled_at):
                    already += 1
                    continue
                decision = classify_pending(scheduled_at, now, self._late_threshold)
                if decision == "wait":
                    # 아직 미래 — 그냥 enqueue 해두고 발송 시점에 처리하도록 함
                    self._sqlite.enqueue_send(self._build_item(task, scheduled_at))
                    enqueued += 1
                elif decision == "send":
                    self._sqlite.enqueue_send(self._build_item(task, scheduled_at))
                    enqueued += 1
                else:
                    # skip_too_late — 큐에 안 넣고 기록만
                    item = self._build_item(task, scheduled_at)
                    qid = self._sqlite.enqueue_send(item)
                    self._sqlite.update_send_status(qid, "skipped_too_late")
                    skipped += 1
                    log.warning(
                        "6시간 룰 위반 — 자동 발송 스킵: task=%s scheduled=%s lag=%s",
                        task.task_id[:8],
                        scheduled_at,
                        now - scheduled_at,
                    )

        return TickOutcome(
            enqueued=enqueued,
            skipped_too_late=skipped,
            already_queued=already,
        )

    def _build_item(self, task: TaskSummary, scheduled_at: datetime) -> SendQueueItem:
        # 발송 시점에 보낼 본문 — Phase 3 단계의 기본 템플릿
        offset = task.due_date - scheduled_at if task.due_date else timedelta()
        hours = int(offset.total_seconds() / 3600)
        when_label = "D-day" if hours == 0 else f"D-{hours // 24 or 1}"
        body = f"{when_label} 알림 — '{task.title}' 마감이 다가옵니다 (담당: {task.assignee})"
        message = (
            self._prefix + body
        )  # safety/prefix.py 의 enforce_prefix 도 발송 직전 한 번 더 검증
        return SendQueueItem(
            task_id=task.task_id,
            chatroom=WindowSpec(title_exact=task.chatroom_title),
            message=message,
            scheduled_at=scheduled_at,
            status="queued",
        )
