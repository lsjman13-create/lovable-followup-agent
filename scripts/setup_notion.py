"""Notion DB 3개(Tasks / Whitelist / Inbox) 자동 생성 — 1회 setup 도구.

사용자가 노션 Integration 토큰을 발급한 뒤 한 번만 실행. notion_repo.py 가 기대하는
정확한 스키마로 DB 들을 만들고 ID 를 config.toml 에 자동 기록.

사전 조건:
1. 노션 Integration 생성: notion.so/profile/integrations → New integration
   - Type: Internal
   - Capabilities: Read/Update/Insert content
2. 발급된 토큰을 환경변수 `NOTION_API_TOKEN` 으로 등록
   - PowerShell (일시): $env:NOTION_API_TOKEN = "secret_xxx..."
   - 영구 등록: [System.Environment]::SetEnvironmentVariable("NOTION_API_TOKEN", "secret_xxx...", "User")
3. DB 들이 생성될 부모 페이지를 노션에서 만들고 Integration 에 공유
   - 페이지 우측 상단 ⋯ → Connections → Add connections → 본인 Integration 선택

사용법:
    # 부모 페이지 URL (또는 ID) 를 인자로
    uv run python scripts/setup_notion.py --parent "https://www.notion.so/.../<32자 hex>"

    # 또는 부모 페이지 ID 직접
    uv run python scripts/setup_notion.py --parent "<32자 hex>"

    # dry-run (실 생성 X, 스키마만 출력)
    uv run python scripts/setup_notion.py --parent <ID> --dry-run

실행 결과:
- Tasks / Whitelist / Inbox DB 3개 생성
- config.toml 에 DB ID 3개 자동 입력 (기존 값 있으면 덮어쓰기)
- 생성된 DB 의 노션 URL stdout 출력
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import sys
from pathlib import Path

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Notion DB 스키마 정의 (notion-client `databases.create` 의 properties 포맷)
# ──────────────────────────────────────────────────────────────
def _tasks_db_schema() -> dict:
    """Tasks DB — 업무 마스터. notion_repo.py 의 COL_* 와 일치 필수."""
    return {
        "Title": {"title": {}},
        "What": {"rich_text": {}},
        "Context": {"rich_text": {}},
        "Due Date": {"date": {}},
        "Assignee": {"rich_text": {}},
        "Source": {
            "select": {
                "options": [
                    {"name": "kakao", "color": "yellow"},
                    {"name": "manual", "color": "gray"},
                    {"name": "email", "color": "blue"},
                    {"name": "calendar", "color": "purple"},
                ],
            },
        },
        "Source Detail": {"rich_text": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "검토 대기", "color": "orange"},
                    {"name": "확정", "color": "green"},
                    {"name": "진행 중", "color": "blue"},
                    {"name": "완료", "color": "default"},
                    {"name": "취소", "color": "red"},
                ],
            },
        },
        "AI Followup Enabled": {"checkbox": {}},
        "Chatroom": {"rich_text": {}},
        "Notes": {"rich_text": {}},
    }


def _whitelist_db_schema() -> dict:
    """Whitelist DB — 자동 발송 허용 톡방 목록."""
    return {
        "Chatroom": {"title": {}},
        "Window Title": {"rich_text": {}},
        "Active": {"checkbox": {}},
        "Memo": {"rich_text": {}},
    }


def _inbox_db_schema() -> dict:
    """Inbox DB — 수동 메모 입력처. 처리됨 컬럼으로 폴링.

    ARCHITECTURE 는 'Inbox 페이지' 라고 적혀있으나, 폴링 효율·처리 마킹 편의로
    DB 로 결정 (DECISIONS 갱신).
    """
    return {
        "Memo": {"title": {}},
        "Processed": {"checkbox": {}},
        "Created": {"created_time": {}},
    }


# ──────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────
_NOTION_ID_RE = re.compile(r"([0-9a-f]{32})", re.IGNORECASE)


def _extract_page_id(parent: str) -> str | None:
    """URL 또는 ID 어느 쪽이든 32자리 hex ID 추출."""
    p = parent.strip().replace("-", "")
    m = _NOTION_ID_RE.search(p)
    return m.group(1) if m else None


def _create_db(client, parent_id: str, title: str, schema: dict, dry_run: bool) -> str:
    """notion-client 로 단일 DB 생성. dry_run 이면 schema 만 출력하고 fake ID 반환."""
    if dry_run:
        log.info("[DRY-RUN] DB 생성 스킵: %r", title)
        log.info("[DRY-RUN]   schema keys: %s", list(schema.keys()))
        return "dry-run-id"

    response = client.databases.create(
        parent={"type": "page_id", "page_id": parent_id},
        title=[{"type": "text", "text": {"content": title}}],
        properties=schema,
    )
    db_id = str(response["id"])
    url = response.get("url", "")
    log.info("✅ %s 생성 — id=%s url=%s", title, db_id, url)
    return db_id


def _update_config_toml(
    config_path: Path,
    tasks_db_id: str,
    whitelist_db_id: str,
    inbox_db_id: str,
) -> bool:
    """config.toml 의 [notion] 섹션을 새 ID 들로 갱신.

    config.toml 이 없으면 config.example.toml 복사. tomllib 는 읽기 전용이라
    단순 텍스트 치환 사용.
    """
    if not config_path.exists():
        example = _PROJECT_ROOT / "config.example.toml"
        if not example.exists():
            log.error("config.example.toml 도 없음 — 직접 config.toml 작성 필요")
            return False
        log.info("config.toml 없음 — config.example.toml 에서 복사")
        config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    text = config_path.read_text(encoding="utf-8")

    replacements = {
        "tasks_db_id": tasks_db_id,
        "whitelist_db_id": whitelist_db_id,
        "inbox_page_id": inbox_db_id,  # config.example.toml 의 키는 inbox_page_id 였으나
        # 의미상 inbox_db_id 로 변경하는 게 맞지만 호환성 유지를 위해 동일 키 사용
    }

    new_text = text
    for key, new_value in replacements.items():
        # tasks_db_id = "..." 형태를 찾아 교체
        pattern = re.compile(rf'^(\s*{key}\s*=\s*)"[^"]*"', re.MULTILINE)
        new_text = pattern.sub(rf'\1"{new_value}"', new_text)

    if new_text == text:
        log.warning("config.toml 의 키 매칭 안 됨 — 수동으로 입력 필요")
        return False

    config_path.write_text(new_text, encoding="utf-8")
    log.info("config.toml 갱신 완료: %s", config_path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup_notion",
        description="Notion DB 3개(Tasks/Whitelist/Inbox) 자동 생성 — 1회 실행",
    )
    parser.add_argument(
        "--parent",
        type=str,
        required=True,
        help="DB 들을 만들 부모 페이지 URL 또는 32자 ID. Integration 에 미리 공유 필요.",
    )
    parser.add_argument(
        "--token-env",
        type=str,
        default="NOTION_API_TOKEN",
        help="토큰을 담은 환경변수 이름 (기본: NOTION_API_TOKEN)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(_PROJECT_ROOT / "config.toml"),
        help="갱신할 config.toml 경로 (기본: 프로젝트 루트)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실 DB 생성 안 함 — 스키마만 stdout 으로 출력. 토큰 없어도 동작.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    parent_id = _extract_page_id(args.parent)
    if parent_id is None:
        log.error("부모 페이지 ID 를 인식하지 못함: %r", args.parent)
        log.error("  URL 또는 32자리 hex 문자열을 인자로 주세요.")
        return 2

    log.info("부모 페이지 ID: %s", parent_id)

    # 토큰 검증
    token = os.environ.get(args.token_env, "")
    if not args.dry_run and not token:
        log.error("환경변수 %s 가 설정되지 않음 — 노션 Integration 토큰 등록 필요", args.token_env)
        return 2

    # notion-client 생성
    client = None
    if not args.dry_run:
        try:
            from notion_client import Client
        except ImportError:
            log.error("notion-client 미설치 — uv sync 로 의존성 설치")
            return 2
        client = Client(auth=token)
        log.info("Notion Client 생성 완료")

    # DB 3개 생성
    schemas = [
        ("Lovable — Tasks", _tasks_db_schema()),
        ("Lovable — Whitelist", _whitelist_db_schema()),
        ("Lovable — Inbox", _inbox_db_schema()),
    ]
    ids: list[str] = []
    for title, schema in schemas:
        db_id = _create_db(client, parent_id, title, schema, dry_run=args.dry_run)
        ids.append(db_id)

    tasks_db, whitelist_db, inbox_db = ids

    # config.toml 갱신
    config_path = Path(args.config)
    if not args.dry_run:
        _update_config_toml(config_path, tasks_db, whitelist_db, inbox_db)

    print()
    print("=" * 60)
    print("Setup 완료")
    print("=" * 60)
    print(f"Tasks DB     : {tasks_db}")
    print(f"Whitelist DB : {whitelist_db}")
    print(f"Inbox DB     : {inbox_db}")
    if not args.dry_run:
        print(f"config.toml  : {config_path}")
        print()
        print("다음 단계: 노션에서 화이트리스트에 발송 허용 톡방을 추가하고,")
        print("           uv run python -m lovable_agent --dry-run 로 통합 확인")
    return 0


if __name__ == "__main__":
    sys.exit(main())
