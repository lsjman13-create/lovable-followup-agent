"""진정한 e2e — 실 카톡 .txt → 실 Claude CLI → 실 노션 → 실 카톡 발송.

Mock 0개. 모든 외부 시스템 실 호출:
- ClaudeCLIClient: claude -p 비대화형 호출 (~17s)
- NotionRepository: 실 노션 API (Tasks DB CRUD + Whitelist 추가)
- KakaoSender: 본인 PC 카톡 자동화 (chats 탭 + 더블클릭)
- SQLite: 인메모리 발송 큐

흐름 (8단계):
1. 환경 점검 (token, config.toml, claude CLI)
2. 노션 Whitelist 에 `--target` 톡방 추가 (이미 있으면 스킵)
3. 카톡 .txt 파일 파싱
4. ClaudeCLIClient.extract_tasks (실 Claude AI)
5. 추출된 첫 업무를 노션 Tasks 에 add_task (검토 대기 상태)
6. 그 업무를 'patch' — Status=확정, Chatroom=`--target`, Due Date=현재+1시간,
   AI Followup Enabled=true (실 운영에서는 매니저가 수동으로 하는 단계)
7. ReminderScheduler.tick → SQLite send_queue
8. SendDispatcher (실 KakaoSender) → 본인 카톡 발송 1건

PII 주의:
- 카톡 .txt 분석 결과가 노션 Tasks DB 에 영구 저장됨 (Context 컬럼 등)
- 사용 후 본인이 노션에서 직접 정리 권장
- 채팅 출력은 마스킹된 요약만

사용법:
    uv run python scripts/integration_e2e.py \\
        --kakao-txt "KakaoTalk_20260523_2138_09_263_group.txt" \\
        --target "이승준"

    # 8단계 (실 카톡 발송) 직전 멈춤 — 안전 점검
    uv run python scripts/integration_e2e.py --kakao-txt <...> --target <...> --no-send
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lovable_agent.config import load_config  # noqa: E402
from lovable_agent.domain import TaskStatus  # noqa: E402
from lovable_agent.ingest.kakao_parser import format_for_llm, parse_kakao_file  # noqa: E402
from lovable_agent.output.kakao_sender import KakaoSender  # noqa: E402
from lovable_agent.output.notifier import Notifier  # noqa: E402
from lovable_agent.output.send_dispatcher import SendDispatcher  # noqa: E402
from lovable_agent.output.steps.ensure_friends_tab import EnsureFriendsTabStep  # noqa: E402
from lovable_agent.output.steps.open_chatroom import OpenChatroomStep  # noqa: E402
from lovable_agent.output.steps.press_enter import PressEnterStep  # noqa: E402
from lovable_agent.output.steps.snapshot_hwnds import SnapshotHwndsStep  # noqa: E402
from lovable_agent.output.steps.type_message import TypeMessageStep  # noqa: E402
from lovable_agent.output.steps.verify_chatroom_title import VerifyChatroomTitleStep  # noqa: E402
from lovable_agent.process.claude_cli_client import (  # noqa: E402
    ClaudeCLIClient,
    ensure_claude_cli_available,
)
from lovable_agent.process.extractor import TaskExtractor  # noqa: E402
from lovable_agent.process.llm_client import LLMClient  # noqa: E402
from lovable_agent.process.ollama_client import (  # noqa: E402
    OllamaClient,
    is_ollama_reachable,
)
from lovable_agent.scheduling.scheduler import ReminderScheduler  # noqa: E402
from lovable_agent.storage.notion_repo import (  # noqa: E402
    COL_CHATROOM,
    COL_DUE,
    COL_FOLLOWUP_ENABLED,
    COL_STATUS,
    COL_WL_ACTIVE,
    COL_WL_TITLE,
    COL_WL_WINDOW_TITLE,
    NotionRepository,
)
from lovable_agent.storage.sqlite_repo import SqliteRepository  # noqa: E402

log = logging.getLogger(__name__)


def _build_real_sender() -> KakaoSender:
    """본인 환경 정답 조합 (tab=chats, open=double_click)."""
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


def _ensure_whitelist_entry(repo: NotionRepository, target: str) -> str | None:
    """Whitelist 에 `target` 이 없으면 추가. 이미 있으면 스킵.

    Returns:
        새로 만든 page_id (이미 있으면 None).
    """
    if repo.is_chatroom_whitelisted(target):
        log.info("Whitelist — %r 이미 등록됨, 스킵", target)
        return None

    page = repo._client.pages.create(  # noqa: SLF001 — 1회용 inline 호출
        parent={"database_id": repo._whitelist_db},  # noqa: SLF001
        properties={
            COL_WL_TITLE: {"title": [{"type": "text", "text": {"content": target}}]},
            COL_WL_WINDOW_TITLE: {"rich_text": [{"type": "text", "text": {"content": target}}]},
            COL_WL_ACTIVE: {"checkbox": True},
        },
    )
    page_id = str(page["id"])
    log.info("Whitelist — %r 새로 추가 (page_id=%s)", target, page_id[:8])
    return page_id


def _patch_task_for_send(
    repo: NotionRepository,
    task_id: str,
    chatroom: str,
    due_date: datetime,
) -> None:
    """추가된 업무를 즉시 '확정' + 발송 조건 충족시키는 patch.

    실 운영에서는 매니저가 노션에서 직접 검토·확정하는 단계.
    """
    repo._client.pages.update(  # noqa: SLF001 — 1회용 inline 호출
        page_id=task_id,
        properties={
            COL_STATUS: {"select": {"name": TaskStatus.CONFIRMED.value}},
            COL_CHATROOM: {"rich_text": [{"type": "text", "text": {"content": chatroom}}]},
            COL_DUE: {"date": {"start": due_date.isoformat()}},
            COL_FOLLOWUP_ENABLED: {"checkbox": True},
        },
    )
    log.info(
        "Tasks — task_id=%s 를 확정으로 patch (chatroom=%r, due=%s)",
        task_id[:8],
        chatroom,
        due_date.isoformat(),
    )


def _build_llm(llm_choice: str) -> LLMClient:
    """--llm 인자에 따라 LLMClient 선택. config.toml [llm] 의 Ollama 옵션을 재사용."""
    if llm_choice == "ollama":
        cfg = load_config()
        if not is_ollama_reachable(cfg.llm.ollama_base_url):
            raise RuntimeError(
                f"Ollama 서버 연결 실패 ({cfg.llm.ollama_base_url}). "
                f"`ollama serve` 또는 Ollama 앱 실행 확인."
            )
        log.info(
            "LLM=Ollama — model=%s, base_url=%s",
            cfg.llm.ollama_model,
            cfg.llm.ollama_base_url,
        )
        return OllamaClient(
            model=cfg.llm.ollama_model,
            base_url=cfg.llm.ollama_base_url,
            timeout_sec=cfg.llm.ollama_timeout_sec,
            use_json_format=cfg.llm.ollama_use_json_format,
        )
    ensure_claude_cli_available()
    log.info("LLM=ClaudeCLI")
    return ClaudeCLIClient()


def _run(
    kakao_txt: Path,
    target: str,
    no_send: bool,
    llm_choice: str,
) -> int:
    # ── 1. 환경 점검 ──
    log.info("=" * 70)
    log.info("진정한 e2e — 실 카톡 → 실 Claude → 실 노션 → 실 카톡 발송")
    log.info("=" * 70)

    if not kakao_txt.exists():
        log.error("카톡 파일 없음: %s", kakao_txt)
        return 2

    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        log.error("NOTION_API_TOKEN 환경변수 미등록")
        return 2

    try:
        llm = _build_llm(llm_choice)
    except RuntimeError as e:
        log.error("%s", e)
        return 2

    with open(_PROJECT_ROOT / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    notion_cfg = cfg["notion"]
    log.info("[0/8] 환경 점검 OK — token, LLM(%s), config.toml 정상", llm_choice)

    # ── 의존성 와이어링 (실 컴포넌트) ──
    sqlite = SqliteRepository(":memory:")
    notion = NotionRepository(
        token=token,
        tasks_db_id=notion_cfg["tasks_db_id"],
        whitelist_db_id=notion_cfg["whitelist_db_id"],
        inbox_db_id=notion_cfg["inbox_page_id"],
    )
    extractor = TaskExtractor(llm=llm, repo=notion)
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    notifier = Notifier()
    sender = _build_real_sender() if not no_send else None

    try:
        # ── 2. Whitelist 추가 ──
        _ensure_whitelist_entry(notion, target)
        log.info("[2/8] Whitelist 준비 완료")

        # ── 3. 카톡 파싱 ──
        messages = parse_kakao_file(kakao_txt)
        log.info("[3/8] 카톡 파싱 — %d개 메시지 (%s)", len(messages), kakao_txt.name)

        # ── 4. 실 LLM 추출 ──
        llm_input = format_for_llm(messages)
        log.info(
            "[4/8] %s 호출 시작 (입력 %d자, 응답 수초~수분 소요)",
            llm.__class__.__name__,
            len(llm_input),
        )
        t0 = datetime.now()
        outcome = extractor.process_text(llm_input, source_label=kakao_txt.name)
        elapsed = (datetime.now() - t0).total_seconds()
        log.info(
            "[4/8] %s 응답 (%.1fs) — 신규 %d / 중복 %d",
            llm.__class__.__name__,
            elapsed,
            len(outcome.new_task_ids),
            len(outcome.merged_task_ids),
        )
        new_ids = outcome.new_task_ids
        if new_ids:
            # 신규 업무 있으면 첫 번째를 발송 대상으로
            first_task_id = new_ids[0]
            log.info(
                "[5/8] 노션 Tasks 에 신규 %d건 추가 — 첫 업무 id=%s 로 e2e 진행",
                len(new_ids),
                first_task_id[:8],
            )
            # ── 6. 그 업무를 즉시 '확정' + chatroom + due_date patch ──
            due = datetime.now() + timedelta(hours=1)
            _patch_task_for_send(notion, first_task_id, chatroom=target, due_date=due)
            log.info(
                "[6/8] 업무 patch — Status=확정, Chatroom=%r, Due=%s",
                target,
                due.strftime("%H:%M"),
            )
        else:
            # 신규 없음 — 모두 중복으로 처리됨. 기존 확정 업무 중 발송 가능한 것 활용
            log.info("[5/8] 신규 업무 0건 (모두 중복) — 기존 확정 업무에서 발송 대상 검색")
            active = notion.list_active_tasks()
            confirmed_ready = [
                t
                for t in active
                if t.status == TaskStatus.CONFIRMED
                and t.chatroom_title == target
                and t.due_date is not None
                and t.followup_enabled
            ]
            if not confirmed_ready:
                log.warning(
                    "발송 가능한 확정 업무가 없음 (chatroom=%r + due + followup) — 종료", target
                )
                return 1
            first_task_id = confirmed_ready[0].task_id
            log.info(
                "[5/8] 기존 확정 업무 %d건 발견 — id=%s 로 e2e 진행",
                len(confirmed_ready),
                first_task_id[:8],
            )
            log.info("[6/8] patch 스킵 (이미 확정 상태)")

        # ── 7. Scheduler.tick → enqueue ──
        tick = scheduler.tick()
        pending = sqlite.list_pending(limit=10)
        log.info(
            "[7/8] Scheduler.tick — enqueued=%d, skipped_too_late=%d, 현재 큐=%d건",
            tick.enqueued,
            tick.skipped_too_late,
            len(pending),
        )

        # 우리 patch 한 업무의 발송이 큐에 들었는지 — 그것만 발송 (다른 큐 항목 제외)
        our_pending = [p for p in pending if p["task_id"] == first_task_id]
        if not our_pending:
            log.error("Patch 한 업무가 큐에 안 들어감 — 6시간 룰로 스킵됐을 가능성. 종료.")
            return 1

        # ── 8. SendDispatcher → 실 KakaoSender ──
        if no_send:
            log.info("[8/8] --no-send 옵션 — 실 카톡 발송 건너뜀. 다음 메시지 발송 예정:")
            for p in our_pending[:2]:
                log.info("        ↳ → %r: %s", p["chatroom_title"], p["message"][:80])
            log.info("=" * 70)
            log.info("✅ e2e 검증 완료 (실 발송 직전까지) — Whitelist, Tasks, Scheduler 모두 정상")
            log.info("=" * 70)
            return 0

        # 다른 큐 항목들 임시 skip 처리 (우리 업무만 처리)
        for p in pending:
            if p["task_id"] != first_task_id:
                sqlite.update_send_status(p["id"], "queued")  # no-op but explicit

        log.info(
            "[8/8] SendDispatcher 시작 — 실 카톡 발송 (사용자: 채팅 탭 + %r 검색 가능 유지)", target
        )
        dispatcher = SendDispatcher(sqlite=sqlite, sender=sender, notifier=notifier, batch_limit=1)
        dispatch = dispatcher.dispatch_pending()
        log.info(
            "[8/8] 결과 — attempted=%d, succeeded=%d, failed=%d",
            dispatch.attempted,
            dispatch.succeeded,
            dispatch.failed,
        )

        # ── 검증 ──
        history = sqlite.list_history()
        all_ok = dispatch.attempted == 1 and dispatch.succeeded == 1 and len(history) == 1
        log.info("=" * 70)
        if all_ok:
            log.info("🎉 진정한 e2e 검증 성공 — 실 카톡, 실 Claude, 실 노션, 실 발송 모두 정상")
            log.info("    → 본인 카톡 '나와의 채팅' 확인하시면 메시지 1건 도착해있을 겁니다")
            log.info("    → 노션 Tasks DB 에 e2e 로 추가된 업무 1건이 '확정' 상태로 있습니다")
            log.info("    → PII 정리 원하시면 노션에서 그 업무 직접 삭제 가능")
        else:
            log.warning(
                "⚠️ e2e 일부 실패 — dispatch attempted=%d succeeded=%d",
                dispatch.attempted,
                dispatch.succeeded,
            )
        log.info("=" * 70)
        return 0 if all_ok else 1

    finally:
        sqlite.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="integration_e2e",
        description="진정한 e2e — Mock 0개, 모든 외부 시스템 실 호출",
    )
    parser.add_argument(
        "--kakao-txt",
        type=str,
        required=True,
        help="분석할 카톡 .txt 익스포트 파일 경로",
    )
    parser.add_argument(
        "--target",
        type=str,
        required=True,
        help="발송 대상 톡방 제목 (보통 본인 본명, '나와의 채팅' 으로 잡힘)",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Step 8 (실 카톡 발송) 건너뜀 — 안전 점검용",
    )
    parser.add_argument(
        "--llm",
        type=str,
        default="claude_cli",
        choices=("claude_cli", "ollama"),
        help="추출에 사용할 LLM 백엔드 (기본: claude_cli)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if sys.platform != "win32" and not args.no_send:
        log.error("실 카톡 발송은 Windows 전용. --no-send 로만 실행 가능")
        return 2

    return _run(Path(args.kakao_txt), args.target, args.no_send, args.llm)


if __name__ == "__main__":
    sys.exit(main())
