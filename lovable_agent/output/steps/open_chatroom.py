"""검색 + 채팅창 별도 창 오픈.

흐름:
1. 메인 창 포그라운드 확보
2. Ctrl+F 검색창 활성화
3. 검색 Edit HWND 직접 클리어 + 검색어 클립보드·Ctrl+V
4. Enter (검색 결과 첫 항목 선택) + Alt+Enter (별도 창)
5. HWND diff 로 새로 생긴 채팅창 식별 (방어선 2 의 두 번째 단계)

이 Step 의 책임은 "채팅창을 열고 ctx.chat_hwnd 를 채우는 것" 까지. 제목 검증은
VerifyChatroomTitleStep 이 따로 담당 (방어선 3).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from lovable_agent.output import hwnd_utils, window_spec
from lovable_agent.output.steps.base import SendContext, Step, StepError

log = logging.getLogger(__name__)

# 기본 타이밍 — 카톡 응답 시간 고려해 보수적으로
DEFAULT_AFTER_CTRL_F_SEC = 0.4
DEFAULT_AFTER_PASTE_SEC = 0.6
DEFAULT_AFTER_ENTER_SEC = 0.25
DEFAULT_NEW_CHAT_TIMEOUT_SEC = 3.0
DEFAULT_POLL_INTERVAL_SEC = 0.1


class OpenChatroomStep:
    name = "open_chatroom"

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        hotkey: Callable[..., None] | None = None,
        press: Callable[[str], None] | None = None,
        after_ctrl_f_sec: float = DEFAULT_AFTER_CTRL_F_SEC,
        after_paste_sec: float = DEFAULT_AFTER_PASTE_SEC,
        after_enter_sec: float = DEFAULT_AFTER_ENTER_SEC,
        new_chat_timeout_sec: float = DEFAULT_NEW_CHAT_TIMEOUT_SEC,
    ) -> None:
        self._sleep = sleep
        self._clock = clock
        self._hotkey = hotkey
        self._press = press
        self._after_ctrl_f = after_ctrl_f_sec
        self._after_paste = after_paste_sec
        self._after_enter = after_enter_sec
        self._new_chat_timeout = new_chat_timeout_sec

    def _get_hotkey(self) -> Callable[..., None]:
        if self._hotkey is not None:
            return self._hotkey
        import pyautogui  # type: ignore

        pyautogui.FAILSAFE = False
        return pyautogui.hotkey

    def _get_press(self) -> Callable[[str], None]:
        if self._press is not None:
            return self._press
        import pyautogui  # type: ignore

        return pyautogui.press

    def execute(self, ctx: SendContext) -> None:
        if ctx.main_hwnd is None:
            raise StepError(self.name, "main_hwnd 가 채워지지 않음 — EnsureFriendsTabStep 먼저")

        hwnd_utils.force_foreground(ctx.main_hwnd)
        self._sleep(0.15)

        # 1) Ctrl+F
        self._get_hotkey()("ctrl", "f")
        self._sleep(self._after_ctrl_f)

        # 2) 검색 Edit 찾기 + 클리어 + 검색어 입력
        search_edit = window_spec.find_friend_tab_search_edit(ctx.main_hwnd)
        if search_edit is None:
            raise StepError(self.name, "친구 탭 검색 Edit 을 찾지 못함")

        hwnd_utils.clear_edit(search_edit)
        hwnd_utils.set_focus(search_edit)
        hwnd_utils.set_clipboard_text(ctx.target.title_exact)
        self._sleep(0.05)
        self._get_hotkey()("ctrl", "v")
        self._sleep(self._after_paste)

        # 3) Enter + Alt+Enter
        self._get_press()("enter")
        self._sleep(self._after_enter)
        self._get_hotkey()("alt", "enter")

        # 4) HWND diff 대기
        new_hwnd = self._wait_new_chat(ctx.hwnds_before_open, ctx.main_hwnd)
        if new_hwnd is None:
            raise StepError(
                self.name,
                f"Alt+Enter 후 새 채팅창이 안 열림 (검색결과 없음 / 단축키 미수신 가능성): "
                f"target={ctx.target.title_exact!r}",
            )
        ctx.chat_hwnd = new_hwnd
        ctx.completed_steps.append(self.name)
        log.debug("새 채팅창 열림: hwnd=%s", hex(new_hwnd))

    def _wait_new_chat(self, prior: set[int], main_hwnd: int) -> int | None:
        deadline = self._clock() + self._new_chat_timeout
        while self._clock() < deadline:
            diff = window_spec.snapshot_chat_hwnds(exclude={main_hwnd}) - prior
            if diff:
                # 여러 개가 동시에 생기는 극히 드문 케이스 — 임의 하나 선택
                return next(iter(diff))
            self._sleep(DEFAULT_POLL_INTERVAL_SEC)
        return None


_check: Step = OpenChatroomStep()
