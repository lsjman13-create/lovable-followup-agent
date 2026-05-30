"""Inbox 경로 e2e — 노션 Inbox DB → Ollama/Claude → Tasks DB → (선택)Scheduler+Dispatch.

원래 설계 (PRD §6.1, ARCHITECTURE §4.2 Notion Inbox Poller) 의 흐름:

    사용자 → 노션 Inbox DB 새 row (Memo=텍스트, Processed=false)
                  ↓
       매 5분마다 데몬 fetch_new_inbox_memos()
                  ↓
       extractor.process_text(memo) — Ollama/Claude 호출
                  ↓
       Tasks DB INSERT + mark_inbox_memo_processed(true)
                  ↓
       (선택) Scheduler.tick + Dispatcher — 확정 업무만 발송

이 스크립트는 데몬의 `_process_inbox_once` 와 동등한 한 사이클을 수동으로 한 번 실행합니다.

사용 전:
    1) 노션 Inbox DB 에 row 직접 추가 (Memo=내용, Processed=false)
    2) 환경변수 NOTION_API_TOKEN 등록

사용법:
    uv run python scripts/integration_inbox_e2e.py --llm ollama
    # Scheduler+Dispatch 까지 가려면:
    uv run python scripts/integration_inbox_e2e.py --llm ollama --send
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import tomllib
from datetime import datetime
from pathlib import Path

with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lovable_agent.config import load_config  # noqa: E402
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
from lovable_agent.storage.notion_repo import NotionRepository  # noqa: E402
from lovable_agent.storage.sqlite_repo import SqliteRepository  # noqa: E402

log = logging.getLogger(__name__)


def _build_llm(llm_choice: str) -> LLMClient:
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


def _build_real_sender() -> KakaoSender:
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


def _run(llm_choice: str, allow_send: bool) -> int:
    log.info("=" * 70)
    log.info("Inbox 경로 e2e — 노션 Inbox → LLM → Tasks (원래 설계 그대로)")
    log.info("=" * 70)

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
    log.info("[0/4] 환경 점검 OK — token, LLM(%s)", llm_choice)

    # 데몬과 동일한 의존성 와이어링
    sqlite = SqliteRepository(":memory:")
    notion = NotionRepository(
        token=token,
        tasks_db_id=notion_cfg["tasks_db_id"],
        whitelist_db_id=notion_cfg["whitelist_db_id"],
        inbox_db_id=notion_cfg["inbox_page_id"],
    )
    cfg_obj = load_config()
    max_chars = cfg_obj.llm.max_input_chars or None
    extractor = TaskExtractor(llm=llm, repo=notion, max_input_chars=max_chars)
    if max_chars:
        log.info("Extractor — max_input_chars=%d (긴 입력 절단)", max_chars)
    scheduler = ReminderScheduler(notion=notion, sqlite=sqlite)
    notifier = Notifier()

    try:
        # ── 1) Inbox 폴링 ──
        log.info("[1/4] 노션 Inbox DB 폴링 — Processed=false 인 row 검색…")
        memos = notion.fetch_new_inbox_memos()
        log.info("[1/4] 새 메모 %d건 발견", len(memos))

        if not memos:
            log.warning("처리할 Inbox 메모가 없습니다 — 노션에서 row 추가 후 다시 실행하세요.")
            log.warning("  · 새 row, Memo 칸에 카톡 내용 붙여넣기")
            log.warning("  · Processed 체크박스 해제")
            return 1

        for memo_id, text in memos:
            preview = text[:80].replace("\n", " ")
            log.info("  · id=%s, %d자 — %r…", memo_id[:8], len(text), preview)

        # ── 2) 각 메모 처리 (데몬 _process_inbox_once 와 동일 로직) ──
        log.info("[2/4] 각 메모 LLM 추출 시작…")
        total_new = 0
        total_merged = 0
        for memo_id, text in memos:
            log.info("  → 처리 시작: id=%s (%d자)", memo_id[:8], len(text))
            t0 = datetime.now()
            outcome = extractor.process_text(text, source_label=f"inbox:{memo_id[:8]}")
            elapsed = (datetime.now() - t0).total_seconds()
            log.info(
                "  → 응답 (%.1fs) — 신규 %d / 중복 %d",
                elapsed,
                len(outcome.new_task_ids),
                len(outcome.merged_task_ids),
            )
            total_new += len(outcome.new_task_ids)
            total_merged += len(outcome.merged_task_ids)
            notion.mark_inbox_memo_processed(memo_id)
            log.info("  → Inbox row 마킹 완료 (Processed=true)")

        log.info(
            "[2/4] 모든 메모 처리 완료 — 신규 %d건 / 중복 %d건 (총 메모 %d개)",
            total_new,
            total_merged,
            len(memos),
        )

        # ── 3) Scheduler.tick (확정 + 발송조건 충족 업무만 enqueue) ──
        log.info("[3/4] Scheduler.tick — 확정된 업무 중 발송 대상 검색…")
        tick = scheduler.tick()
        pending = sqlite.list_pending(limit=10)
        log.info(
            "[3/4] enqueued=%d, skipped_too_late=%d, 큐=%d건",
            tick.enqueued,
            tick.skipped_too_late,
            len(pending),
        )
        for p in pending[:3]:
            log.info("  · [%s] %s → %s", p["scheduled_at"], p["chatroom_title"], p["message"][:60])

        # ── 4) Dispatcher (--send 일 때만) ──
        if not pending:
            log.info("[4/4] 큐가 비어있음 — 발송할 확정 업무 없음")
            log.info("=" * 70)
            log.info("✅ Inbox 경로 검증 완료 (Inbox → Tasks). 확정 후 발송은 매니저가 노션에서.")
            log.info("=" * 70)
            return 0

        if not allow_send:
            log.info("[4/4] --send 미지정 — 발송은 건너뜀. 다음 발송 예정:")
            for p in pending[:2]:
                log.info("  · → %r: %s", p["chatroom_title"], p["message"][:80])
            log.info("=" * 70)
            log.info("✅ Inbox 경로 검증 완료 (Inbox → Tasks → Scheduler. 발송 직전 멈춤)")
            log.info("=" * 70)
            return 0

        sender = _build_real_sender()
        dispatcher = SendDispatcher(
            sqlite=sqlite, sender=sender, notifier=notifier, batch_limit=1
        )
        log.info("[4/4] Dispatcher 시작 — 실 카톡 발송")
        dispatch = dispatcher.dispatch_pending()
        log.info(
            "[4/4] 결과 — attempted=%d, succeeded=%d, failed=%d",
            dispatch.attempted,
            dispatch.succeeded,
            dispatch.failed,
        )
        log.info("=" * 70)
        if dispatch.succeeded:
            log.info(
                "🎉 Inbox 경로 풀 e2e 성공 — Inbox → LLM → Tasks → Scheduler → 실 카톡 발송"
            )
        else:
            log.warning("⚠️ 발송 실패 또는 0건")
        log.info("=" * 70)
        return 0 if dispatch.succeeded == dispatch.attempted else 1

    finally:
        sqlite.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="integration_inbox_e2e",
        description="원래 설계대로 노션 Inbox → LLM → Tasks 흐름 검증",
    )
    parser.add_argument(
        "--llm",
        type=str,
        default="ollama",
        choices=("claude_cli", "ollama"),
        help="추출에 사용할 LLM 백엔드 (기본: ollama)",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Scheduler 가 큐에 enqueue 한 항목이 있으면 실 카톡 발송까지 진행",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if sys.platform != "win32" and args.send:
        log.error("실 카톡 발송은 Windows 전용. --send 빼고 실행 가능")
        return 2

    return _run(args.llm, args.send)


if __name__ == "__main__":
    sys.exit(main())
