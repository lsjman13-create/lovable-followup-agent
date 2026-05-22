"""도메인 모델 — 시스템 전반에서 쓰는 dataclass 정의.

여기 정의된 타입들은 인터페이스 경계에서 쓰이므로 가급적 외부 의존성 없이 표준
라이브러리만 사용. ARCHITECTURE §5 데이터 모델 참조.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal


class TaskStatus(StrEnum):
    """업무 상태 — 노션 Tasks DB의 Status 컬럼과 1:1 매핑."""

    REVIEW_PENDING = "검토 대기"  # AI가 추출했지만 매니저 확인 전
    CONFIRMED = "확정"  # 매니저가 검토 완료, 자동 리마인드 활성화
    IN_PROGRESS = "진행 중"
    DONE = "완료"
    CANCELLED = "취소"


@dataclass(frozen=True)
class TaskSummary:
    """기존 진행 중인 업무의 간략 요약 — Extractor·Scheduler 가 사용."""

    task_id: str  # 노션 페이지 ID
    title: str
    assignee: str
    due_date: datetime | None
    one_line_summary: str
    status: TaskStatus = TaskStatus.CONFIRMED
    chatroom_title: str = ""  # 발송할 톡방 (확정된 업무일 때만 의미 있음)
    followup_enabled: bool = True


@dataclass
class ExtractedTask:
    """AI가 비정형 텍스트에서 추출한 업무 1건."""

    title: str
    what: str
    context: str
    due_date: datetime | None
    assignee: str
    source: Literal["kakao", "manual"] = "kakao"
    source_detail: str = ""
    is_duplicate_of: str | None = None  # 중복이면 기존 task_id


@dataclass
class ExtractionResult:
    """Extractor의 출력 — LLM 호출 1회의 결과."""

    tasks: list[ExtractedTask] = field(default_factory=list)


@dataclass(frozen=True)
class WindowSpec:
    """카톡 채팅창 식별 정보 — ARCHITECTURE §4.6.1.

    동명 톡방·동명이인 오발송 방지를 위해 단순 문자열이 아닌 구조체로 캡슐화.
    실제 win32 매칭 로직은 Phase 2에서 output/window_spec.py에 구현.
    """

    title_exact: str  # 채팅창 제목 (완전일치)
    class_name: str = "EVA_ChildWindow"  # Phase 2 실측 후 확정
    process_name: str = "KakaoTalk.exe"
    expected_input_class: str = "RICHEDIT50W"


@dataclass
class SendQueueItem:
    """발송 큐 1건 — SQLite send_queue 테이블의 행에 대응."""

    task_id: str
    chatroom: WindowSpec
    message: str
    scheduled_at: datetime
    status: Literal["queued", "sent", "failed", "skipped_too_late", "skipped_not_whitelisted"] = (
        "queued"
    )
    attempted_count: int = 0
