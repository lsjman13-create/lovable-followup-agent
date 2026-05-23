"""엔트리포인트.

사용법:
    uv run python -m lovable_agent --dry-run    # mock 의존성으로 한 사이클
    uv run python -m lovable_agent              # 실 운영 (Phase 4 이후)

--dry-run 은 외부 호출 없이 다음 통합 흐름을 수행하고 종료:

1. 가짜 카톡 .txt 텍스트 준비 → 카톡 파서로 메시지 분리
2. mock LLM + mock Notion 으로 4요소 추출 + 노션(가짜) 저장
3. SQLite(인메모리) 발송 큐 초기화
4. WhitelistChecker 로 톡방 검증
5. ReminderScheduler.tick 으로 due 도래 항목 enqueue
6. **SendDispatcher.dispatch_pending — 발송 큐 → mock KakaoSender 호출**
   send_history 기록 + 큐 상태 갱신 (실 카톡 발송 X)
7. Notifier 로 결과 알림 (실제 토스트는 Windows 에서만)
"""

from __future__ import annotations

import argparse
import logging
import sys

from lovable_agent.config import load_config
from lovable_agent.domain import WindowSpec
from lovable_agent.ingest.kakao_parser import format_for_llm, parse_kakao_text
from lovable_agent.output.kakao_sender import SendResult
from lovable_agent.output.notifier import Notifier
from lovable_agent.output.send_dispatcher import SendDispatcher
from lovable_agent.process.extractor import TaskExtractor
from lovable_agent.process.mock_client import MockLLMClient
from lovable_agent.safety.whitelist import WhitelistChecker
from lovable_agent.scheduling.scheduler import ReminderScheduler
from lovable_agent.storage.mock_notion_repo import MockNotionRepository
from lovable_agent.storage.sqlite_repo import SqliteRepository

log = logging.getLogger("lovable_agent")


# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


# ──────────────────────────────────────────────────────────────
# Dry-run 통합 시나리오
# ──────────────────────────────────────────────────────────────
_FAKE_KAKAO_TXT = """\
--------------- 2026년 5월 23일 토요일 ---------------
[김매니저] [오전 10:30] 다음 주 수요일까지 MOP 8월 운영 보고서 초안 부탁드립니다
[나] [오전 10:31] 네, 알겠습니다. 5월 27일까지 공유드릴게요
[김매니저] [오전 10:32] 회의 내용도 같이 정리해 주세요
[박팀장] [오후 2:00] 6월 첫 주 GGE 일정 확정해주세요
"""


class _MockKakaoSender:
    """Dry-run 용 가짜 KakaoSender — 실제 카톡 호출 없이 항상 성공 반환.

    실 발송은 Phase 5 운영 단계에서 `KakaoSender(default_steps())` 로 교체.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[WindowSpec, str]] = []

    def send(self, target: WindowSpec, message: str) -> SendResult:
        self.calls.append((target, message))
        return SendResult(
            success=True,
            completed_steps=[
                "ensure_friends_tab",
                "snapshot_hwnds",
                "open_chatroom",
                "verify_chatroom_title",
                "type_message",
                "press_enter",
            ],
        )


def _run_dry_cycle(config) -> int:
    """Phase 3 dry-run — 카톡 파서 + Extractor + SQLite + 스케줄러 + 화이트리스트
    통합 시나리오. 외부 호출 0건.
    """
    log.info("=" * 60)
    log.info("DRY-RUN 시작 (Phase 3 통합 시나리오) — 외부 호출 0건")
    log.info("=" * 60)

    # 의존성 와이어링
    sqlite = SqliteRepository(":memory:")
    notion = MockNotionRepository()
    llm = MockLLMClient()
    extractor = TaskExtractor(llm=llm, repo=notion)
    whitelist = WhitelistChecker(sqlite=sqlite, notion=notion)
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    notifier = Notifier()
    mock_sender = _MockKakaoSender()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=mock_sender, notifier=notifier)

    try:
        # 1) 카톡 파서
        messages = parse_kakao_text(_FAKE_KAKAO_TXT)
        log.info("[1/7] 카톡 .txt 파싱 — %d개 메시지 추출", len(messages))
        for m in messages[:3]:
            log.info("      ↳ %s | %s: %s", m.timestamp, m.speaker, m.body[:40])

        # 2) LLM 추출 + Notion 저장 (mock)
        llm_input = format_for_llm(messages)
        outcome = extractor.process_text(llm_input, source_label="가짜 톡방 (dry-run)")
        log.info(
            "[2/7] Extractor — 신규 %d건 / 중복 병합 %d건 (총 추출 %d건)",
            len(outcome.new_task_ids),
            len(outcome.merged_task_ids),
            outcome.raw_extracted_count,
        )

        # 3) 화이트리스트 더블체크 시연
        check_ok = whitelist.check("MOP 운영방")
        check_no = whitelist.check("등록되지 않은 톡방")
        log.info(
            "[3/7] Whitelist — 'MOP 운영방' %s | '등록되지 않은 톡방' %s",
            "✓ 통과" if check_ok.allowed else "✗ 차단",
            "✓ 통과" if check_no.allowed else "✗ 차단",
        )

        # 4) Scheduler tick — 확정된 시드 업무가 발송 큐에 enqueue 되어야 함
        tick = scheduler.tick()
        log.info(
            "[4/7] Scheduler.tick — enqueued=%d, skipped_too_late=%d, already_queued=%d",
            tick.enqueued,
            tick.skipped_too_late,
            tick.already_queued,
        )

        # 5) 발송 큐 미리보기 (dispatch 전 상태)
        pending = sqlite.list_pending(limit=5)
        log.info("[5/7] send_queue 미리보기 — %d건 대기 중", len(pending))
        for row in pending[:3]:
            log.info(
                "      ↳ [%s] %s → %s",
                row["scheduled_at"],
                row["chatroom_title"],
                row["message"][:60],
            )

        # 6) SendDispatcher — Scheduler 가 enqueue 한 항목들을 mock sender 로 처리
        #    실 운영(Phase 5) 에선 mock_sender 대신 KakaoSender(default_steps()) 사용.
        dispatch = dispatcher.dispatch_pending()
        log.info(
            "[6/7] SendDispatcher — attempted=%d, succeeded=%d, failed=%d (mock sender)",
            dispatch.attempted,
            dispatch.succeeded,
            dispatch.failed,
        )
        for target, msg in mock_sender.calls[:3]:
            log.info("      ↳ → %s: %s", target.title_exact, msg[:60])

        # 7) Notifier — 화이트리스트에 없는 톡방을 사용자에게만 알림
        if not check_no.allowed:
            notifier.warning(
                title="수동 처리 필요",
                body=f"'{check_no.chatroom}' 은(는) 화이트리스트에 없어 자동 발송이 차단됐습니다.",
            )
            log.info(
                "[7/7] Notifier 호출 완료 (last_level=%s)", notifier.last_notification.level.value
            )
        else:
            log.info("[7/7] Notifier — 알릴 사항 없음")

        log.info("=" * 60)
        log.info(
            "DRY-RUN 완료 — 외부 호출 0건, prefix=%r 강제됨",
            config.safety.message_prefix,
        )
        log.info("=" * 60)
        return 0
    finally:
        sqlite.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lovable_agent",
        description="Lovable 업무 팔로업 에이전트 (MVP)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="외부 호출 없이 mock 의존성으로 한 사이클 실행 후 종료 (Phase 1~3)",
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
        return _run_dry_cycle(config)

    log.error("실 운영 모드는 Phase 4 에서 구현됩니다. 지금은 --dry-run 을 사용하세요.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
