"""MockNotionRepository — Python dict 기반 인메모리 가짜 노션.

Phase 1~3 동안 실제 노션 API 호출 없이 시스템을 구동하기 위함. 데이터는 프로세스
수명 동안만 유지되며, 종료 시 소멸.

생성자에서 화이트리스트 톡방·기존 업무 몇 개를 미리 채워둬서 dry-run 시 의미
있는 흐름이 흘러가게 한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from lovable_agent.domain import ExtractedTask, TaskStatus, TaskSummary, WindowSpec


class MockNotionRepository:
    """인메모리 가짜 노션 (NotionRepository Protocol 준수)."""

    def __init__(self) -> None:
        # Tasks DB
        self._tasks: dict[str, dict] = {}
        # Whitelist DB
        self._whitelist: list[WindowSpec] = []
        # Inbox 페이지
        self._inbox: list[tuple[str, str]] = []  # (memo_id, text)
        self._processed_inbox_ids: set[str] = set()

        self._seed_initial_data()

    def _seed_initial_data(self) -> None:
        """dry-run 흐름이 의미를 갖도록 초기 데이터 시드."""
        # 기존 진행 중인 업무 1건 (중복 판별 입력으로 쓰임)
        existing_id = self._new_id()
        self._tasks[existing_id] = {
            "task_id": existing_id,
            "title": "MOP 8월 운영 보고서",
            "what": "8월 한 달 MOP 운영 결과 보고",
            "context": "기존 업무",
            "due_date": datetime.now() + timedelta(days=10),
            "assignee": "김매니저",
            "source": "kakao",
            "source_detail": "MOP 운영방",
            "status": TaskStatus.CONFIRMED,
            "chatroom_title": "MOP 운영방",
            "followup_enabled": True,
            "notes": [],
        }

        # 화이트리스트 톡방 1건
        self._whitelist.append(WindowSpec(title_exact="MOP 운영방", class_name="EVA_ChildWindow"))

        # Inbox 메모 1건
        self._inbox.append((self._new_id(), "내일까지 김매니저에게 8월 운영 회의록 공유 요청"))

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:32]

    # ─── Tasks ───
    def list_active_tasks(self) -> list[TaskSummary]:
        result = []
        active_statuses = {
            TaskStatus.REVIEW_PENDING,
            TaskStatus.CONFIRMED,
            TaskStatus.IN_PROGRESS,
        }
        for t in self._tasks.values():
            if t["status"] not in active_statuses:
                continue
            result.append(
                TaskSummary(
                    task_id=t["task_id"],
                    title=t["title"],
                    assignee=t["assignee"],
                    due_date=t["due_date"],
                    one_line_summary=t["what"][:80],
                    status=t["status"],
                    chatroom_title=t.get("chatroom_title", ""),
                    followup_enabled=t.get("followup_enabled", True),
                )
            )
        return result

    def add_task(self, task: ExtractedTask) -> str:
        task_id = self._new_id()
        self._tasks[task_id] = {
            "task_id": task_id,
            "title": task.title,
            "what": task.what,
            "context": task.context,
            "due_date": task.due_date,
            "assignee": task.assignee,
            "source": task.source,
            "source_detail": task.source_detail,
            "status": TaskStatus.REVIEW_PENDING,
            "chatroom_title": task.source_detail or "",
            "followup_enabled": True,
            "notes": [],
        }
        return task_id

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = status

    def append_task_note(self, task_id: str, note: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id]["notes"].append(note)

    # ─── Whitelist ───
    def list_whitelisted_chatrooms(self) -> list[WindowSpec]:
        return list(self._whitelist)

    def is_chatroom_whitelisted(self, title_exact: str) -> bool:
        return any(w.title_exact == title_exact for w in self._whitelist)

    # ─── Inbox ───
    def fetch_new_inbox_memos(self) -> list[str]:
        return [text for (memo_id, text) in self._inbox if memo_id not in self._processed_inbox_ids]

    def mark_inbox_memo_processed(self, memo_id: str) -> None:
        self._processed_inbox_ids.add(memo_id)
