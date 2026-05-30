"""엔트리포인트.

사용법:
    uv run python -m lovable_agent --dry-run    # mock 의존성으로 한 사이클 후 종료
    uv run python -m lovable_agent              # 운영 모드 — 매 분 polling + 발송 루프

운영 모드 (`--dry-run` 미지정):
- 실 ClaudeCLI, 실 NotionRepository, 실 KakaoSender
- 매 `reminder_check_interval_seconds` (기본 60초) 마다 Scheduler.tick + Dispatcher
- 매 `notion_poll_interval_seconds` (기본 300초) 마다 Notion Inbox 폴링 + Extractor
- Ctrl+C 또는 SIGTERM 으로 우아한 종료
- 파일 로그: ~/lovable-agent/logs/agent.log (회전 10개)

--dry-run 은 외부 호출 없이 다음 통합 흐름을 수행하고 종료 (Phase 3 검증용):

1. 가짜 카톡 .txt 텍스트 준비 → 카톡 파서로 메시지 분리
2. mock LLM + mock Notion 으로 4요소 추출 + 노션(가짜) 저장
3. SQLite(인메모리) 발송 큐 초기화
4. WhitelistChecker 로 톡방 검증
5. ReminderScheduler.tick 으로 due 도래 항목 enqueue
6. SendDispatcher.dispatch_pending — 발송 큐 → mock KakaoSender 호출
7. Notifier 로 결과 알림 (실제 토스트는 Windows 에서만)
"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path

# Windows cp949 콘솔에서 한국어·유니코드 문자 출력 오류 방지
with contextlib.suppress(Exception):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from lovable_agent.config import Config, load_config
from lovable_agent.domain import WindowSpec
from lovable_agent.ingest.kakao_parser import format_for_llm, parse_kakao_text
from lovable_agent.output.kakao_sender import KakaoSender, SendResult
from lovable_agent.output.notifier import Notifier
from lovable_agent.output.send_dispatcher import SendDispatcher
from lovable_agent.output.steps.ensure_friends_tab import EnsureFriendsTabStep
from lovable_agent.output.steps.open_chatroom import OpenChatroomStep
from lovable_agent.output.steps.press_enter import PressEnterStep
from lovable_agent.output.steps.snapshot_hwnds import SnapshotHwndsStep
from lovable_agent.output.steps.type_message import TypeMessageStep
from lovable_agent.output.steps.verify_chatroom_title import VerifyChatroomTitleStep
from lovable_agent.process.claude_cli_client import ClaudeCLIClient
from lovable_agent.process.extractor import TaskExtractor
from lovable_agent.process.llm_client import LLMClient
from lovable_agent.process.mock_client import MockLLMClient
from lovable_agent.process.ollama_client import OllamaClient, is_ollama_reachable
from lovable_agent.safety.whitelist import WhitelistChecker
from lovable_agent.scheduling.scheduler import ReminderScheduler
from lovable_agent.storage.mock_notion_repo import MockNotionRepository
from lovable_agent.storage.notion_repo import NotionRepository
from lovable_agent.storage.sqlite_repo import SqliteRepository

log = logging.getLogger("lovable_agent")


# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # UTF-8 stream 으로 콘솔 핸들러 생성 (cp949 인코딩 오류 방지)
    console_handler = logging.StreamHandler(sys.stdout)
    handlers: list[logging.Handler] = [console_handler]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # 일자별 회전, 10개 보존
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_file),
            when="midnight",
            backupCount=10,
            encoding="utf-8",
        )
        handlers.append(file_handler)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
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


# ──────────────────────────────────────────────────────────────
# 운영 데몬 — 실 환경 백그라운드 루프
# ──────────────────────────────────────────────────────────────
_stop_requested = False


def _request_stop(signum: int, _frame: object) -> None:
    """SIGINT/SIGTERM 핸들러 — 다음 루프 반복에서 우아한 종료."""
    global _stop_requested
    _stop_requested = True
    log.info("종료 신호 수신 (signum=%d). 현재 사이클 완료 후 종료합니다…", signum)


def _build_llm_client(config: Config) -> LLMClient:
    """config.llm.backend 에 따라 LLMClient 구현 선택.

    - "ollama": 사전 reachability 점검 — 서버 꺼져있으면 RuntimeError.
    - "claude_cli": 로그인된 Claude Code CLI 가정 (PATH 의 `claude` 실행).
    - "anthropic": 환경변수 ANTHROPIC_API_KEY 필요 (현재 구현 보류 — claude_cli 와 동일 취급).
    - "mock": 오프라인 디버깅용.
    """
    backend = (config.llm.backend or "claude_cli").lower()
    if backend == "ollama":
        if not is_ollama_reachable(config.llm.ollama_base_url):
            raise RuntimeError(
                f"Ollama 서버에 연결할 수 없습니다 ({config.llm.ollama_base_url}). "
                f"`ollama serve` 또는 Ollama 앱이 실행 중인지 확인하세요."
            )
        log.info(
            "Ollama 백엔드 — model=%s, base_url=%s",
            config.llm.ollama_model,
            config.llm.ollama_base_url,
        )
        return OllamaClient(
            model=config.llm.ollama_model,
            base_url=config.llm.ollama_base_url,
            timeout_sec=config.llm.ollama_timeout_sec,
            use_json_format=config.llm.ollama_use_json_format,
        )
    if backend == "mock":
        log.warning("LLM=mock — 운영 모드에서 mock 백엔드 사용 중. 테스트 목적인지 확인하세요.")
        return MockLLMClient()
    # claude_cli (기본) / anthropic — 현재는 둘 다 ClaudeCLIClient 사용
    return ClaudeCLIClient()


def _build_real_kakao_sender() -> KakaoSender:
    """본인 환경 정답 조합 (tab=chats, open=double_click) — 운영 기본값."""
    return KakaoSender(
        steps=[
            EnsureFriendsTabStep(expected_tab="chats"),
            SnapshotHwndsStep(),
            OpenChatroomStep(open_method="double_click"),
            VerifyChatroomTitleStep(),
            TypeMessageStep(),
            PressEnterStep(),
        ]
    )


def _run_daemon(config: Config) -> int:
    """운영 모드 — 매 분 polling + 발송 루프. Ctrl+C 로 우아한 종료."""
    log.info("=" * 60)
    log.info("운영 데몬 시작")
    log.info("=" * 60)

    # ── 의존성 검증 ──
    token = os.environ.get(config.notion.api_token_env, "")
    if not token:
        log.error("환경변수 %s 미등록 — 운영 시작 불가", config.notion.api_token_env)
        return 2
    if not config.notion.tasks_db_id:
        log.error("config.notion.tasks_db_id 비어있음 — setup_notion.py 먼저 실행")
        return 2

    # ── 실 의존성 와이어링 ──
    db_path = Path(config.paths.db_path).expanduser()
    log.info("SQLite: %s", db_path)
    sqlite = SqliteRepository(db_path)

    notion = NotionRepository(
        token=token,
        tasks_db_id=config.notion.tasks_db_id,
        whitelist_db_id=config.notion.whitelist_db_id,
        inbox_db_id=config.notion.inbox_page_id,
    )
    log.info("Notion: 연결 완료 (tasks/whitelist/inbox DB)")

    llm = _build_llm_client(config)
    max_chars = config.llm.max_input_chars or None
    extractor = TaskExtractor(llm=llm, repo=notion, max_input_chars=max_chars)
    log.info(
        "LLM: %s%s",
        llm.__class__.__name__,
        f" (max_input_chars={max_chars})" if max_chars else "",
    )

    scheduler = ReminderScheduler(
        notion=notion,
        sqlite=sqlite,
        message_prefix=config.safety.message_prefix,
        offsets_hours=config.scheduling.default_reminder_offsets_hours,
        late_threshold_hours=config.scheduling.late_reminder_threshold_hours,
    )
    notifier = Notifier()
    sender = _build_real_kakao_sender()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender, notifier=notifier)

    # ── 신호 핸들러 등록 ──
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    # ── 메인 루프 ──
    reminder_interval = config.scheduling.reminder_check_interval_seconds
    notion_poll_interval = config.scheduling.notion_poll_interval_seconds
    last_inbox_poll = 0.0
    cycle = 0

    log.info(
        "메인 루프 시작 — reminder=%ds, inbox=%ds. Ctrl+C 로 종료.",
        reminder_interval,
        notion_poll_interval,
    )

    try:
        while not _stop_requested:
            cycle += 1
            now = time.monotonic()

            # 1) Inbox 폴링 (느린 주기)
            if now - last_inbox_poll >= notion_poll_interval:
                _process_inbox_once(notion, extractor, notifier, sqlite)
                last_inbox_poll = now

            # 2) Scheduler.tick — 확정 업무 → 발송 큐
            try:
                tick = scheduler.tick()
                if tick.enqueued or tick.skipped_too_late:
                    log.info(
                        "[cycle %d] Scheduler — enqueued=%d, skipped=%d",
                        cycle,
                        tick.enqueued,
                        tick.skipped_too_late,
                    )
            except Exception as e:
                log.exception("[cycle %d] Scheduler.tick 실패 — 계속 진행", cycle)
                notifier.error(title="스케줄러 오류", body=f"업무 스케줄링 중 오류 발생: {e}")

            # 3) Dispatcher — 발송 큐 처리
            try:
                outcome = dispatcher.dispatch_pending()
                if outcome.attempted:
                    log.info(
                        "[cycle %d] Dispatcher — attempted=%d, succeeded=%d, failed=%d",
                        cycle,
                        outcome.attempted,
                        outcome.succeeded,
                        outcome.failed,
                    )
            except Exception as e:
                log.exception("[cycle %d] Dispatcher 실패 — 계속 진행", cycle)
                notifier.error(title="발송 처리 오류", body=f"발송 디스패처 실행 중 오류 발생: {e}")

            # 4) 다음 사이클 대기 — 중단 시 조기 종료
            for _ in range(reminder_interval):
                if _stop_requested:
                    break
                time.sleep(1.0)
    finally:
        sqlite.close()
        log.info("=" * 60)
        log.info("운영 데몬 종료 — %d cycles 실행", cycle)
        log.info("=" * 60)
    return 0


def _process_inbox_once(notion: NotionRepository, extractor: TaskExtractor, notifier: Notifier, sqlite: SqliteRepository) -> None:
    """Inbox DB 의 미처리 메모들을 한 번에 처리."""
    try:
        memos = notion.fetch_new_inbox_memos()
    except Exception as e:
        log.exception("Inbox 폴링 실패 — 다음 사이클에서 재시도")
        notifier.error(title="Inbox 폴링 오류", body=f"Notion 메모를 가져오는데 실패했습니다: {e}")
        return
    if not memos:
        return
    log.info("Inbox — 새 메모 %d건 처리 시작", len(memos))
    for memo_id, text in memos:
        try:
            # 카톡 파서를 통한 해시 기반 중복 필터링
            msgs = parse_kakao_text(text)
            hashes_to_mark = []
            if msgs:
                all_hashes = [m.message_hash for m in msgs]
                new_hashes_set = set(sqlite.filter_new_messages(all_hashes))
                
                new_msgs = [m for m in msgs if m.message_hash in new_hashes_set]
                
                if not new_msgs:
                    log.info("Inbox [%s] — 중복 대화만 존재하여 분석을 건너뜁니다.", memo_id[:8])
                    notion.mark_inbox_memo_processed(memo_id)
                    continue
                
                log.info("Inbox [%s] — 총 %d개 메시지 중 새로운 메시지 %d건만 분석 진행", memo_id[:8], len(msgs), len(new_msgs))
                text_to_process = format_for_llm(new_msgs, max_messages=0)
                hashes_to_mark = [m.message_hash for m in new_msgs]
            else:
                # 카톡 포맷이 아닌 일반 메모
                text_to_process = text
                
            outcome = extractor.process_text(text_to_process, source_label=f"inbox:{memo_id[:8]}")
            
            if hashes_to_mark:
                sqlite.mark_messages_processed(hashes_to_mark)
                
            log.info(
                "Inbox [%s] — 신규 %d / 중복 %d",
                memo_id[:8],
                len(outcome.new_task_ids),
                len(outcome.merged_task_ids),
            )
            notion.mark_inbox_memo_processed(memo_id)
        except Exception as e:
            log.exception("Inbox [%s] 처리 실패 — 다음 사이클에서 재시도", memo_id[:8])
            notifier.error(title="작업 추출 오류", body=f"메모에서 작업을 추출하는데 실패했습니다: {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lovable_agent",
        description="Lovable 업무 팔로업 에이전트",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="외부 호출 없이 mock 의존성으로 한 사이클 실행 후 종료",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG 로그 출력")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="config.toml 경로 (기본: 프로젝트 루트의 config.toml)",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="파일 로깅 비활성화 (운영 모드에서만 영향)",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)

    # 파일 로그 경로 — 운영 모드일 때만 활성화
    log_file: Path | None = None
    if not args.dry_run and not args.no_log_file:
        log_file = Path(config.paths.db_path).expanduser().parent / "logs" / "agent.log"

    _setup_logging(args.verbose, log_file=log_file)

    if args.dry_run:
        return _run_dry_cycle(config)
    return _run_daemon(config)


if __name__ == "__main__":
    sys.exit(main())
