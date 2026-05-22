"""LLMClient Protocol — AI 호출부의 단일 인터페이스.

이 인터페이스를 구현하는 객체라면 무엇이든 Extractor 가 받아들인다.
구현체:
- AnthropicAPIClient (anthropic_client.py) — 실제 Claude API 호출. Phase 4.
- MockLLMClient (mock_client.py) — 하드코딩 응답. Phase 1~3 개발용.

Why Protocol? — DECISIONS.md §의 'AI 엔진 격리 설계' 원칙. 향후 Claude Code 직접
운영, 로컬 LLM 등으로 전환하기 쉽게.
"""

from __future__ import annotations

from typing import Protocol

from lovable_agent.domain import ExtractionResult, TaskSummary


class LLMClient(Protocol):
    """비정형 텍스트 + 기존 업무 목록 → 4요소 추출 결과."""

    def extract_tasks(
        self,
        text: str,
        existing_tasks: list[TaskSummary],
    ) -> ExtractionResult:
        """텍스트에서 업무를 추출.

        Args:
            text: 카톡 .txt 일부, 노션 메모, 기타 비정형 텍스트.
            existing_tasks: 노션 Tasks DB의 진행 중인 업무 요약 — 중복 판별용.

        Returns:
            추출된 업무 0개 이상. is_duplicate_of 가 설정된 항목은 신규 추가가
            아니라 기존 업무에 맥락을 보태는 의미.
        """
        ...
