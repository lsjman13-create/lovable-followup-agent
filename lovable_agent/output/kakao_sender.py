"""KakaoTalk Sender 오케스트레이션 — Step 6개를 순서대로 실행.

흐름 (ARCHITECTURE §4.6):
1. enforce_prefix — `[AI 자동 팔로우업] ` 접두어 강제 (Step 외부)
2. 화이트리스트 더블체크 (호출자 책임 — 본 모듈에 들어오기 전)
3. Step 1: EnsureFriendsTab    ← 방어선 1
4. Step 2: SnapshotHwnds       ← 방어선 2 (전)
5. Step 3: OpenChatroom        ← Ctrl+F + Enter + Alt+Enter + HWND diff
6. Step 4: VerifyChatroomTitle ← 방어선 3 (제목 완전일치 + RichEdit 자식 확인)
7. Step 5: TypeMessage
8. Step 6: PressEnter + 전송 검증

실패 시 어느 Step 에서 깨졌는지가 `SendResult.failed_step` 으로 명확.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from lovable_agent.domain import WindowSpec
from lovable_agent.output.steps.base import SendContext, Step, StepError
from lovable_agent.output.steps.ensure_friends_tab import EnsureFriendsTabStep
from lovable_agent.output.steps.open_chatroom import OpenChatroomStep
from lovable_agent.output.steps.press_enter import PressEnterStep
from lovable_agent.output.steps.snapshot_hwnds import SnapshotHwndsStep
from lovable_agent.output.steps.type_message import TypeMessageStep
from lovable_agent.output.steps.verify_chatroom_title import VerifyChatroomTitleStep
from lovable_agent.safety.prefix import enforce_prefix

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    """발송 1회의 결과."""

    success: bool
    completed_steps: list[str]
    failed_step: str | None = None  # 실패 Step 이름 (성공 시 None)
    error_reason: str | None = None


def default_steps() -> list[Step]:
    """기본 Step 시퀀스 — Phase 2 본구현 기준."""
    return [
        EnsureFriendsTabStep(),
        SnapshotHwndsStep(),
        OpenChatroomStep(),
        VerifyChatroomTitleStep(),
        TypeMessageStep(),
        PressEnterStep(),
    ]


class KakaoSender:
    """카톡 발송 오케스트레이션.

    Step 시퀀스는 생성자에서 주입받아 테스트·확장 가능. 기본은 `default_steps()`.
    """

    def __init__(self, steps: Sequence[Step] | None = None) -> None:
        self._steps: list[Step] = list(steps) if steps else default_steps()

    def send(self, target: WindowSpec, message: str) -> SendResult:
        """target 채팅창에 message 전송.

        Args:
            target: 발송 대상 채팅창 식별 정보 (제목 완전일치).
            message: 본문. 자동으로 [AI 자동 팔로우업] 접두어가 prepend 됨.

        Returns:
            SendResult — 어느 단계까지 통과했는지 + 실패 사유.
        """
        ctx = SendContext(target=target, message=enforce_prefix(message))
        log.info(
            "발송 시작: target=%r, message=%r",
            target.title_exact,
            ctx.message[:60],
        )

        for step in self._steps:
            try:
                step.execute(ctx)
                log.debug("Step ✓ %s", step.name)
            except StepError as e:
                log.warning("Step ✗ %s: %s", e.step_name, e.reason)
                return SendResult(
                    success=False,
                    completed_steps=list(ctx.completed_steps),
                    failed_step=e.step_name,
                    error_reason=e.reason,
                )
            except Exception as e:  # 예기치 못한 예외 — 안전 측에서 실패 보고
                log.exception("Step 예외 %s", step.name)
                return SendResult(
                    success=False,
                    completed_steps=list(ctx.completed_steps),
                    failed_step=step.name,
                    error_reason=f"{type(e).__name__}: {e}",
                )

        log.info("발송 성공: target=%r", target.title_exact)
        return SendResult(success=True, completed_steps=list(ctx.completed_steps))
