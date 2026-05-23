"""NotionRepository 단위 테스트 — fake notion-client Client 주입."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lovable_agent.domain import ExtractedTask, TaskStatus
from lovable_agent.storage.notion_repo import (
    NotionRepository,
    _extract_rich_text,
    _extract_title,
    _task_to_properties,
)


# ──────────────────────────────────────────────────────────────
# Fake notion Client — notion-client 3.x 스타일 (data_sources.query)
# ──────────────────────────────────────────────────────────────
class _FakeDatabases:
    """databases.retrieve 만 노출 (3.x 에서 query 는 data_sources 로 이동)."""

    def __init__(self) -> None:
        self.retrieve_calls: list[dict] = []

    def retrieve(self, database_id: str, **kwargs: Any) -> dict:
        self.retrieve_calls.append({"database_id": database_id})
        # 각 DB 에 동일 형식의 ds 1개 시드
        return {
            "id": database_id,
            "data_sources": [{"id": f"{database_id}_ds", "name": "default"}],
        }


class _FakeDataSources:
    def __init__(self, query_result: dict | None = None) -> None:
        self.query_result = query_result or {"results": []}
        self.query_calls: list[dict] = []

    def query(self, **kwargs: Any) -> dict:
        self.query_calls.append(kwargs)
        return self.query_result


class _FakePages:
    def __init__(self, created_page_id: str = "new_page_id") -> None:
        self._next_id = created_page_id
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.retrieve_responses: dict[str, dict] = {}

    def create(self, **kwargs: Any) -> dict:
        self.create_calls.append(kwargs)
        return {"id": self._next_id}

    def update(self, **kwargs: Any) -> dict:
        self.update_calls.append(kwargs)
        return {}

    def retrieve(self, page_id: str, **kwargs: Any) -> dict:
        return self.retrieve_responses.get(page_id, {"id": page_id, "properties": {}})


class _FakeClient:
    def __init__(self, query_result: dict | None = None) -> None:
        self.databases = _FakeDatabases()
        self.data_sources = _FakeDataSources(query_result=query_result)
        self.pages = _FakePages()


def _make_repo(query_result: dict | None = None) -> tuple[NotionRepository, _FakeClient]:
    fake = _FakeClient(query_result=query_result)
    repo = NotionRepository(
        token="fake-token",
        tasks_db_id="tasks_db",
        whitelist_db_id="wl_db",
        inbox_db_id="inbox_db",
        client=fake,
    )
    return repo, fake


# ──────────────────────────────────────────────────────────────
# properties 변환 헬퍼
# ──────────────────────────────────────────────────────────────
def test_task_to_properties_includes_required_fields():
    task = ExtractedTask(
        title="T",
        what="W",
        context="C",
        due_date=datetime(2026, 6, 1, 15, 0),
        assignee="A",
        source="kakao",
        source_detail="MOP 운영방",
    )
    props = _task_to_properties(task, status=TaskStatus.REVIEW_PENDING)
    assert props["Title"]["title"][0]["text"]["content"] == "T"
    assert props["What"]["rich_text"][0]["text"]["content"] == "W"
    assert props["Status"]["select"]["name"] == TaskStatus.REVIEW_PENDING.value
    assert props["Due Date"]["date"]["start"] == "2026-06-01T15:00:00"
    assert props["Source"]["select"]["name"] == "kakao"
    assert props["AI Followup Enabled"]["checkbox"] is True


def test_task_to_properties_omits_due_date_when_none():
    task = ExtractedTask(title="T", what="", context="", due_date=None, assignee="A")
    props = _task_to_properties(task, status=TaskStatus.REVIEW_PENDING)
    assert "Due Date" not in props


def test_extract_title_handles_empty():
    assert _extract_title(None) == ""
    assert _extract_title({}) == ""
    assert _extract_title({"title": []}) == ""


def test_extract_title_joins_segments():
    prop = {
        "title": [
            {"plain_text": "Hello "},
            {"plain_text": "world"},
        ]
    }
    assert _extract_title(prop) == "Hello world"


def test_extract_rich_text_falls_back_to_nested_content():
    """plain_text 가 없으면 text.content 도 시도."""
    prop = {"rich_text": [{"text": {"content": "fallback"}}]}
    assert _extract_rich_text(prop) == "fallback"


# ──────────────────────────────────────────────────────────────
# Tasks — list_active_tasks
# ──────────────────────────────────────────────────────────────
def test_list_active_tasks_returns_summaries():
    query_result = {
        "results": [
            {
                "id": "page_1",
                "properties": {
                    "Title": {"title": [{"plain_text": "MOP 보고서"}]},
                    "What": {"rich_text": [{"plain_text": "8월 정리"}]},
                    "Assignee": {"rich_text": [{"plain_text": "김매니저"}]},
                    "Due Date": {"date": {"start": "2026-06-01T15:00:00"}},
                    "Status": {"select": {"name": "확정"}},
                    "Chatroom": {"rich_text": [{"plain_text": "MOP 운영방"}]},
                    "AI Followup Enabled": {"checkbox": True},
                },
            }
        ]
    }
    repo, fake = _make_repo(query_result=query_result)
    summaries = repo.list_active_tasks()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.task_id == "page_1"
    assert s.title == "MOP 보고서"
    assert s.assignee == "김매니저"
    assert s.status == TaskStatus.CONFIRMED
    assert s.chatroom_title == "MOP 운영방"
    assert s.followup_enabled is True
    # 쿼리 필터에 종료 상태 제외 조건이 있는지
    assert len(fake.data_sources.query_calls) == 1
    filter_arg = fake.data_sources.query_calls[0].get("filter", {})
    assert "and" in filter_arg


def test_list_active_tasks_empty():
    repo, _ = _make_repo(query_result={"results": []})
    assert repo.list_active_tasks() == []


def test_list_active_tasks_handles_unknown_status():
    """알 수 없는 status 값이 와도 예외 X."""
    query_result = {
        "results": [
            {
                "id": "p1",
                "properties": {
                    "Title": {"title": [{"plain_text": "T"}]},
                    "Status": {"select": {"name": "이상한값"}},
                },
            }
        ]
    }
    repo, _ = _make_repo(query_result=query_result)
    summaries = repo.list_active_tasks()
    # 모르는 값 → IN_PROGRESS fallback
    assert summaries[0].status == TaskStatus.IN_PROGRESS


# ──────────────────────────────────────────────────────────────
# Tasks — add / update
# ──────────────────────────────────────────────────────────────
def test_add_task_creates_page_with_review_pending_status():
    repo, fake = _make_repo()
    fake.pages._next_id = "new_id"

    task = ExtractedTask(
        title="신규",
        what="내용",
        context="맥락",
        due_date=None,
        assignee="박팀장",
    )
    new_id = repo.add_task(task)
    assert new_id == "new_id"
    assert len(fake.pages.create_calls) == 1
    call = fake.pages.create_calls[0]
    assert call["parent"]["database_id"] == "tasks_db"
    props = call["properties"]
    assert props["Status"]["select"]["name"] == TaskStatus.REVIEW_PENDING.value


def test_update_task_status_calls_pages_update():
    repo, fake = _make_repo()
    repo.update_task_status("page_xyz", TaskStatus.CONFIRMED)
    assert len(fake.pages.update_calls) == 1
    call = fake.pages.update_calls[0]
    assert call["page_id"] == "page_xyz"
    assert call["properties"]["Status"]["select"]["name"] == "확정"


def test_append_task_note_prepends_new_line():
    """기존 노트가 있으면 새 줄을 앞에 prepend (timestamp 포함)."""
    repo, fake = _make_repo()
    fake.pages.retrieve_responses["p1"] = {
        "id": "p1",
        "properties": {"Notes": {"rich_text": [{"plain_text": "기존 내용"}]}},
    }
    repo.append_task_note("p1", "새 메모")
    assert len(fake.pages.update_calls) == 1
    new_text = fake.pages.update_calls[0]["properties"]["Notes"]["rich_text"][0]["text"]["content"]
    assert "새 메모" in new_text
    assert "기존 내용" in new_text  # 기존 보존
    # 시간 형식 (yyyy-mm-dd hh:mm) 포함
    assert "[" in new_text and "]" in new_text


# ──────────────────────────────────────────────────────────────
# Whitelist
# ──────────────────────────────────────────────────────────────
def test_list_whitelisted_chatrooms_filters_active():
    query_result = {
        "results": [
            {
                "id": "w1",
                "properties": {
                    "Chatroom": {"title": [{"plain_text": "MOP 운영방"}]},
                    "Window Title": {"rich_text": [{"plain_text": "MOP 운영방"}]},
                    "Active": {"checkbox": True},
                },
            }
        ]
    }
    repo, fake = _make_repo(query_result=query_result)
    specs = repo.list_whitelisted_chatrooms()
    assert len(specs) == 1
    assert specs[0].title_exact == "MOP 운영방"
    # Active=true 필터링 확인
    filter_arg = fake.data_sources.query_calls[0]["filter"]
    assert filter_arg == {"property": "Active", "checkbox": {"equals": True}}


def test_is_chatroom_whitelisted_empty_returns_false():
    repo, _ = _make_repo()
    assert repo.is_chatroom_whitelisted("") is False
    # 공백만 있는 문자열도 거부 — PRD R7 동명 톡방 오발송 방지 일관성
    assert repo.is_chatroom_whitelisted("   ") is False


def test_is_chatroom_whitelisted_queries_with_active_and_or_filter():
    repo, fake = _make_repo(query_result={"results": [{"id": "x"}]})
    assert repo.is_chatroom_whitelisted("MOP") is True
    filter_arg = fake.data_sources.query_calls[0]["filter"]
    assert filter_arg["and"][0] == {"property": "Active", "checkbox": {"equals": True}}


def test_is_chatroom_whitelisted_returns_false_when_no_match():
    repo, _ = _make_repo(query_result={"results": []})
    assert repo.is_chatroom_whitelisted("없는방") is False


# ──────────────────────────────────────────────────────────────
# Inbox
# ──────────────────────────────────────────────────────────────
def test_fetch_new_inbox_memos_returns_id_and_text():
    query_result = {
        "results": [
            {
                "id": "memo_1",
                "properties": {
                    "Memo": {"title": [{"plain_text": "내일 미팅"}]},
                    "Processed": {"checkbox": False},
                },
            }
        ]
    }
    repo, fake = _make_repo(query_result=query_result)
    memos = repo.fetch_new_inbox_memos()
    assert memos == [("memo_1", "내일 미팅")]
    # Processed=false 필터링
    filter_arg = fake.data_sources.query_calls[0]["filter"]
    assert filter_arg == {"property": "Processed", "checkbox": {"equals": False}}


def test_fetch_new_inbox_memos_skips_empty_text():
    query_result = {
        "results": [
            {
                "id": "memo_1",
                "properties": {"Memo": {"title": []}, "Processed": {"checkbox": False}},
            }
        ]
    }
    repo, _ = _make_repo(query_result=query_result)
    assert repo.fetch_new_inbox_memos() == []


def test_mark_inbox_memo_processed_updates_checkbox():
    repo, fake = _make_repo()
    repo.mark_inbox_memo_processed("memo_id")
    assert len(fake.pages.update_calls) == 1
    call = fake.pages.update_calls[0]
    assert call["page_id"] == "memo_id"
    assert call["properties"]["Processed"]["checkbox"] is True


# ──────────────────────────────────────────────────────────────
# 인터페이스 호환 — Protocol 준수
# ──────────────────────────────────────────────────────────────
def test_satisfies_notion_repository_protocol():
    """NotionRepository 가 storage.repository.NotionRepository Protocol 준수."""
    from lovable_agent.storage.repository import NotionRepository as NRProto

    repo, _ = _make_repo()
    # 정적 타입 체크 흉내 — 모든 메서드가 호출 가능한지
    assert callable(repo.list_active_tasks)
    assert callable(repo.add_task)
    assert callable(repo.update_task_status)
    assert callable(repo.append_task_note)
    assert callable(repo.list_whitelisted_chatrooms)
    assert callable(repo.is_chatroom_whitelisted)
    assert callable(repo.fetch_new_inbox_memos)
    assert callable(repo.mark_inbox_memo_processed)

    # Protocol annotation 만 확인 (런타임 isinstance 는 Protocol 에 대해 동작 안 함)
    _: NRProto = repo  # type: ignore[assignment]
