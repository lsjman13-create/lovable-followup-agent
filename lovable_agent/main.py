"""엔트리포인트.

사용법:
    uv run python -m lovable_agent --dry-run    # mock 의존성으로 한 사이클
    uv run python -m lovable_agent              # 실 운영 (Phase 4 이후)

Phase 1 단계의 --dry-run 은 외부 호출 없이 다음을 수행하고 종료:
1. 설정 로딩
2. MockLLMClient + MockNotionRepository 와이어링
3. 가짜 카톡 메시지 → 4요소 추출 → 노션(가짜) 추가 → 화이트리스트 확인 → 메시지
   생성 (실제 발송 X) 까지 한 사이클을 콘솔에 출력.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

from lovable_agent.config import load_config
from lovable_agent.process.mock_client import MockLLMClient
from lovable_agent.safety.prefix import enforce_prefix
from lovable_agent.storage.mock_notion_repo import MockNotionRepository

if TYPE_CHECKING:
    from lovable_agent.process.llm_client import LLMClient
    from lovable_agent.storage.repository import NotionRepository


log = logging.getLogger("lovable_agent")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _run_dry_cycle(llm: LLMClient, repo: NotionRepository, message_prefix: str) -> int:
    """Phase 1 dry-run — 외부 호출 0건으로 한 사이클 흘려본다.

    Returns: 종료 코드 (0=정상).
    """
    log.info("=" * 60)
    log.info("DRY-RUN 시작 — 외부 호출 없이 한 사이클 시뮬레이션")
    log.info("=" * 60)

    # 1) 가짜 인입 — 카톡 익스포트 일부를 흉내낸 문자열
    fake_kakao_text = (
        "[김매니저] 다음 주 수요일까지 MOP 8월 운영 보고서 초안 공유 부탁드립니다\n"
        "[나] 네, 알겠습니다"
    )
    log.info("[1/5] 가짜 카톡 텍스트 인입: %d자", len(fake_kakao_text))

    # 2) 기존 진행 중인 업무 조회 (중복 판별 입력)
    existing = repo.list_active_tasks()
    log.info("[2/5] 기존 진행 중인 업무 %d건 조회 (중복 판별 입력)", len(existing))
    for t in existing:
        log.info("      ↳ %s (담당: %s)", t.title, t.assignee)

    # 3) Mock LLM 으로 4요소 추출
    result = llm.extract_tasks(fake_kakao_text, existing)
    log.info("[3/5] LLM 추출 결과 — 업무 %d건", len(result.tasks))
    for task in result.tasks:
        is_dup = " [중복]" if task.is_duplicate_of else ""
        log.info("      ↳ %s%s", task.title, is_dup)
        log.info("        What: %s", task.what)
        log.info("        Due: %s", task.due_date)
        log.info("        Assignee: %s", task.assignee)

    # 4) 노션(가짜) 에 검토 대기로 추가
    for task in result.tasks:
        if task.is_duplicate_of:
            repo.append_task_note(task.is_duplicate_of, f"[중복 감지] {task.what}")
            log.info("[4/5] 중복 업무에 메모 추가 → %s", task.is_duplicate_of)
        else:
            task_id = repo.add_task(task)
            log.info("[4/5] 새 업무 추가 → task_id=%s (검토 대기)", task_id)

    # 5) 메시지 한 줄 만들어보고 (실제 발송 X), 화이트리스트 검증만
    chatroom_title = "MOP 운영방"
    raw_msg = "보고서 초안 공유 마감 D-1 입니다"
    final_msg = enforce_prefix(raw_msg, message_prefix)
    is_wl = repo.is_chatroom_whitelisted(chatroom_title)
    log.info(
        "[5/5] 메시지 미리보기: '%s' / 톡방: %s / 화이트리스트: %s",
        final_msg,
        chatroom_title,
        "✓" if is_wl else "✗ (자동 발송 차단됨)",
    )

    log.info("=" * 60)
    log.info("DRY-RUN 완료 — 외부 호출 0건, 정상 종료")
    log.info("=" * 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lovable_agent",
        description="Lovable 업무 팔로업 에이전트 (MVP)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="외부 호출 없이 mock 의존성으로 한 사이클 실행 후 종료 (Phase 1)",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG 로그 출력")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="config.toml 경로 (기본: 프로젝트 루트의 config.toml)",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    config = load_config(args.config)

    if args.dry_run:
        llm: LLMClient = MockLLMClient()
        repo: NotionRepository = MockNotionRepository()
        return _run_dry_cycle(llm, repo, config.safety.message_prefix)

    # 실 운영 모드는 Phase 4에서 구현
    log.error("실 운영 모드는 Phase 4 에서 구현됩니다. 지금은 --dry-run 을 사용하세요.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
