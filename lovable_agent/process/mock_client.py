"""MockLLMClient — Phase 1~3 개발용 가짜 LLM.

실제 Anthropic API 호출 없이 결정적인 응답을 돌려준다. PLAN.md 의 외부 의존성
가드레일을 지키기 위해 사용.

동작:
- 입력 텍스트의 길이·키워드 기반으로 1~2개의 가짜 업무를 생성.
- existing_tasks 와 유사 키워드가 매칭되면 is_duplicate_of 채워서 반환.
- 결과는 입력에 대해 항상 같음 (테스트 가능).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from lovable_agent.domain import ExtractedTask, ExtractionResult, TaskSummary


class MockLLMClient:
    """결정적 응답을 돌려주는 가짜 LLMClient (LLMClient Protocol 준수)."""

    def extract_tasks(
        self,
        text: str,
        existing_tasks: list[TaskSummary],
    ) -> ExtractionResult:
        if not text.strip():
            return ExtractionResult(tasks=[])

        # 매우 단순한 규칙: 첫 줄을 제목으로 쓰고 마감을 D+3 으로.
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        title = (first_line[:40] + "...") if len(first_line) > 40 else first_line or "샘플 업무"

        # 기존 업무와 첫 줄 키워드가 겹치면 중복 처리.
        duplicate_of: str | None = None
        for existing in existing_tasks:
            if any(word in existing.title for word in title.split() if len(word) >= 3):
                duplicate_of = existing.task_id
                break

        task = ExtractedTask(
            title=title,
            what=f"[MOCK] {title} 처리",
            context=f"[MOCK] 원본 텍스트 {len(text)}자 분석 결과",
            due_date=datetime.now() + timedelta(days=3),
            assignee="[MOCK] 김매니저",
            source="kakao",
            source_detail="[MOCK] 테스트 톡방",
            is_duplicate_of=duplicate_of,
        )
        return ExtractionResult(tasks=[task])
