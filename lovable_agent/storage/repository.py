"""Repository Protocol — 노션 저장소의 단일 인터페이스.

구현체:
- NotionRepository (notion_repo.py) — 실제 Notion API. Phase 4.
- MockNotionRepository (mock_notion_repo.py) — 인메모리 dict. Phase 1~3.

ARCHITECTURE §4.4 참조.
"""

from __future__ import annotations

from typing import Protocol

from lovable_agent.domain import ExtractedTask, TaskStatus, TaskSummary, WindowSpec


class NotionRepository(Protocol):
    """노션 데이터에 대한 추상 접근 — Tasks / Whitelist / Inbox 3종 DB."""

    # ─── Tasks ───
    def list_active_tasks(self) -> list[TaskSummary]:
        """진행 중·확정·검토대기 상태의 업무 요약 — Extractor의 중복 판별 입력."""
        ...

    def add_task(self, task: ExtractedTask) -> str:
        """새 업무를 검토 대기 상태로 추가. 반환값은 생성된 task_id."""
        ...

    def update_task_status(self, task_id: str, status: TaskStatus) -> None: ...

    def append_task_note(self, task_id: str, note: str) -> None:
        """기존 업무에 맥락 메모 추가 — 중복 판별 시 사용."""
        ...

    # ─── Whitelist ───
    def list_whitelisted_chatrooms(self) -> list[WindowSpec]:
        """자동 발송 허용 톡방 목록 — Active 체크된 항목만."""
        ...

    def is_chatroom_whitelisted(self, title_exact: str) -> bool:
        """ARCHITECTURE §4.6.3 방어선 0(사전 체크) — 발송 전 화이트리스트 검증."""
        ...

    # ─── Inbox ───
    def fetch_new_inbox_memos(self) -> list[str]:
        """노션 Inbox 페이지에서 아직 처리하지 않은 메모 텍스트 목록."""
        ...

    def mark_inbox_memo_processed(self, memo_id: str) -> None: ...
