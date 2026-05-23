"""Enter 키로 메시지 전송 + RichEdit 가 비워졌는지 검증.

전송 후 잠시 대기 → RichEdit 의 현재 텍스트를 읽어 우리가 보낸 본문이 남아있지
않은지 확인. 카톡 반자동화 감지로 전송이 차단된 경우 false positive (전송 실패를
성공으로 보고) 를 방지.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from lovable_agent.output import hwnd_utils
from lovable_agent.output.steps.base import SendContext, Step, StepError

log = logging.getLogger(__name__)

DEFAULT_AFTER_ENTER_SEC = 0.4


class PressEnterStep:
    name = "press_enter"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        press: Callable[[str], None] | None = None,
        verify_sent: bool = True,
        after_enter_sec: float = DEFAULT_AFTER_ENTER_SEC,
    ) -> None:
        self._sleep = sleep
        self._press = press
        self._verify = verify_sent
        self._after_enter = after_enter_sec

    def _get_press(self) -> Callable[[str], None]:
        if self._press is not None:
            return self._press
        import pyautogui  # type: ignore

        return pyautogui.press

    def execute(self, ctx: SendContext) -> None:
        if ctx.chat_hwnd is None or ctx.richedit_hwnd is None:
            raise StepError(self.name, "chat_hwnd / richedit_hwnd 가 없음")

        self._get_press()("enter")
        self._sleep(self._after_enter)

        if self._verify:
            remaining = hwnd_utils.get_edit_text(ctx.richedit_hwnd)
            sent = ctx.message.strip()
            # 우리가 보낸 본문이 RichEdit 에 그대로 남아있다면 전송 실패
            if sent and sent in remaining:
                raise StepError(
                    self.name,
                    f"전송 실패 — 보낸 본문이 입력창에 남아있음. "
                    f"카톡 반자동화 감지 또는 '엔터로 전송' 설정을 확인하세요. "
                    f"remaining={remaining[:80]!r}",
                )
        ctx.completed_steps.append(self.name)


_check: Step = PressEnterStep()
