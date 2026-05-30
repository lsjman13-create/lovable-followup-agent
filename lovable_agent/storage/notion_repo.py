"""NotionRepository — `notion-client` SDK 기반 실 노션 API 구현체.

`MockNotionRepository` 와 동일한 인터페이스. config.toml 의 DB ID 들과 환경변수
`NOTION_API_TOKEN` 만 있으면 동작. PLAN Phase 4 의 노션 부분.

스키마 정의는 ARCHITECTURE §5.1 + `scripts/setup_notion.py` 에서 자동 생성한
스키마와 일치해야 함.

설계 결정:
- ORM·복잡한 추상화 미사용. notion-client 의 dict API 직접 사용.
- properties 변환 헬퍼는 모듈 함수로 분리 → 테스트 가능
- Notion API 의 page_id 가 곧 우리 시스템의 task_id / memo_id (외부 키 통일)
- Inbox 는 ARCHITECTURE 의 "페이지" 가 아니라 **DB 로 결정** — 폴링 효율·"처리됨"
  마킹 편의 (DECISIONS 에 기록 예정)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from lovable_agent.domain import ExtractedTask, TaskStatus, TaskSummary, WindowSpec

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# 스키마 — Tasks DB
# ──────────────────────────────────────────────────────────────
# 컬럼명 (notion property name). 변경 시 `setup_notion.py` 도 같이.
COL_TITLE = "Title"
COL_WHAT = "What"
COL_CONTEXT = "Context"
COL_DUE = "Due Date"
COL_ASSIGNEE = "Assignee"
COL_SOURCE = "Source"
COL_SOURCE_DETAIL = "Source Detail"
COL_STATUS = "Status"
COL_FOLLOWUP_ENABLED = "AI Followup Enabled"
COL_CHATROOM = "Chatroom"
COL_NOTES = "Notes"

# Whitelist DB
COL_WL_TITLE = "Chatroom"  # title
COL_WL_WINDOW_TITLE = "Window Title"
COL_WL_ACTIVE = "Active"
COL_WL_MEMO = "Memo"

# Inbox DB
COL_INBOX_TITLE = "Memo"
COL_INBOX_PROCESSED = "Processed"


# 종료 상태 — 활성 업무 필터에서 제외
_TERMINAL_STATUSES = {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}


class NotionRepository:
    """실 노션 API 백엔드. token + 3개 DB ID 로 초기화.

    notion-client 3.x 의 변화 반영:
    - `databases.query` 는 deprecated → `data_sources.query` 사용
    - 각 DB 의 default data_source_id 를 init 시 1회 조회·캐시
    """

    def __init__(
        self,
        token: str,
        tasks_db_id: str,
        whitelist_db_id: str,
        inbox_db_id: str,
        client: Any = None,
        # 테스트 친화 — data_source_id 직접 주입 시 retrieve 안 함
        tasks_ds_id: str | None = None,
        whitelist_ds_id: str | None = None,
        inbox_ds_id: str | None = None,
    ) -> None:
        """
        Args:
            token: NOTION_API_TOKEN (Integration secret).
            tasks_db_id: Tasks DB 의 ID.
            whitelist_db_id: Whitelist DB 의 ID.
            inbox_db_id: Inbox DB 의 ID.
            client: 테스트용 fake notion Client 주입. None 이면 실제 Client 생성.
            tasks_ds_id / whitelist_ds_id / inbox_ds_id: 테스트에서 retrieve 회피용
                직접 주입. None 이면 client.databases.retrieve 로 조회.
        """
        if client is None:
            from notion_client import Client  # 임포트 비용 회피 — 실제 호출 시점에 import

            client = Client(auth=token)
        self._client = client
        self._tasks_db = tasks_db_id
        self._whitelist_db = whitelist_db_id
        self._inbox_db = inbox_db_id
        # data_source_id 캐시 (notion-client 3.x 의 query 대상)
        self._tasks_ds = tasks_ds_id or self._get_default_data_source_id(tasks_db_id)
        self._whitelist_ds = whitelist_ds_id or self._get_default_data_source_id(whitelist_db_id)
        self._inbox_ds = inbox_ds_id or self._get_default_data_source_id(inbox_db_id)

    def _get_default_data_source_id(self, db_id: str) -> str:
        """DB 의 첫 번째 (default) data source ID 조회."""
        db = self._client.databases.retrieve(database_id=db_id)
        sources = db.get("data_sources") or []
        if not sources:
            raise RuntimeError(f"DB {db_id} 에 data_sources 가 없음 — 노션 API 응답 확인 필요")
        return str(sources[0]["id"])

    # ──────────────────────────────────────────────────────────────
    # Tasks DB
    # ──────────────────────────────────────────────────────────────
    def list_active_tasks(self) -> list[TaskSummary]:
        """완료·취소 외의 모든 업무 — Extractor·Scheduler 의 입력."""
        results = self._client.data_sources.query(
            data_source_id=self._tasks_ds,
            filter={
                "and": [
                    {
                        "property": COL_STATUS,
                        "select": {"does_not_equal": TaskStatus.DONE.value},
                    },
                    {
                        "property": COL_STATUS,
                        "select": {"does_not_equal": TaskStatus.CANCELLED.value},
                    },
                ],
            },
            page_size=100,
        )
        rows = results.get("results", [])
        return [_page_to_task_summary(page) for page in rows]

    def add_task(self, task: ExtractedTask) -> str:
        """새 업무 페이지를 검토 대기 상태로 생성. 반환값은 page_id."""
        properties = _task_to_properties(task, status=TaskStatus.REVIEW_PENDING)
        page = self._client.pages.create(
            parent={"database_id": self._tasks_db},
            properties=properties,
        )
        page_id = str(page["id"])
        log.debug("새 업무 생성: id=%s title=%r", page_id[:8], task.title)
        return page_id

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        self._client.pages.update(
            page_id=task_id,
            properties={COL_STATUS: _select_prop(status.value)},
        )

    def append_task_note(self, task_id: str, note: str) -> None:
        """기존 업무에 맥락 메모 추가 — Notes 컬럼에 timestamp 와 함께 prepend.

        Notion 의 rich_text 컬럼이라 전체 텍스트를 읽고 새 줄을 prepend 해서 다시 쓴다.
        """
        page = self._client.pages.retrieve(page_id=task_id)
        existing = _extract_rich_text(page.get("properties", {}).get(COL_NOTES))
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_text = f"[{timestamp}] {note}\n{existing}" if existing else f"[{timestamp}] {note}"
        self._client.pages.update(
            page_id=task_id,
            properties={COL_NOTES: _rich_text_prop(new_text)},
        )

    # ──────────────────────────────────────────────────────────────
    # Whitelist DB
    # ──────────────────────────────────────────────────────────────
    def list_whitelisted_chatrooms(self) -> list[WindowSpec]:
        """Active 체크된 화이트리스트 톡방 목록."""
        results = self._client.data_sources.query(
            data_source_id=self._whitelist_ds,
            filter={"property": COL_WL_ACTIVE, "checkbox": {"equals": True}},
            page_size=100,
        )
        rows = results.get("results", [])
        specs: list[WindowSpec] = []
        for page in rows:
            props = page.get("properties", {})
            title = _extract_title(props.get(COL_WL_TITLE))
            window_title = _extract_rich_text(props.get(COL_WL_WINDOW_TITLE)) or title
            if title:
                specs.append(WindowSpec(title_exact=window_title or title))
        return specs

    def is_chatroom_whitelisted(self, title_exact: str) -> bool:
        if not title_exact or not title_exact.strip():
            return False
        results = self._client.data_sources.query(
            data_source_id=self._whitelist_ds,
            filter={
                "and": [
                    {"property": COL_WL_ACTIVE, "checkbox": {"equals": True}},
                    {
                        "or": [
                            {
                                "property": COL_WL_WINDOW_TITLE,
                                "rich_text": {"equals": title_exact},
                            },
                            {
                                "property": COL_WL_TITLE,
                                "title": {"equals": title_exact},
                            },
                        ]
                    },
                ]
            },
            page_size=1,
        )
        return len(results.get("results", [])) > 0

    # ──────────────────────────────────────────────────────────────
    # Inbox DB
    # ──────────────────────────────────────────────────────────────
    def fetch_new_inbox_memos(self) -> list[tuple[str, str]]:
        """Processed=false 인 Inbox 항목 — (page_id, memo_text).

        memo_text 는 Memo title + 페이지 본문 텍스트 블록을 합친 것.
        - title 만 있으면 → S4 자유 메모 시나리오 (PRD §6.1 FR-1.2)
        - 본문에 카톡 .txt 를 통째로 붙여넣은 경우 → S1 (PRD §6.1 FR-1.1)
        파일 첨부 블록(`file`, `pdf`)은 파싱하지 않음 — 텍스트만 본문에 넣어야 함.
        """
        results = self._client.data_sources.query(
            data_source_id=self._inbox_ds,
            filter={"property": COL_INBOX_PROCESSED, "checkbox": {"equals": False}},
            page_size=100,
        )
        rows = results.get("results", [])
        out: list[tuple[str, str]] = []
        for page in rows:
            page_id = str(page["id"])
            title_text = _extract_title(page.get("properties", {}).get(COL_INBOX_TITLE))
            body_text = self._fetch_page_body_text(page_id)
            combined = "\n".join(part for part in (title_text, body_text) if part)
            if combined.strip():
                out.append((page_id, combined))
        return out

    def _fetch_page_body_text(self, page_id: str) -> str:
        """페이지의 자식 블록들에서 모든 텍스트를 추출해 합친다.

        지원: paragraph, heading_1/2/3, bulleted/numbered_list_item, quote,
              callout, to_do, toggle, code.
        미지원: file, pdf, image, table 등 비텍스트 블록 — 무시.
        nested children 은 1단계만 — 깊은 중첩은 일반적이지 않음.
        """
        parts: list[str] = []
        next_cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"block_id": page_id, "page_size": 100}
            if next_cursor:
                kwargs["start_cursor"] = next_cursor
            try:
                result = self._client.blocks.children.list(**kwargs)
            except Exception:
                log.exception("페이지 본문 블록 조회 실패 — page_id=%s, title만 사용", page_id)
                return ""
            for block in result.get("results", []):
                text = _extract_block_text(block)
                if text:
                    parts.append(text)
            if not result.get("has_more"):
                break
            next_cursor = result.get("next_cursor")
        return "\n".join(parts)

    def mark_inbox_memo_processed(self, memo_id: str) -> None:
        self._client.pages.update(
            page_id=memo_id,
            properties={COL_INBOX_PROCESSED: _checkbox_prop(True)},
        )


# ──────────────────────────────────────────────────────────────
# properties 변환 헬퍼 — Notion API 의 dict 포맷
# ──────────────────────────────────────────────────────────────
def _title_prop(text: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": text}}]}


def _rich_text_prop(text: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def _select_prop(name: str) -> dict[str, Any]:
    return {"select": {"name": name}}


def _checkbox_prop(value: bool) -> dict[str, Any]:
    return {"checkbox": value}


def _date_prop(dt: datetime | date | None) -> dict[str, Any]:
    if dt is None:
        return {"date": None}
    iso = dt.isoformat() if isinstance(dt, datetime | date) else str(dt)
    return {"date": {"start": iso}}


def _extract_title(prop: dict | None) -> str:
    if not prop:
        return ""
    items = prop.get("title") or []
    return "".join(
        item.get("plain_text") or item.get("text", {}).get("content", "") for item in items
    )


def _extract_rich_text(prop: dict | None) -> str:
    if not prop:
        return ""
    items = prop.get("rich_text") or []
    return "".join(
        item.get("plain_text") or item.get("text", {}).get("content", "") for item in items
    )


# 텍스트를 담는 블록 유형 — rich_text 필드를 직접 갖는 것들
_TEXT_BLOCK_TYPES = (
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "quote",
    "callout",
    "to_do",
    "toggle",
    "code",
)


def _extract_block_text(block: dict) -> str:
    """단일 블록의 rich_text 배열에서 plain_text 를 합쳐 반환.

    파일/이미지/표 등 비텍스트 블록은 빈 문자열.
    """
    btype = block.get("type")
    if btype not in _TEXT_BLOCK_TYPES:
        return ""
    payload = block.get(btype) or {}
    items = payload.get("rich_text") or []
    return "".join(
        item.get("plain_text") or item.get("text", {}).get("content", "") for item in items
    )


def _extract_select(prop: dict | None) -> str:
    if not prop:
        return ""
    select = prop.get("select")
    return select.get("name", "") if select else ""


def _extract_checkbox(prop: dict | None) -> bool:
    if not prop:
        return False
    return bool(prop.get("checkbox", False))


def _extract_date(prop: dict | None) -> datetime | None:
    """노션 date property → datetime. 우리 시스템 컨벤션상 naive 로 통일.

    노션은 timezone-aware ISO 를 반환할 수 있는데, 우리 Scheduler 는 `datetime.now()`
    (naive) 와 비교해서 tz-aware 와 섞이면 TypeError. 항상 naive 반환.
    """
    if not prop:
        return None
    date_obj = prop.get("date")
    if not date_obj:
        return None
    start = date_obj.get("start")
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _task_to_properties(task: ExtractedTask, status: TaskStatus) -> dict[str, Any]:
    """ExtractedTask → Notion page properties dict."""
    props: dict[str, Any] = {
        COL_TITLE: _title_prop(task.title),
        COL_WHAT: _rich_text_prop(task.what),
        COL_CONTEXT: _rich_text_prop(task.context),
        COL_ASSIGNEE: _rich_text_prop(task.assignee),
        COL_SOURCE: _select_prop(task.source),
        COL_SOURCE_DETAIL: _rich_text_prop(task.source_detail),
        COL_STATUS: _select_prop(status.value),
        COL_FOLLOWUP_ENABLED: _checkbox_prop(True),
        COL_CHATROOM: _rich_text_prop(task.source_detail or ""),
    }
    if task.due_date is not None:
        props[COL_DUE] = _date_prop(task.due_date)
    return props


def _page_to_task_summary(page: dict) -> TaskSummary:
    props = page.get("properties", {})
    title = _extract_title(props.get(COL_TITLE))
    what = _extract_rich_text(props.get(COL_WHAT))
    assignee = _extract_rich_text(props.get(COL_ASSIGNEE)) or "미정"
    due = _extract_date(props.get(COL_DUE))
    status_name = _extract_select(props.get(COL_STATUS))
    chatroom = _extract_rich_text(props.get(COL_CHATROOM)) or _extract_rich_text(
        props.get(COL_SOURCE_DETAIL)
    )
    followup_enabled = _extract_checkbox(props.get(COL_FOLLOWUP_ENABLED))

    # status 문자열 → enum (모르는 값이면 진행 중 으로 fallback)
    try:
        status = TaskStatus(status_name) if status_name else TaskStatus.REVIEW_PENDING
    except ValueError:
        status = TaskStatus.IN_PROGRESS

    return TaskSummary(
        task_id=str(page["id"]),
        title=title,
        assignee=assignee,
        due_date=due,
        one_line_summary=what[:80] if what else "",
        status=status,
        chatroom_title=chatroom or "",
        followup_enabled=followup_enabled,
    )
