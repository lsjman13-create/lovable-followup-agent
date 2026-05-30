"""TaskExtractor 단위 테스트 — mock LLM + mock Notion."""

from __future__ import annotations

from datetime import datetime

import pytest

from lovable_agent.domain import ExtractedTask, ExtractionResult, TaskSummary
from lovable_agent.process.extractor import TaskExtractor
from lovable_agent.storage.mock_notion_repo import MockNotionRepository


class FakeLLM:
    """테스트용 결정적 LLM — 미리 정한 응답을 그대로 반환."""

    def __init__(self, response: ExtractionResult) -> None:
        self.response = response
        self.calls: list[tuple[str, list[TaskSummary]]] = []

    def extract_tasks(self, text, existing_tasks):
        self.calls.append((text, list(existing_tasks)))
        return self.response


@pytest.fixture()
def notion():
    return MockNotionRepository()


def test_empty_text_skips_llm_and_returns_empty_outcome(notion):
    llm = FakeLLM(ExtractionResult([]))
    extractor = TaskExtractor(llm=llm, repo=notion)
    outcome = extractor.process_text("   \n   ")
    assert outcome.new_task_ids == []
    assert outcome.merged_task_ids == []
    assert outcome.raw_extracted_count == 0
    assert llm.calls == []  # 빈 텍스트는 LLM 호출 안 함


def test_new_task_is_added_to_notion(notion):
    new_task = ExtractedTask(
        title="신규 업무",
        what="X 처리",
        context="문맥",
        due_date=datetime(2026, 6, 1, 15, 0),
        assignee="박팀장",
    )
    llm = FakeLLM(ExtractionResult([new_task]))
    extractor = TaskExtractor(llm=llm, repo=notion)

    before_count = len(notion.list_active_tasks())
    outcome = extractor.process_text("아무 텍스트", source_label="테스트")
    after = notion.list_active_tasks()

    assert len(outcome.new_task_ids) == 1
    assert len(after) == before_count + 1
    titles = [t.title for t in after]
    assert "신규 업무" in titles


def test_duplicate_task_appends_note_instead_of_adding_new(notion):
    # 기존 업무 1건이 시드되어 있음 ('MOP 8월 운영 보고서')
    existing = notion.list_active_tasks()
    assert len(existing) == 1
    target_id = existing[0].task_id

    dup_task = ExtractedTask(
        title="중복 추정 업무",
        what="비슷한 내용",
        context="새로 받은 추가 맥락",
        due_date=None,
        assignee="김매니저",
        is_duplicate_of=target_id,
    )
    llm = FakeLLM(ExtractionResult([dup_task]))
    extractor = TaskExtractor(llm=llm, repo=notion)

    before_active = len(notion.list_active_tasks())
    outcome = extractor.process_text("아무 텍스트")
    after_active = len(notion.list_active_tasks())

    assert outcome.merged_task_ids == [target_id]
    assert outcome.new_task_ids == []
    # 활성 업무 개수는 그대로 (새 업무 안 만듦)
    assert after_active == before_active


def test_llm_receives_existing_tasks_as_context(notion):
    llm = FakeLLM(ExtractionResult([]))
    extractor = TaskExtractor(llm=llm, repo=notion)
    extractor.process_text("뭔가 텍스트")

    assert len(llm.calls) == 1
    text_passed, existing_passed = llm.calls[0]
    assert text_passed == "뭔가 텍스트"
    assert len(existing_passed) >= 1  # 시드 데이터 1건
    assert all(isinstance(t, TaskSummary) for t in existing_passed)


def test_chunks_input_longer_than_max_input_chars(notion):
    """max_input_chars 초과 시 마지막 줄바꿈 기준으로 청크 분할되어 여러 번 LLM이 호출되어야 한다."""
    llm = FakeLLM(ExtractionResult([]))
    extractor = TaskExtractor(llm=llm, repo=notion, max_input_chars=100)

    # 50자 줄 * 4개 = 200자 (줄바꿈 포함) -> 최대 100자이므로 총 4번의 청크가 발생
    long_text = "\n".join(["가" * 50] * 4)
    extractor.process_text(long_text, source_label="긴 입력")

    assert len(llm.calls) == 4
    for text_passed, _ in llm.calls:
        assert len(text_passed) <= 100
        assert text_passed == "가" * 50


def test_no_truncation_when_max_input_chars_unset(notion):
    """max_input_chars 미설정(None)이면 입력 그대로 통과."""
    llm = FakeLLM(ExtractionResult([]))
    extractor = TaskExtractor(llm=llm, repo=notion)

    long_text = "가" * 10000
    extractor.process_text(long_text)
    assert llm.calls[0][0] == long_text


def test_no_truncation_when_input_under_limit(notion):
    """상한 미만 입력은 절단 없음."""
    llm = FakeLLM(ExtractionResult([]))
    extractor = TaskExtractor(llm=llm, repo=notion, max_input_chars=1000)

    text = "짧은 텍스트"
    extractor.process_text(text)
    assert llm.calls[0][0] == text


def test_mixed_new_and_duplicate_in_one_call(notion):
    existing = notion.list_active_tasks()
    target_id = existing[0].task_id

    tasks = [
        ExtractedTask(title="신규 A", what="A 처리", context="", due_date=None, assignee="X"),
        ExtractedTask(
            title="중복",
            what="이미 있는 거",
            context="추가 맥락",
            due_date=None,
            assignee="Y",
            is_duplicate_of=target_id,
        ),
        ExtractedTask(title="신규 B", what="B 처리", context="", due_date=None, assignee="Z"),
    ]
    llm = FakeLLM(ExtractionResult(tasks))
    extractor = TaskExtractor(llm=llm, repo=notion)
    outcome = extractor.process_text("긴 텍스트")

    assert len(outcome.new_task_ids) == 2
    assert outcome.merged_task_ids == [target_id]
    assert outcome.raw_extracted_count == 3
