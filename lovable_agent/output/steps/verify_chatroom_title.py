"""방어선 3 — 채팅창 제목 완전일치 검증 + RichEdit 자식 확인.

`OpenChatroomStep` 이 연 채팅창의 제목이 `ctx.target.title_exact` 와 정확히 일치해야
만 전송 진행. 불일치 시 즉시 WM_CLOSE 로 채팅창 닫고 StepError raise — 엉뚱한
대상에게 메시지가 가는 것을 차단.
"""

from __future__ import annotations

import logging

from lovable_agent.output import hwnd_utils, window_spec
from lovable_agent.output.steps.base import SendContext, Step, StepError

log = logging.getLogger(__name__)


class VerifyChatroomTitleStep:
    name = "verify_chatroom_title"

    def execute(self, ctx: SendContext) -> None:
        if ctx.chat_hwnd is None:
            raise StepError(self.name, "chat_hwnd 가 없음 — OpenChatroomStep 먼저")

        # window_spec.matches_hwnd 가 제목 완전일치 + RichEdit 자식 존재까지 검증
        if not window_spec.matches_hwnd(ctx.target, ctx.chat_hwnd):
            # 안전 측에서 즉시 닫음
            hwnd_utils.post_close(ctx.chat_hwnd)
            raise StepError(
                self.name,
                f"채팅창 매칭 실패 (제목 불일치 또는 RichEdit 자식 없음): "
                f"target={ctx.target.title_exact!r}, hwnd={hex(ctx.chat_hwnd)} — "
                f"안전 측에서 채팅창을 닫았습니다.",
            )

        # RichEdit HWND 도 ctx 에 저장 — 다음 Step (TypeMessage) 가 사용
        richedit_hwnd = window_spec.find_richedit_in_chat(
            ctx.chat_hwnd, expected_class=ctx.target.expected_input_class
        )
        if richedit_hwnd is None:
            hwnd_utils.post_close(ctx.chat_hwnd)
            raise StepError(
                self.name,
                f"RichEdit({ctx.target.expected_input_class!r}) HWND 를 찾지 못함",
            )
        ctx.richedit_hwnd = richedit_hwnd
        ctx.completed_steps.append(self.name)
        log.debug(
            "채팅창 검증 통과: chat=%s richedit=%s",
            hex(ctx.chat_hwnd),
            hex(richedit_hwnd),
        )


_check: Step = VerifyChatroomTitleStep()
