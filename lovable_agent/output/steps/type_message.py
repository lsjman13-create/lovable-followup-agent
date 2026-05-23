"""메시지 본문 RichEdit 에 입력.

kakao-sender v2 의 실측에 따르면 카톡의 반자동화 감지가 WM_SETTEXT 만으로 채워진
입력창에 대해 전송 버튼을 비활성 상태로 유지하므로, 텍스트도 클립보드 + Ctrl+V
경로로 보낸다.

테스트 친화성을 위해 `paste_func` 주입 가능.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from lovable_agent.output import hwnd_utils
from lovable_agent.output.steps.base import SendContext, Step, StepError

log = logging.getLogger(__name__)

DEFAULT_AFTER_PASTE_SEC = 0.3


class TypeMessageStep:
    name = "type_message"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        hotkey: Callable[..., None] | None = None,
        after_paste_sec: float = DEFAULT_AFTER_PASTE_SEC,
    ) -> None:
        self._sleep = sleep
        self._hotkey = hotkey
        self._after_paste = after_paste_sec

    def _get_hotkey(self) -> Callable[..., None]:
        if self._hotkey is not None:
            return self._hotkey
        import pyautogui  # type: ignore

        pyautogui.FAILSAFE = False
        return pyautogui.hotkey

    def execute(self, ctx: SendContext) -> None:
        if ctx.chat_hwnd is None or ctx.richedit_hwnd is None:
            raise StepError(self.name, "chat_hwnd / richedit_hwnd 가 없음")

        # 채팅창 포그라운드 + RichEdit 포커스
        hwnd_utils.force_foreground(ctx.chat_hwnd)
        self._sleep(0.1)
        hwnd_utils.set_focus(ctx.richedit_hwnd)

        # 클립보드 + Ctrl+V (WM_SETTEXT 만으론 카톡 전송 버튼이 활성화 안 됨)
        hwnd_utils.set_clipboard_text(ctx.message)
        self._sleep(0.05)
        self._get_hotkey()("ctrl", "v")
        self._sleep(self._after_paste)
        ctx.completed_steps.append(self.name)


_check: Step = TypeMessageStep()
