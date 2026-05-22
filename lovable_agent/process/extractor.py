"""Task Extractor — 비정형 텍스트에서 4요소 추출 오케스트레이션.

흐름:
1. 기존 진행 중인 업무 목록 조회 (NotionRepository.list_active_tasks)
2. LLMClient.extract_tasks(text, existing_tasks) 호출
3. 결과 분기:
   - is_duplicate_of 설정됨 → 해당 업무에 맥락 메모 추가
   - 미설정 → 새 업무를 `검토 대기` 상태로 추가
4. 처리된 task_id 들을 반환

ARCHITECTURE §4.3 참조.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from lovable_agent.process.llm_client import LLMClient
from lovable_agent.storage.repository import NotionRepository

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionOutcome:
    """Extractor 한 사이클의 결과 요약 — 로그·메트릭용."""

    new_task_ids: list[str]
    merged_task_ids: list[str]
    raw_extracted_count: int


class TaskExtractor:
    """LLMClient + NotionRepository 를 묶은 오케스트레이션."""

    def __init__(self, llm: LLMClient, repo: NotionRepository) -> None:
        self._llm = llm
        self._repo = repo

    def process_text(self, text: str, source_label: str = "") -> ExtractionOutcome:
        """비정형 텍스트 → 노션 업무로 동기화.

        Args:
            text: 카톡 .txt 일부, 노션 메모, 회의록 등.
            source_label: 로그에 남길 출처 표시 (예: 'MOP 운영방 익스포트').

        Returns:
            처리된 업무 ID 들의 분류.
        """
        if not text.strip():
            log.info("Extractor — 빈 텍스트, 스킵")
            return ExtractionOutcome([], [], 0)

        existing = self._repo.list_active_tasks()
        log.info(
            "Extractor — 기존 진행중 업무 %d개와 함께 LLM 호출 (출처: %s, %d자)",
            len(existing),
            source_label or "?",
            len(text),
        )

        result = self._llm.extract_tasks(text, existing)
        log.info("Extractor — LLM 응답 %d건 (중복 포함)", len(result.tasks))

        new_ids: list[str] = []
        merged_ids: list[str] = []

        for task in result.tasks:
            if task.is_duplicate_of:
                # 기존 업무에 맥락 추가
                note = f"[자동] {task.context or task.what}"
                self._repo.append_task_note(task.is_duplicate_of, note)
                merged_ids.append(task.is_duplicate_of)
                log.info("  → 중복 감지: 기존 %s 에 메모 추가", task.is_duplicate_of[:8])
            else:
                new_id = self._repo.add_task(task)
                new_ids.append(new_id)
                log.info(
                    "  → 신규: %s (담당: %s, 마감: %s)",
                    task.title,
                    task.assignee,
                    task.due_date.isoformat() if task.due_date else "미정",
                )

        return ExtractionOutcome(
            new_task_ids=new_ids,
            merged_task_ids=merged_ids,
            raw_extracted_count=len(result.tasks),
        )
