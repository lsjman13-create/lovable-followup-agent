"""Phase 3 e2e 검증 — 분석·저장 흐름이 실 카톡 발송까지 한 번에 흘러가는지.

차이점 (vs `main.py --dry-run`):
- mock LLM / mock Notion / mock Inbox 는 그대로 유지 (Phase 4 셋업 전)
- **SendDispatcher 에 실 KakaoSender(default_steps()) 주입** ← 핵심
- mock Notion 의 시드 업무 1건을 본인 환경(=`--target`) 으로 덮어써서 실 발송 가능하게
- batch_limit=1 로 안전하게 1회 발송만

흐름:
1. 가짜 카톡 .txt → 카톡 파서 (4 메시지)
2. mock LLM Extractor → mock Notion 저장
3. mock Notion 시드 업무의 chatroom_title 을 `--target` 으로 덮어씀
4. mock Notion 화이트리스트에 `--target` 추가
5. ReminderScheduler.tick → SQLite 발송 큐 enqueue
6. SendDispatcher.dispatch_pending(batch_limit=1) → **실 KakaoSender 호출 → 실 카톡 발송**
7. 결과 보고

대상 권장: `"이승준"` (본인 본명, "나와의 채팅" 으로 잡힘 → PII 노출 0, 사고 0)

사전 조건:
- 카톡 PC 실행·로그인
- 카톡 메인 창 사이드바가 **채팅 탭** (--tab-mode chats 기본)
- `--target` 검색 시 채팅 목록에 잡힘

사용법:
    uv run python scripts/e2e_test.py --target "이승준"
    uv run python scripts/e2e_test.py --target "이승준" --dry-sender  # 실 sender 대신 mock
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 한글 인코딩 안전 설정
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lovable_agent.domain import WindowSpec  # noqa: E402
from lovable_agent.ingest.kakao_parser import format_for_llm, parse_kakao_text  # noqa: E402
from lovable_agent.output.kakao_sender import KakaoSender, SendResult  # noqa: E402
from lovable_agent.output.notifier import Notifier  # noqa: E402
from lovable_agent.output.send_dispatcher import SendDispatcher  # noqa: E402
from lovable_agent.output.steps.ensure_friends_tab import EnsureFriendsTabStep  # noqa: E402
from lovable_agent.output.steps.open_chatroom import OpenChatroomStep  # noqa: E402
from lovable_agent.output.steps.press_enter import PressEnterStep  # noqa: E402
from lovable_agent.output.steps.snapshot_hwnds import SnapshotHwndsStep  # noqa: E402
from lovable_agent.output.steps.type_message import TypeMessageStep  # noqa: E402
from lovable_agent.output.steps.verify_chatroom_title import VerifyChatroomTitleStep  # noqa: E402
from lovable_agent.process.extractor import TaskExtractor  # noqa: E402
from lovable_agent.process.mock_client import MockLLMClient  # noqa: E402
from lovable_agent.safety.whitelist import WhitelistChecker  # noqa: E402
from lovable_agent.scheduling.scheduler import ReminderScheduler  # noqa: E402
from lovable_agent.storage.mock_notion_repo import MockNotionRepository  # noqa: E402
from lovable_agent.storage.sqlite_repo import SqliteRepository  # noqa: E402

log = logging.getLogger(__name__)

_FAKE_KAKAO_TXT = """\
--------------- 2026년 5월 23일 토요일 ---------------
[김매니저] [오전 10:30] e2e 검증 — 본인에게 메시지가 가는지 확인
"""


class _DryRunSender:
    """`--dry-sender` 옵션에서 사용하는 가짜 sender — 실 카톡 호출 안 함."""

    def __init__(self) -> None:
        self.calls: list[tuple[WindowSpec, str]] = []

    def send(self, target: WindowSpec, message: str) -> SendResult:
        self.calls.append((target, message))
        return SendResult(success=True, completed_steps=["(dry-sender)"])


def _build_real_sender() -> KakaoSender:
    """Phase 2 본구현 + 본인 환경 옵션값 (tab=chats, open=double_click)."""
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


def _override_seed_for_target(notion: MockNotionRepository, target: str) -> str:
    """mock Notion 의 시드 업무 1건의 chatroom_title 을 `target` 으로 덮어쓰고
    화이트리스트에 추가. 발송 가능하게 만든다.

    Returns:
        덮어쓴 task_id (디버깅용).
    """
    active = notion.list_active_tasks()
    if not active:
        raise RuntimeError("MockNotionRepository 시드가 비어있음 — 코드 변경 의심")
    target_task = active[0]
    notion._tasks[target_task.task_id]["chatroom_title"] = target  # noqa: SLF001
    # 발송 시점이 미래라도 dispatch_pending 은 즉시 처리하므로 D-day 가 가까워야
    # 메시지 라벨이 의미있게 나옴 → due_date 를 1시간 후로 당김
    notion._tasks[target_task.task_id]["due_date"] = datetime.now() + timedelta(hours=1)  # noqa: SLF001
    # 화이트리스트에 target 추가
    notion._whitelist.append(WindowSpec(title_exact=target))  # noqa: SLF001
    return target_task.task_id


def _run_e2e(target: str, dry_sender: bool) -> int:
    log.info("=" * 60)
    log.info("E2E 검증 — Phase 3 실 카톡 발송 (대상=%r, dry_sender=%s)", target, dry_sender)
    log.info("=" * 60)

    # 의존성 와이어링
    sqlite = SqliteRepository(":memory:")
    notion = MockNotionRepository()
    llm = MockLLMClient()
    extractor = TaskExtractor(llm=llm, repo=notion)
    whitelist = WhitelistChecker(sqlite=sqlite, notion=notion)
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    notifier = Notifier()

    sender = _DryRunSender() if dry_sender else _build_real_sender()
    dispatcher = SendDispatcher(sqlite=sqlite, sender=sender, notifier=notifier, batch_limit=1)

    try:
        # 1) 카톡 파서
        messages = parse_kakao_text(_FAKE_KAKAO_TXT)
        log.info("[1/7] 카톡 파싱 — %d개 메시지", len(messages))

        # 2) Extractor → mock Notion
        outcome = extractor.process_text(format_for_llm(messages))
        log.info(
            "[2/7] Extractor — 신규 %d / 중복 %d",
            len(outcome.new_task_ids),
            len(outcome.merged_task_ids),
        )

        # 3) mock Notion 시드 덮어쓰기 (e2e 핵심)
        overridden_id = _override_seed_for_target(notion, target)
        log.info(
            "[3/7] mock Notion 시드 덮어쓰기 — task_id=%s, chatroom=%r 으로 변경",
            overridden_id[:8],
            target,
        )

        # 4) 화이트리스트 검증
        check = whitelist.check(target)
        log.info("[4/7] Whitelist — %r %s", target, "✓ 통과" if check.allowed else "✗ 차단")
        if not check.allowed:
            log.error("화이트리스트 차단 — 시드 덮어쓰기가 잘못된 듯. 중단.")
            return 2

        # 5) Scheduler.tick
        tick = scheduler.tick()
        log.info(
            "[5/7] Scheduler.tick — enqueued=%d, skipped_too_late=%d, already_queued=%d",
            tick.enqueued,
            tick.skipped_too_late,
            tick.already_queued,
        )

        pending_before = sqlite.list_pending(limit=10)
        log.info("        대기 중 큐: %d건", len(pending_before))
        for row in pending_before[:3]:
            log.info("        ↳ %s → %s", row["chatroom_title"], row["message"][:60])

        # 6) SendDispatcher → 실 KakaoSender 호출 (batch_limit=1 이라 1건만)
        log.info(
            "[6/7] SendDispatcher.dispatch_pending — batch_limit=1, %s sender",
            "DRY" if dry_sender else "**REAL KAKAO**",
        )
        if not dry_sender:
            log.info(
                "        ⚠️ 5초 내 카톡 채팅 탭 + '%s' 검색 가능 상태 유지 — 만지지 마세요", target
            )
        dispatch = dispatcher.dispatch_pending()
        log.info(
            "        결과: attempted=%d, succeeded=%d, failed=%d",
            dispatch.attempted,
            dispatch.succeeded,
            dispatch.failed,
        )

        # 7) send_history 확인
        history = sqlite.list_history(limit=5)
        log.info("[7/7] send_history — %d건 기록", len(history))
        for h in history:
            log.info(
                "        ↳ success=%s, error=%r, sent_at=%s",
                bool(h["success"]),
                h["error_detail"],
                h["sent_at"],
            )

        log.info("=" * 60)
        all_ok = dispatch.attempted >= 1 and dispatch.succeeded == dispatch.attempted
        if all_ok:
            log.info("✅ Phase 3 e2e 검증 성공 — 모든 단계 통과")
        else:
            log.warning("⚠️ Phase 3 e2e 검증 실패 — dispatch 결과 위 로그 참조")
        log.info("=" * 60)
        return 0 if all_ok else 1
    finally:
        sqlite.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="e2e_test",
        description="Phase 3 e2e 검증 — 실 카톡 발송까지 한 번에",
    )
    parser.add_argument(
        "--target",
        type=str,
        required=True,
        help="발송 대상 톡방 제목 (보통 본인 본명, '나와의 채팅' 으로 잡힘)",
    )
    parser.add_argument(
        "--dry-sender",
        action="store_true",
        help="실 KakaoSender 대신 가짜 sender 사용 (안전 점검용)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if sys.platform != "win32" and not args.dry_sender:
        log.error("실 카톡 발송은 Windows 전용. --dry-sender 로만 실행 가능")
        return 2

    return _run_e2e(args.target, args.dry_sender)


if __name__ == "__main__":
    sys.exit(main())
