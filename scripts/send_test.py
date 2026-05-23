"""실 카톡 발송 검증 도구 — Phase 2 본구현이 본인 PC 에서 실제로 동작하는지 확인.

기본 대상은 **"나와의 채팅"** — PII 노출 0, 자기 자신에게만 메시지가 가므로 사고
시에도 영향 없음. 첫 검증은 반드시 이걸로.

사용법:
    # 1) 사전 점검만 (메시지 입력 직전까지) — 친구 탭·검색·Alt+Enter·제목 검증
    uv run python scripts/send_test.py --preflight

    # 2) 실 발송 1회 (안전 확인용)
    uv run python scripts/send_test.py --count 1

    # 3) 5회 연속 발송 (PLAN Phase 2 완료 기준)
    uv run python scripts/send_test.py --count 5

    # 다른 톡방 (PII 주의 — 결과 콘솔에 톡방명 노출됨)
    uv run python scripts/send_test.py --target "MOP 운영방" --count 1

사전 조건:
- 카톡 PC 가 실행·로그인 상태
- 카톡 메인 창 사이드바가 **친구 탭**에 있음 (자동 전환 불가)
- 대상 톡방이 친구 목록에서 검색 가능
- 검사 중 카톡 창을 만지지 않을 것

PRD §NFR-1 / ARCHITECTURE §4.6 의 모든 안전장치가 실 환경에서 통과하는지 확인.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# argparse --help 출력에서 한글 em dash 가 cp949 콘솔에 깨지는 것을 막기 위해
# 모듈 import 단계에서 stdout/stderr 를 utf-8 로 재설정
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

# 프로젝트 루트를 sys.path 에 추가 (uv run python scripts/X.py 형태에서 import 위해)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lovable_agent.domain import WindowSpec  # noqa: E402
from lovable_agent.output import hwnd_utils, window_spec  # noqa: E402
from lovable_agent.output.kakao_sender import KakaoSender, SendResult  # noqa: E402
from lovable_agent.output.steps.ensure_friends_tab import EnsureFriendsTabStep  # noqa: E402
from lovable_agent.output.steps.open_chatroom import OpenChatroomStep, OpenMethod  # noqa: E402
from lovable_agent.output.steps.press_enter import PressEnterStep  # noqa: E402
from lovable_agent.output.steps.snapshot_hwnds import SnapshotHwndsStep  # noqa: E402
from lovable_agent.output.steps.type_message import TypeMessageStep  # noqa: E402
from lovable_agent.output.steps.verify_chatroom_title import VerifyChatroomTitleStep  # noqa: E402

log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_message(idx: int, total: int) -> str:
    """매 발송마다 일련번호·타임스탬프를 포함한 메시지."""
    ts = datetime.now().strftime("%H:%M:%S")
    return f"테스트 [{idx}/{total}] · 자동 검증 · {ts}"


def _close_existing_chat(target_title: str) -> bool:
    """이미 열려있는 동명 채팅창이 있으면 닫음. HWND diff 정확성을 위해."""
    existing = window_spec.find_chat_by_title(target_title)
    if existing is not None:
        log.info("기존 채팅창 정리: %s '%s' 닫는 중...", hex(existing), target_title)
        hwnd_utils.post_close(existing)
        time.sleep(0.8)  # WM_CLOSE 처리 대기
        return True
    return False


def _build_steps_for_preflight(open_method: OpenMethod, tab_mode: str) -> list:
    return [
        EnsureFriendsTabStep(expected_tab=tab_mode),  # type: ignore[arg-type]
        SnapshotHwndsStep(),
        OpenChatroomStep(open_method=open_method),
        VerifyChatroomTitleStep(),
    ]


def _build_steps_for_full_send(open_method: OpenMethod, tab_mode: str) -> list:
    return [
        EnsureFriendsTabStep(expected_tab=tab_mode),  # type: ignore[arg-type]
        SnapshotHwndsStep(),
        OpenChatroomStep(open_method=open_method),
        VerifyChatroomTitleStep(),
        TypeMessageStep(),
        PressEnterStep(),
    ]


def _run_preflight(target: WindowSpec, open_method: OpenMethod, tab_mode: str) -> SendResult:
    """발송 직전까지만 — type_message / press_enter 는 실행하지 않음."""
    log.info("=" * 60)
    log.info(
        "PREFLIGHT — 탭검증/검색/창오픈/제목검증 까지만 (실 메시지 X) | tab=%s | open_method=%s",
        tab_mode,
        open_method,
    )
    log.info("=" * 60)

    _close_existing_chat(target.title_exact)

    sender = KakaoSender(steps=_build_steps_for_preflight(open_method, tab_mode))
    result = sender.send(target, "(preflight)")

    # 검증용으로 열린 채팅창은 즉시 닫음
    time.sleep(0.5)
    _close_existing_chat(target.title_exact)
    return result


def _run_sends(
    target: WindowSpec,
    count: int,
    inter_delay_sec: float,
    open_method: OpenMethod,
    tab_mode: str,
) -> list[SendResult]:
    """N회 실 발송."""
    log.info("=" * 60)
    log.info(
        "실 발송 — %d회 연속, 톡방=%r, 사이=%.1f초, tab=%s, open_method=%s",
        count,
        target.title_exact,
        inter_delay_sec,
        tab_mode,
        open_method,
    )
    log.info("=" * 60)

    sender = KakaoSender(steps=_build_steps_for_full_send(open_method, tab_mode))
    results: list[SendResult] = []
    for i in range(1, count + 1):
        # 매 사이클 시작 시 기존 채팅창 정리 (HWND diff 정확성)
        _close_existing_chat(target.title_exact)
        time.sleep(0.3)

        msg = _build_message(i, count)
        log.info("[%d/%d] 발송 시작: %r", i, count, msg)
        result = sender.send(target, msg)
        results.append(result)

        status = "✅ 성공" if result.success else "❌ 실패"
        log.info(
            "[%d/%d] %s — completed=%s, failed_step=%s",
            i,
            count,
            status,
            result.completed_steps,
            result.failed_step,
        )
        if not result.success:
            log.warning("[%d/%d] 실패 사유: %s", i, count, result.error_reason)
            # 첫 실패에서 중단 — 사용자가 원인 파악할 수 있게
            log.warning("→ 안전 측에서 중단합니다. 카톡 상태 확인 후 재시도하세요.")
            break

        if i < count:
            time.sleep(inter_delay_sec)

    return results


def _print_summary(results: list[SendResult], total: int) -> None:
    successes = sum(1 for r in results if r.success)
    failures = sum(1 for r in results if not r.success)
    log.info("=" * 60)
    log.info(
        "결과 요약: 시도 %d / %d, 성공 %d, 실패 %d",
        len(results),
        total,
        successes,
        failures,
    )
    log.info("=" * 60)
    for i, r in enumerate(results, 1):
        status = "✓" if r.success else "✗"
        steps_str = " → ".join(r.completed_steps) if r.completed_steps else "-"
        log.info("  [%d] %s steps=[%s] failed=%s", i, status, steps_str, r.failed_step or "-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="send_test",
        description="실 카톡 발송 검증 도구 (Phase 2 완료 기준)",
    )
    parser.add_argument(
        "--target",
        type=str,
        default="나와의 채팅",
        help="발송 대상 톡방 제목 (기본: '나와의 채팅' — PII 노출 0)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="발송 횟수 (기본: 1). PLAN Phase 2 완료 기준은 5회 연속 성공.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="메시지 입력 직전까지만 — 친구탭/검색/Alt+Enter/제목검증 만. 실 메시지 X.",
    )
    parser.add_argument(
        "--inter-delay",
        type=float,
        default=3.0,
        help="발송 간 대기 시간 (초). 기본 3초.",
    )
    parser.add_argument(
        "--open-method",
        type=str,
        choices=("alt_enter", "enter_only", "double_click"),
        default="alt_enter",
        help=(
            "검색 결과를 별도 창으로 여는 방법. 카톡 버전·환경마다 다름. "
            "alt_enter=Enter+Alt+Enter (기본), enter_only=Enter 한번, "
            "double_click=win32 WM_LBUTTONDBLCLK 메시지."
        ),
    )
    parser.add_argument(
        "--tab-mode",
        type=str,
        choices=("friends", "chats", "any"),
        default="friends",
        help=(
            "어느 탭에서 검색할지. friends=친구 탭 강제(기본·R7 방지), "
            "chats=채팅 탭 강제('나와의 채팅' 같이 친구로 안 잡히는 톡방 대응), "
            "any=어느 탭이든 OK."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    # 모듈 import 시점에 이미 stdout/stderr utf-8 재설정 완료
    if sys.platform != "win32":
        log.error("이 스크립트는 Windows 전용 (카톡 PC 가 Windows 에만 있음)")
        return 2

    target = WindowSpec(title_exact=args.target)

    if args.preflight:
        result = _run_preflight(target, args.open_method, args.tab_mode)
        log.info(
            "PREFLIGHT 결과: %s, 완료 step=%s, 실패=%s",
            "✅" if result.success else "❌",
            result.completed_steps,
            result.failed_step,
        )
        if not result.success:
            log.warning("실패 사유: %s", result.error_reason)
            return 1
        log.info("✅ 사전 점검 통과 — `--count N` 으로 실 발송 가능")
        return 0

    results = _run_sends(target, args.count, args.inter_delay, args.open_method, args.tab_mode)
    _print_summary(results, args.count)

    all_succeeded = len(results) == args.count and all(r.success for r in results)
    return 0 if all_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
