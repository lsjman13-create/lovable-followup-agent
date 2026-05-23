"""SendDispatcher — Scheduler 가 enqueue 한 발송 큐 항목을 KakaoSender 로 처리.

Phase 3 통합의 핵심:
- Scheduler.tick() 이 SQLite send_queue 에 queued 항목을 채워넣음
- Dispatcher 가 queued 항목들을 꺼내 KakaoSender.send() 호출
- 결과를 send_history 에 기록 + send_queue.status 갱신
- 실패 시 Notifier 로 사용자에게 즉시 알림

ARCHITECTURE §4.6 + §4.7 통합 지점.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from lovable_agent.domain import WindowSpec
from lovable_agent.output.kakao_sender import SendResult
from lovable_agent.output.notifier import Notifier
from lovable_agent.storage.sqlite_repo import SqliteRepository

log = logging.getLogger(__name__)


class SenderProtocol(Protocol):
    """KakaoSender 와 호환되는 최소 인터페이스 — 테스트에서 fake 주입 가능."""

    def send(self, target: WindowSpec, message: str) -> SendResult: ...


@dataclass
class DispatchOutcome:
    """한 번의 dispatch_pending 호출 결과."""

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_no_message: int = 0
    sent_queue_ids: list[int] = field(default_factory=list)
    failed_queue_ids: list[int] = field(default_factory=list)


class SendDispatcher:
    """발송 큐의 queued 항목을 KakaoSender 로 처리."""

    def __init__(
        self,
        sqlite: SqliteRepository,
        sender: SenderProtocol,
        notifier: Notifier | None = None,
        batch_limit: int = 10,
    ) -> None:
        self._sqlite = sqlite
        self._sender = sender
        self._notifier = notifier
        self._batch_limit = batch_limit

    def dispatch_pending(self) -> DispatchOutcome:
        """큐에서 status='queued' 항목을 최대 batch_limit 개 가져와 발송 시도.

        각 항목은 다음 흐름:
        1. KakaoSender.send() 호출
        2. send_history 기록
        3. send_queue 상태 갱신 (sent / failed)
        4. 실패면 Notifier 알림

        Returns:
            DispatchOutcome — 시도·성공·실패·스킵 카운트.
        """
        pending = self._sqlite.list_pending(limit=self._batch_limit)
        log.info("Dispatch — 발송 후보 %d건", len(pending))
        return self._dispatch_rows(pending)

    def _dispatch_rows(self, rows: Iterable[dict]) -> DispatchOutcome:
        outcome = DispatchOutcome()
        for row in rows:
            outcome.attempted += 1
            queue_id = int(row["id"])
            message = row.get("message") or ""
            chatroom = row.get("chatroom_title") or ""

            if not message or not chatroom:
                # 데이터 무결성 문제 — 비정상 큐 항목 스킵
                outcome.skipped_no_message += 1
                self._sqlite.update_send_status(queue_id, "failed", increment_attempt=True)
                self._sqlite.record_send_attempt(
                    queue_id,
                    success=False,
                    error_detail=f"비정상 큐 항목: message={message!r}, chatroom={chatroom!r}",
                )
                log.warning("Dispatch 스킵 (큐 항목 비정상): id=%d", queue_id)
                continue

            target = WindowSpec(title_exact=chatroom)
            log.info("Dispatch [%d] → %r: %r", queue_id, chatroom, message[:60])

            result = self._sender.send(target, message)

            if result.success:
                outcome.succeeded += 1
                outcome.sent_queue_ids.append(queue_id)
                self._sqlite.update_send_status(queue_id, "sent", increment_attempt=True)
                self._sqlite.record_send_attempt(queue_id, success=True)
                log.info("Dispatch [%d] ✅ 성공", queue_id)
            else:
                outcome.failed += 1
                outcome.failed_queue_ids.append(queue_id)
                self._sqlite.update_send_status(queue_id, "failed", increment_attempt=True)
                self._sqlite.record_send_attempt(
                    queue_id,
                    success=False,
                    error_detail=f"[{result.failed_step}] {result.error_reason}",
                )
                log.warning(
                    "Dispatch [%d] ❌ 실패: step=%s reason=%s",
                    queue_id,
                    result.failed_step,
                    result.error_reason,
                )
                if self._notifier is not None:
                    self._notifier.error(
                        title="자동 발송 실패",
                        body=(
                            f"'{chatroom}' 에 메시지 발송 실패\n"
                            f"실패 단계: {result.failed_step}\n"
                            f"사유: {result.error_reason}"
                        ),
                    )
        return outcome
